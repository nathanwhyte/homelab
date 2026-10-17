#!/usr/bin/env python3
"""Embedding prompt shim between OpenViking and the embedder (IMPR-1344).

Qwen3-Embedding is instruction-aware: queries should arrive as
"Instruct: <task>\\nQuery: <text>" and documents as raw text. OpenViking's
openai provider cannot prepend text, but with query_param/document_param set it
sends an `input_type` body field (query|document) that llama-server ignores.
This shim reads that field on POST .../embeddings, prepends SHIM_QUERY_PREFIX
to query inputs (SHIM_DOCUMENT_PREFIX to documents, empty by default), drops the
field, and forwards to SHIM_UPSTREAM. An embeddings request without a known
input_type is rejected with 400 rather than guessed. Every other request is
passed through unchanged.

A document request with an empty prefix is forwarded with only input_type
removed and its input untouched, so stored document vectors stay identical to
direct-embedder vectors and no reindex is needed. Ported from the TASK-1218 prototype (prefix-shim.py). Stdlib only.

GET /healthz answers locally and never touches the upstream.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# Headers copied onto the upstream request; hop-by-hop ones are left out.
FORWARD_HEADERS = ("Authorization", "Accept")
LOG_EVERY = 500


class Config:
    def __init__(self, upstream: str, prompts: dict[str, str], timeout: float):
        self.upstream = upstream.rstrip("/")
        self.prompts = prompts
        self.timeout = timeout


def config_from_env(env=os.environ) -> Config:
    return Config(
        upstream=env["SHIM_UPSTREAM"],
        prompts={
            "query": env["SHIM_QUERY_PREFIX"],
            "document": env.get("SHIM_DOCUMENT_PREFIX", ""),
        },
        timeout=float(env.get("SHIM_UPSTREAM_TIMEOUT", "600")),
    )


class Stats:
    def __init__(self):
        self._lock = threading.Lock()
        self._counts: Counter[str] = Counter()

    def add(self, key: str, n: int = 1) -> None:
        with self._lock:
            before = sum(self._counts.values())
            self._counts[key] += n
            if before // LOG_EVERY != (before + n) // LOG_EVERY:
                print(json.dumps(dict(self._counts)), flush=True)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)


def rewrite(body: dict, prompts: dict[str, str]) -> tuple[bytes | None, str]:
    """Return (forward body or None if unchanged, input_type) or raise ValueError."""
    kind = body.pop("input_type", None)
    if kind not in prompts:
        raise ValueError(f"input_type {kind!r} is not one of {sorted(prompts)}")
    prefix = prompts[kind]
    if not prefix:
        return json.dumps(body).encode(), kind
    inputs = body.get("input")
    items = inputs if isinstance(inputs, list) else [inputs]
    if not items or not all(isinstance(t, str) for t in items):
        raise ValueError("input must be a string or a list of strings")
    body["input"] = [prefix + t for t in items]
    return json.dumps(body).encode(), kind


def make_handler(cfg: Config, stats: Stats):
    class Shim(BaseHTTPRequestHandler):
        def _reply(self, status: int, data: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _error(self, status: int, message: str) -> None:
            stats.add(f"http_{status}")
            payload = {"error": {"code": status, "message": f"shim: {message}"}}
            self._reply(status, json.dumps(payload).encode())

        def _forward(self, method: str, body: bytes | None = None) -> None:
            headers = {"Content-Type": "application/json"}
            for name in FORWARD_HEADERS:
                if self.headers.get(name):
                    headers[name] = self.headers[name]
            req = Request(
                cfg.upstream + self.path, data=body, method=method, headers=headers
            )
            try:
                with urlopen(req, timeout=cfg.timeout) as r:
                    status, data = r.status, r.read()
            except HTTPError as e:
                status, data = e.code, e.read()
            except (URLError, OSError) as e:
                self._error(502, f"upstream unreachable: {e}")
                return
            self._reply(status, data)

        def do_GET(self):
            if self.path == "/healthz":
                self._reply(200, json.dumps(stats.snapshot()).encode())
                return
            self._forward("GET")

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if not self.path.rstrip("/").endswith("/embeddings"):
                self._forward("POST", raw)
                return
            try:
                body = json.loads(raw or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("body must be a JSON object")
                data, kind = rewrite(body, cfg.prompts)
            except ValueError as e:  # JSONDecodeError is a ValueError
                self._error(400, str(e))
                return
            inputs = body.get("input")
            stats.add(kind, len(inputs) if isinstance(inputs, list) else 1)
            self._forward("POST", data)

        def log_message(self, *args):
            pass

    return Shim


def main() -> int:
    cfg = config_from_env()
    port = int(os.environ.get("SHIM_PORT", "8080"))
    stats = Stats()
    print(
        f"shim :{port} -> {cfg.upstream} prompts={cfg.prompts!r}",
        flush=True,
    )
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(cfg, stats))
    server.daemon_threads = True
    # Kubernetes stops the pod with SIGTERM; exit through the finally below.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("final", json.dumps(stats.snapshot()), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
