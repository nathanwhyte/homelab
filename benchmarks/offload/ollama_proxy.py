"""Minimal Ollama-API shim in front of an OpenAI-compatible server (oMLX), for proposer-shadow.py.

Translates POST /api/chat (think, format, options) into POST /v1/chat/completions
(chat_template_kwargs.enable_thinking, response_format json_schema, sampling,
max_tokens) and answers /api/version and /api/ps. Every request is forwarded to
one fixed upstream model.

Sampling defaults mirror the baseline tag's Modelfile (`ollama show --parameters
qwen3.6:35b-mlx`: top_p 0.95, top_k 20, min_p 0, presence_penalty 1.5,
repeat_penalty 1), so both sides decode under the same settings; request options
override them. Each request appends one JSON line to --log with the upstream
Warning header (set when oMLX falls back from grammar-constrained decoding),
the reasoning length, and token counts.
"""

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ARGS = None

# Ollama option name -> OpenAI/oMLX field, with the baseline Modelfile's value.
SAMPLING = {
    "top_p": ("top_p", 0.95),
    "top_k": ("top_k", 20),
    "min_p": ("min_p", 0.0),
    "presence_penalty": ("presence_penalty", 1.5),
    "repeat_penalty": ("repetition_penalty", 1.0),
}


def upstream(path, body=None, timeout=3600):
    req = urllib.request.Request(
        ARGS.upstream + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r), r.headers.get("Warning")


def translate(body):
    opts = body.get("options") or {}
    out = {
        "model": ARGS.model,
        "messages": body["messages"],
        "stream": False,
        "temperature": opts.get("temperature", 0),
        "max_tokens": opts.get("num_predict", 1024),
        "chat_template_kwargs": {"enable_thinking": bool(body.get("think", False))},
    }
    for ollama_key, (openai_key, default) in SAMPLING.items():
        out[openai_key] = opts.get(ollama_key, default)
    fmt = body.get("format")
    if isinstance(fmt, dict):
        out["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "reply", "schema": fmt, "strict": True},
        }
    elif fmt == "json":
        out["response_format"] = {"type": "json_object"}
    return out


def log_line(record):
    if ARGS.log:
        with open(ARGS.log, "a") as f:
            f.write(json.dumps(record) + "\n")


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/api/version":
            self._send(200, {"version": f"omlx-proxy:{ARGS.model}"})
        elif self.path == "/api/ps":
            self._send(200, {"models": []})
        else:
            self._send(404, {"error": self.path})

    def do_POST(self):
        if self.path != "/api/chat":
            self._send(404, {"error": self.path})
            return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        req = translate(body)
        t0 = time.perf_counter()
        try:
            r, warning = upstream("/v1/chat/completions", req)
        except urllib.error.HTTPError as e:
            err = e.read().decode()[:2000]
            log_line({"status": e.code, "error": err[:300]})
            self._send(e.code, {"error": err})
            return
        wall_ns = int((time.perf_counter() - t0) * 1e9)
        msg = r["choices"][0]["message"]
        usage = r.get("usage") or {}
        reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
        log_line(
            {
                "status": 200,
                "model": req["model"],
                "sent": {
                    k: req[k]
                    for k in ("temperature", "max_tokens", "chat_template_kwargs")
                    + tuple(openai_key for openai_key, _ in SAMPLING.values())
                },
                "schema": "response_format" in req,
                "grammar_warning": warning,
                "content_chars": len(msg.get("content") or ""),
                "reasoning_chars": len(reasoning),
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "finish_reason": r["choices"][0].get("finish_reason"),
                "wall_s": round(wall_ns / 1e9, 3),
            }
        )
        self._send(
            200,
            {
                "model": body.get("model"),
                "message": {"role": "assistant", "content": msg.get("content") or ""},
                "done": True,
                "prompt_eval_count": usage.get("prompt_tokens"),
                "eval_count": usage.get("completion_tokens"),
                # End-to-end request time: proposer-shadow's decode tok/s is not
                # comparable with Ollama's for this side.
                "total_duration": wall_ns,
                "eval_duration": wall_ns,
            },
        )

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--upstream",
        default=os.environ.get("OMLX_BASE") or "http://127.0.0.1:8000",
        help="OpenAI-compatible server base URL ($OMLX_BASE)",
    )
    ap.add_argument("--model", required=True)
    ap.add_argument("--port", type=int, default=11500)
    ap.add_argument("--log", help="append one JSON line per request")
    ARGS = ap.parse_args()
    ThreadingHTTPServer(("127.0.0.1", ARGS.port), Handler).serve_forever()
