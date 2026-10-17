#!/usr/bin/env python3
"""Tests for embedder-prompt-shim.py (IMPR-1344). Stdlib only, loopback sockets."""

from __future__ import annotations

import importlib.util
import json
import os
import threading
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
    """Records each request; answers 200 with the parsed body, or `status`."""

    requests: list[dict] = []
    status = 200

    def _answer(self, body):
        FakeUpstream.requests.append(
            {
                "method": self.command,
                "path": self.path,
                "body": body,
                "auth": self.headers.get("Authorization"),
            }
        )
        data = json.dumps({"echo": body}).encode()
        self.send_response(FakeUpstream.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._answer(None)

    def do_POST(self):
        self._answer(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))

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
