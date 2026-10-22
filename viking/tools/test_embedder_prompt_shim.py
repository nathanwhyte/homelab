#!/usr/bin/env python3
"""Tests for embedder-prompt-shim.py (IMPR-1344). Stdlib only, loopback sockets."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_scripts" / "lib"))
from git_env import scrub_environ  # noqa: E402

scrub_environ()

import http.client
import importlib.util
import json
import os
import signal
import socket
import subprocess
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

_DIR = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "embedder_prompt_shim", os.path.join(_DIR, "embedder-prompt-shim.py")
)
shim = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(shim)

QUERY_PREFIX = "Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery: "


class FakeUpstream(BaseHTTPRequestHandler):
    """Records each request; answers `status` (200) with the parsed body echoed."""

    requests: list[dict] = []
    status = 200
    extra_headers: list[tuple[str, str]] = []
    delay = 0.0

    def _answer(self, body):
        FakeUpstream.requests.append(
            {
                "method": self.command,
                "path": self.path,
                "body": body,
                "auth": self.headers.get("Authorization"),
                "content_type": self.headers.get("Content-Type"),
                "accept": self.headers.get_all("Accept"),
            }
        )
        time.sleep(FakeUpstream.delay)
        data = json.dumps({"echo": body}).encode()
        self.send_response(FakeUpstream.status)
        self.send_header("Content-Type", "application/json")
        for k, v in FakeUpstream.extra_headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if "Content-Length" in self.headers:
            self.do_POST()
        else:
            self._answer(None)

    def do_HEAD(self):
        # The headers of the GET answer, without its body.
        FakeUpstream.requests.append({"method": "HEAD", "path": self.path})
        self.send_response(FakeUpstream.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(json.dumps({"echo": None}))))
        self.end_headers()

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        try:
            self._answer(json.loads(raw))
        except ValueError:
            self._answer(raw.decode())

    do_OPTIONS = do_DELETE = do_POST

    def log_message(self, *args):
        pass


def serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class ShimTest(unittest.TestCase):
    upstream_url = None

    @classmethod
    def setUpClass(cls):
        cls.upstream = serve(FakeUpstream)
        cls.upstream_url = f"http://127.0.0.1:{cls.upstream.server_port}"
        cls.stats = shim.Stats()
        cfg = shim.Config(
            cls.upstream_url, {"query": QUERY_PREFIX, "document": ""}, timeout=5
        )
        cls.shim = serve(shim.make_handler(cfg, cls.stats))
        cls.base = f"http://127.0.0.1:{cls.shim.server_port}"

    @classmethod
    def tearDownClass(cls):
        for server in (cls.shim, cls.upstream):
            server.shutdown()
            server.server_close()

    def setUp(self):
        FakeUpstream.requests = []
        FakeUpstream.status = 200
        FakeUpstream.extra_headers = []
        FakeUpstream.delay = 0.0

    def raw_request(self, data: bytes, shut_write: bool = False) -> bytes:
        """Send bytes on a bare socket and return the whole response."""
        with socket.create_connection(("127.0.0.1", self.shim.server_port), 5) as s:
            s.sendall(data)
            if shut_write:
                s.shutdown(socket.SHUT_WR)
            out = b""
            while chunk := s.recv(65536):
                out += chunk
        return out

    def call(self, path, body=None, raw=None, headers=None):
        data = (
            raw
            if raw is not None
            else (None if body is None else json.dumps(body).encode())
        )
        req = Request(
            self.base + path,
            data=data,
            method="GET" if data is None else "POST",
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            with urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def test_query_gets_the_prefix_and_loses_input_type(self):
        status, _ = self.call(
            "/v1/embeddings",
            {
                "model": "qwen3-embedding-4b",
                "input": ["neon cold start"],
                "input_type": "query",
            },
        )
        self.assertEqual(status, 200)
        sent = FakeUpstream.requests[0]["body"]
        self.assertEqual(sent["input"], [QUERY_PREFIX + "neon cold start"])
        self.assertNotIn("input_type", sent)
        self.assertEqual(sent["model"], "qwen3-embedding-4b")

    def test_string_query_is_prefixed_as_a_list(self):
        self.call("/v1/embeddings", {"input": "x", "input_type": "query"})
        self.assertEqual(
            FakeUpstream.requests[0]["body"]["input"], [QUERY_PREFIX + "x"]
        )

    def test_document_input_is_untouched(self):
        docs = ["a long document", "another"]
        self.call(
            "/v1/embeddings",
            {"input": docs, "input_type": "document", "dimensions": 2560},
        )
        sent = FakeUpstream.requests[0]["body"]
        self.assertEqual(sent, {"input": docs, "dimensions": 2560})

    def test_string_document_stays_a_string(self):
        self.call("/v1/embeddings", {"input": "doc", "input_type": "document"})
        self.assertEqual(FakeUpstream.requests[0]["body"]["input"], "doc")

    def test_missing_input_type_is_rejected_not_guessed(self):
        status, body = self.call("/v1/embeddings", {"input": ["x"]})
        self.assertEqual(status, 400)
        self.assertIn("input_type None", body["error"]["message"])
        self.assertEqual(FakeUpstream.requests, [])

    def test_unknown_input_type_is_rejected(self):
        status, _ = self.call(
            "/v1/embeddings", {"input": ["x"], "input_type": "passage"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(FakeUpstream.requests, [])

    def test_token_id_query_is_rejected(self):
        status, _ = self.call(
            "/v1/embeddings", {"input": [[1, 2, 3]], "input_type": "query"}
        )
        self.assertEqual(status, 400)

    def test_bad_json_is_rejected(self):
        status, _ = self.call("/v1/embeddings", raw=b"{not json")
        self.assertEqual(status, 400)

    def test_authorization_is_forwarded(self):
        self.call(
            "/v1/embeddings",
            {"input": ["x"], "input_type": "document"},
            headers={"Authorization": "Bearer sk-no-key-required"},
        )
        self.assertEqual(FakeUpstream.requests[0]["auth"], "Bearer sk-no-key-required")

    def test_other_requests_pass_through(self):
        status, _ = self.call("/v1/models")
        self.assertEqual(status, 200)
        self.assertEqual(FakeUpstream.requests[0]["path"], "/v1/models")
        self.call("/v1/other", {"input_type": "kept"})
        self.assertEqual(FakeUpstream.requests[1]["body"], {"input_type": "kept"})

    def test_upstream_error_status_is_relayed(self):
        FakeUpstream.status = 503
        status, _ = self.call("/v1/embeddings", {"input": ["x"], "input_type": "query"})
        self.assertEqual(status, 503)

    def test_healthz_does_not_touch_the_upstream(self):
        status, _ = self.call("/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(FakeUpstream.requests, [])

    def test_query_string_does_not_bypass_the_rewrite(self):
        status, _ = self.call(
            "/v1/embeddings?trace=1", {"input": "x", "input_type": "query"}
        )
        self.assertEqual(status, 200)
        sent = FakeUpstream.requests[0]
        self.assertEqual(sent["path"], "/v1/embeddings?trace=1")
        self.assertEqual(sent["body"], {"input": [QUERY_PREFIX + "x"]})
        status, _ = self.call("/v1/embeddings?trace=1", {"input": "x"})
        self.assertEqual(status, 400)

    def test_percent_encoded_path_is_refused(self):
        # cpp-httplib decodes %XX and %uXXXX; the shim refuses both, any method.
        for path in ("/v1/%65mbeddings", "/v1/%u0065mbeddings", "/v1/models%2F"):
            for body in ({"input": "x"}, {"input": "x", "input_type": "query"}):
                status, out = self.call(path, body)
                self.assertEqual(status, 400, path)
                self.assertIn("percent-encoded", out["error"]["message"])
            status, _ = self.call(path)
            self.assertEqual(status, 400, path)
        self.assertEqual(FakeUpstream.requests, [])

    def test_percent_in_the_query_string_is_forwarded(self):
        status, _ = self.call(
            "/v1/embeddings?q=%20", {"input": "x", "input_type": "query"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(FakeUpstream.requests[0]["path"], "/v1/embeddings?q=%20")

    def test_absolute_form_target_is_refused(self):
        out = self.raw_request(
            b"GET http://embedder/v1/models HTTP/1.1\r\nHost: shim\r\n\r\n"
        )
        self.assertTrue(out.startswith(b"HTTP/1.0 400"), out[:40])
        self.assertEqual(FakeUpstream.requests, [])

    def test_every_embeddings_route_is_rewritten(self):
        for path in ("/embedding", "/embeddings", "/v1/embeddings/"):
            status, _ = self.call(path, {"input": "x"})
            self.assertEqual(status, 400, path)
            FakeUpstream.requests = []
            self.call(path, {"input": "x", "input_type": "query"})
            self.assertEqual(
                FakeUpstream.requests[0]["body"],
                {"input": [QUERY_PREFIX + "x"]},
                path,
            )

    def test_head_is_forwarded_with_get_framing(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.shim.server_port, timeout=5)
        conn.request("HEAD", "/v1/models")
        resp = conn.getresponse()
        self.assertEqual(resp.read(), b"")
        self.assertEqual(resp.status, 200)
        self.assertEqual(
            resp.getheader("Content-Length"), str(len(json.dumps({"echo": None})))
        )
        self.assertEqual(
            FakeUpstream.requests, [{"method": "HEAD", "path": "/v1/models"}]
        )
        # No stray body bytes: the same connection carries the next request.
        if not resp.will_close:
            conn.request("GET", "/v1/models")
            nxt = conn.getresponse()
            self.assertEqual(json.loads(nxt.read()), {"echo": None})
        conn.close()

    def test_repeated_request_headers_are_kept(self):
        self.raw_request(
            b"GET /v1/models HTTP/1.1\r\nHost: shim\r\n"
            b"Accept: application/json\r\nAccept: text/plain\r\n"
            b"Connection: close\r\n\r\n"
        )
        self.assertEqual(
            FakeUpstream.requests[0]["accept"], ["application/json", "text/plain"]
        )

    def test_get_body_is_forwarded(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.shim.server_port, timeout=5)
        conn.request("GET", "/v1/models", b"payload", {"Content-Type": "text/plain"})
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 200)
        self.assertEqual(FakeUpstream.requests[0]["body"], "payload")

    def test_head_healthz_is_local(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.shim.server_port, timeout=5)
        conn.request("HEAD", "/healthz")
        resp = conn.getresponse()
        self.assertEqual(resp.read(), b"")
        conn.close()
        self.assertEqual(resp.status, 200)
        self.assertGreater(int(resp.getheader("Content-Length")), 0)
        self.assertEqual(FakeUpstream.requests, [])

    def test_options_and_delete_pass_through(self):
        for method in ("OPTIONS", "DELETE"):
            conn = http.client.HTTPConnection(
                "127.0.0.1", self.shim.server_port, timeout=5
            )
            conn.request(method, "/v1/embeddings", headers={"Origin": "http://x"})
            resp = conn.getresponse()
            resp.read()
            conn.close()
            self.assertEqual(resp.status, 200, method)
        self.assertEqual(
            [(r["method"], r["path"]) for r in FakeUpstream.requests],
            [("OPTIONS", "/v1/embeddings"), ("DELETE", "/v1/embeddings")],
        )

    def test_expect_100_continue_gets_an_interim_response(self):
        body = json.dumps({"input": "x", "input_type": "document"}).encode()
        head = (
            b"POST /v1/embeddings HTTP/1.1\r\nHost: shim\r\n"
            b"Content-Type: application/json\r\nExpect: 100-continue\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n"
        )
        with socket.create_connection(("127.0.0.1", self.shim.server_port), 5) as s:
            s.sendall(head)
            self.assertIn(b" 100 ", s.recv(1024))
            s.sendall(body)
            out = b""
            while chunk := s.recv(65536):
                out += chunk
        self.assertIn(b" 200 ", out.split(b"\r\n", 1)[0])
        self.assertEqual(FakeUpstream.requests[0]["body"], {"input": "x"})

    def test_non_string_input_type_is_a_400(self):
        for kind in ([], {}, 1):
            status, _ = self.call("/v1/embeddings", {"input": "x", "input_type": kind})
            self.assertEqual(status, 400, kind)
        self.assertEqual(FakeUpstream.requests, [])

    def test_chunked_body_is_decoded(self):
        body = json.dumps({"input": "x", "input_type": "query"}).encode()
        conn = http.client.HTTPConnection("127.0.0.1", self.shim.server_port, timeout=5)
        conn.request(
            "POST",
            "/v1/embeddings",
            iter([body[:7], body[7:]]),
            {"Content-Type": "application/json", "Transfer-Encoding": "chunked"},
            encode_chunked=True,
        )
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 200)
        self.assertEqual(
            FakeUpstream.requests[0]["body"], {"input": [QUERY_PREFIX + "x"]}
        )

    def test_short_body_is_rejected_not_forwarded(self):
        body = json.dumps({"input": "x", "input_type": "document"}).encode()
        head = (
            b"POST /v1/embeddings HTTP/1.1\r\nHost: shim\r\n"
            b"Content-Type: application/json\r\nContent-Length: 500\r\n\r\n"
        )
        out = self.raw_request(head + body, shut_write=True)
        self.assertTrue(out.startswith(b"HTTP/1.0 400"), out[:40])
        self.assertEqual(FakeUpstream.requests, [])

    def test_bad_content_length_is_a_400(self):
        out = self.raw_request(
            b"POST /v1/embeddings HTTP/1.1\r\nHost: shim\r\nContent-Length: ten\r\n\r\n"
        )
        self.assertTrue(out.startswith(b"HTTP/1.0 400"), out[:40])

    def test_end_to_end_headers_are_kept(self):
        FakeUpstream.status = 429
        FakeUpstream.extra_headers = [("Retry-After", "17")]
        req = Request(
            self.base + "/v1/other",
            data=b"plain",
            method="POST",
            headers={"Content-Type": "text/plain"},
        )
        with self.assertRaises(HTTPError) as ctx:
            urlopen(req, timeout=5)
        with ctx.exception as e:
            self.assertEqual(e.code, 429)
            self.assertEqual(e.headers["Retry-After"], "17")
        self.assertEqual(FakeUpstream.requests[0]["content_type"], "text/plain")
        self.assertEqual(FakeUpstream.requests[0]["body"], "plain")

    def test_redirect_is_relayed_not_followed(self):
        FakeUpstream.status = 302
        FakeUpstream.extra_headers = [("Location", "/elsewhere")]
        conn = http.client.HTTPConnection("127.0.0.1", self.shim.server_port, timeout=5)
        conn.request("GET", "/v1/models")
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 302)
        self.assertEqual(resp.getheader("Location"), "/elsewhere")
        self.assertEqual(len(FakeUpstream.requests), 1)


class DrainOnSigtermTest(unittest.TestCase):
    def test_request_in_flight_finishes_after_sigterm(self):
        upstream = serve(FakeUpstream)
        FakeUpstream.requests, FakeUpstream.status = [], 200
        FakeUpstream.extra_headers, FakeUpstream.delay = [], 1.5
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        env = {
            **os.environ,
            "SHIM_PORT": str(port),
            "SHIM_UPSTREAM": f"http://127.0.0.1:{upstream.server_port}",
            "SHIM_QUERY_PREFIX": "q: ",
        }
        proc = subprocess.Popen(
            [sys.executable, os.path.join(_DIR, "embedder-prompt-shim.py")],
            env=env,
            stdout=subprocess.DEVNULL,
        )
        try:
            for _ in range(50):
                try:
                    socket.create_connection(("127.0.0.1", port), 0.2).close()
                    break
                except OSError:
                    time.sleep(0.1)
            result = {}

            def post():
                req = Request(
                    f"http://127.0.0.1:{port}/v1/embeddings",
                    data=json.dumps({"input": "x", "input_type": "document"}).encode(),
                    method="POST",
                    headers={"Content-Type": "application/json"},
                )
                with urlopen(req, timeout=10) as r:
                    result["status"] = r.status

            t = threading.Thread(target=post)
            t.start()
            time.sleep(0.5)  # the upstream is now holding the request
            proc.send_signal(signal.SIGTERM)
            t.join(10)
            self.assertEqual(result.get("status"), 200)
            self.assertEqual(proc.wait(10), 0)
        finally:
            FakeUpstream.delay = 0.0
            if proc.poll() is None:
                proc.kill()
            upstream.shutdown()
            upstream.server_close()


class UpstreamDownTest(unittest.TestCase):
    def test_unreachable_upstream_is_a_502(self):
        probe = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
        port = probe.server_port
        probe.server_close()  # nothing listens on this port now
        cfg = shim.Config(
            f"http://127.0.0.1:{port}", {"query": "q: ", "document": ""}, 2
        )
        server = serve(shim.make_handler(cfg, shim.Stats()))
        try:
            req = Request(
                f"http://127.0.0.1:{server.server_port}/v1/embeddings",
                data=json.dumps({"input": ["x"], "input_type": "query"}).encode(),
                method="POST",
            )
            with self.assertRaises(HTTPError) as ctx:
                urlopen(req, timeout=5)
            ctx.exception.close()
            self.assertEqual(ctx.exception.code, 502)
        finally:
            server.shutdown()
            server.server_close()


class ConfigTest(unittest.TestCase):
    def test_env_config(self):
        cfg = shim.config_from_env(
            {"SHIM_UPSTREAM": "http://u:8080/", "SHIM_QUERY_PREFIX": "q: "}
        )
        self.assertEqual(cfg.upstream, "http://u:8080")
        self.assertEqual(cfg.prompts, {"query": "q: ", "document": ""})
        self.assertEqual(cfg.timeout, 600)

    def test_query_prefix_is_required(self):
        with self.assertRaises(KeyError):
            shim.config_from_env({"SHIM_UPSTREAM": "http://u"})


class TestConfigMapInSync(unittest.TestCase):
    def test_configmap_carries_the_current_script(self):
        # The pod runs the ConfigMap copy; an unregenerated edit ships nothing.
        path = os.path.join(
            _DIR, "..", "manifests", "embedder-prompt-shim-configmap.yaml"
        )
        with open(path) as fh:
            lines = fh.read().splitlines()
        start = lines.index("  embedder-prompt-shim.py: |") + 1
        body = []
        for line in lines[start:]:
            if line and not line.startswith("    "):
                break
            body.append(line[4:])
        with open(os.path.join(_DIR, "embedder-prompt-shim.py")) as fh:
            source = fh.read().splitlines()
        self.assertEqual(
            [b.rstrip() for b in body],
            [x.rstrip() for x in source],
            "run viking/scripts/regen-prompt-shim-configmap.sh",
        )


if __name__ == "__main__":
    unittest.main()
