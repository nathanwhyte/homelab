#!/usr/bin/env python3
"""Prometheus exporter for Ollama inference metrics.

Exposes /metrics in Prometheus format by polling Ollama's /api/ps endpoint
and tracking per-request metrics via a lightweight proxy or push model.

Usage:
    python3 ollama-exporter.py                        # default: poll localhost:11434, serve :9111
    python3 ollama-exporter.py --ollama http://host:11434 --port 9111
"""

import argparse
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ── Metrics State ────────────────────────────────────────────────────────────

_lock = threading.Lock()
_metrics = {
    # From /api/ps polling
    "ollama_up": 0,
    "ollama_models_loaded": 0,
    "ollama_model_vram_bytes": {},       # {model: bytes}
    "ollama_model_size_bytes": {},       # {model: bytes}
    "ollama_model_expires_at": {},       # {model: unix timestamp}
    "ollama_model_context_length": {},   # {model: max context tokens}
    # From /api/chat response tracking (updated via /push endpoint)
    "ollama_request_total": {},          # {model: count}
    "ollama_gen_tokens_total": {},       # {model: count}
    "ollama_prompt_tokens_total": {},    # {model: count}
    "ollama_gen_duration_seconds": {},   # {model: total seconds}
    "ollama_prompt_duration_seconds": {},# {model: total seconds}
    "ollama_request_duration_seconds": {},# {model: total seconds}
    # Latest per-model snapshot
    "ollama_gen_tokens_per_sec": {},     # {model: latest tok/s}
    "ollama_prompt_tokens_per_sec": {},  # {model: latest tok/s}
    "ollama_last_request_seconds": {},   # {model: last wall time}
    "ollama_last_prompt_tokens": {},     # {model: prompt tokens in last request}
}


def poll_ps(ollama_url):
    """Poll /api/ps and update model-level metrics."""
    try:
        req = urllib.request.Request(f"{ollama_url}/api/ps", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())

        models = data.get("models", [])

        # Fetch context length for each loaded model outside the lock
        ctx_lengths = {}
        for m in models:
            raw_name = m.get("name", "unknown")
            name = raw_name.removesuffix(":latest")
            try:
                show_req = urllib.request.Request(
                    f"{ollama_url}/api/show",
                    data=json.dumps({"name": raw_name}).encode(),
                    method="POST",
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(show_req, timeout=5) as r:
                    show_data = json.loads(r.read())
                # Prefer runtime num_ctx (from modelfile) over architectural max
                params_text = show_data.get("parameters", "")
                ctx_len = None
                for line in params_text.splitlines():
                    parts = line.split()
                    if len(parts) == 2 and parts[0] == "num_ctx":
                        ctx_len = int(parts[1])
                        break
                if ctx_len is None:
                    model_info = show_data.get("model_info", {})
                    raw = next(
                        (v for k, v in model_info.items() if "context_length" in k),
                        None,
                    )
                    if raw:
                        ctx_len = int(raw)
                if ctx_len:
                    ctx_lengths[name] = ctx_len
            except Exception:
                pass

        with _lock:
            _metrics["ollama_up"] = 1
            _metrics["ollama_models_loaded"] = len(models)
            _metrics["ollama_model_vram_bytes"] = {}
            _metrics["ollama_model_size_bytes"] = {}
            _metrics["ollama_model_expires_at"] = {}
            _metrics["ollama_model_context_length"] = ctx_lengths

            for m in models:
                name = m.get("name", "unknown").removesuffix(":latest")
                _metrics["ollama_model_vram_bytes"][name] = m.get("size_vram", 0)
                _metrics["ollama_model_size_bytes"][name] = m.get("size", 0)
                expires = m.get("expires_at", "")
                if expires:
                    try:
                        from datetime import datetime
                        dt = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                        _metrics["ollama_model_expires_at"][name] = dt.timestamp()
                    except Exception:
                        pass

    except Exception:
        with _lock:
            _metrics["ollama_up"] = 0
            _metrics["ollama_models_loaded"] = 0


def push_request_metrics(model, eval_count, eval_duration_ns, prompt_count, prompt_duration_ns, total_duration_ns):
    """Record metrics from a completed /api/chat response."""
    with _lock:
        _metrics["ollama_request_total"].setdefault(model, 0)
        _metrics["ollama_request_total"][model] += 1

        _metrics["ollama_gen_tokens_total"].setdefault(model, 0)
        _metrics["ollama_gen_tokens_total"][model] += eval_count

        _metrics["ollama_prompt_tokens_total"].setdefault(model, 0)
        _metrics["ollama_prompt_tokens_total"][model] += prompt_count

        eval_sec = eval_duration_ns / 1e9 if eval_duration_ns else 0
        prompt_sec = prompt_duration_ns / 1e9 if prompt_duration_ns else 0
        total_sec = total_duration_ns / 1e9 if total_duration_ns else 0

        _metrics["ollama_gen_duration_seconds"].setdefault(model, 0)
        _metrics["ollama_gen_duration_seconds"][model] += eval_sec

        _metrics["ollama_prompt_duration_seconds"].setdefault(model, 0)
        _metrics["ollama_prompt_duration_seconds"][model] += prompt_sec

        _metrics["ollama_request_duration_seconds"].setdefault(model, 0)
        _metrics["ollama_request_duration_seconds"][model] += total_sec

        if eval_sec > 0:
            _metrics["ollama_gen_tokens_per_sec"][model] = round(eval_count / eval_sec, 1)
        if prompt_sec > 0:
            _metrics["ollama_prompt_tokens_per_sec"][model] = round(prompt_count / prompt_sec, 1)
        _metrics["ollama_last_request_seconds"][model] = round(total_sec, 3)
        _metrics["ollama_last_prompt_tokens"][model] = prompt_count


def format_metrics():
    """Render all metrics in Prometheus exposition format."""
    lines = []

    def gauge(name, help_text, value, labels=None):
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} gauge")
        if labels:
            for lv, v in value.items():
                lines.append(f'{name}{{model="{lv}"}} {v}')
        else:
            lines.append(f"{name} {value}")

    def counter(name, help_text, value):
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} counter")
        for lv, v in value.items():
            lines.append(f'{name}{{model="{lv}"}} {v}')

    with _lock:
        gauge("ollama_up", "Whether Ollama is reachable (1=up, 0=down).", _metrics["ollama_up"])
        gauge("ollama_models_loaded", "Number of models currently loaded.", _metrics["ollama_models_loaded"])
        gauge("ollama_model_vram_bytes", "VRAM used by model in bytes.", _metrics["ollama_model_vram_bytes"], labels=True)
        gauge("ollama_model_size_bytes", "Total model size in bytes.", _metrics["ollama_model_size_bytes"], labels=True)
        gauge("ollama_model_expires_at", "Unix timestamp when model will be unloaded.", _metrics["ollama_model_expires_at"], labels=True)
        gauge("ollama_model_context_length", "Configured context window length in tokens.", _metrics["ollama_model_context_length"], labels=True)
        gauge("ollama_gen_tokens_per_sec", "Latest generation speed in tokens/sec.", _metrics["ollama_gen_tokens_per_sec"], labels=True)
        gauge("ollama_prompt_tokens_per_sec", "Latest prompt processing speed in tokens/sec.", _metrics["ollama_prompt_tokens_per_sec"], labels=True)
        gauge("ollama_last_request_seconds", "Wall time of last request in seconds.", _metrics["ollama_last_request_seconds"], labels=True)
        gauge("ollama_last_prompt_tokens", "Prompt token count from the most recent request.", _metrics["ollama_last_prompt_tokens"], labels=True)

        counter("ollama_request_total", "Total number of completed requests.", _metrics["ollama_request_total"])
        counter("ollama_gen_tokens_total", "Total generated tokens.", _metrics["ollama_gen_tokens_total"])
        counter("ollama_prompt_tokens_total", "Total prompt tokens processed.", _metrics["ollama_prompt_tokens_total"])
        counter("ollama_gen_duration_seconds_total", "Total generation time in seconds.", _metrics["ollama_gen_duration_seconds"])
        counter("ollama_prompt_duration_seconds_total", "Total prompt processing time in seconds.", _metrics["ollama_prompt_duration_seconds"])
        counter("ollama_request_duration_seconds_total", "Total request wall time in seconds.", _metrics["ollama_request_duration_seconds"])

    return "\n".join(lines) + "\n"


# ── HTTP Handler ─────────────────────────────────────────────────────────────

class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metrics":
            body = format_metrics().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok\n")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/push":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            push_request_metrics(
                model=body.get("model", "unknown"),
                eval_count=body.get("eval_count", 0),
                eval_duration_ns=body.get("eval_duration", 0),
                prompt_count=body.get("prompt_eval_count", 0),
                prompt_duration_ns=body.get("prompt_eval_duration", 0),
                total_duration_ns=body.get("total_duration", 0),
            )
            self.send_response(204)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress request logging


# ── Polling Thread ───────────────────────────────────────────────────────────

def poll_loop(ollama_url, interval):
    while True:
        poll_ps(ollama_url)
        time.sleep(interval)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Prometheus exporter for Ollama")
    parser.add_argument("--ollama", default="http://localhost:11434", help="Ollama API URL")
    parser.add_argument("--port", type=int, default=9111, help="Exporter listen port")
    parser.add_argument("--interval", type=int, default=15, help="Poll interval in seconds")
    args = parser.parse_args()

    print(f"ollama-exporter starting")
    print(f"  Ollama: {args.ollama}")
    print(f"  Metrics: http://0.0.0.0:{args.port}/metrics")
    print(f"  Push:    http://0.0.0.0:{args.port}/push")
    print(f"  Poll:    every {args.interval}s")

    poller = threading.Thread(target=poll_loop, args=(args.ollama, args.interval), daemon=True)
    poller.start()

    server = ThreadingHTTPServer(("0.0.0.0", args.port), MetricsHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
