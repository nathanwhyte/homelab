#!/usr/bin/env python3
"""FIM-vs-background-load contention probe against ONE resident Ollama runner.

Measures inline-FIM TTFT while the same runner serves background /api/generate
load, to answer "can a long-running agent share the FIM runner?" (IDEA-1090).

Conditions:
  A idle              FIM probes alone
  B 1-bg-decode       one looping short-prompt / long-output generation (2 in flight = NUM_PARALLEL=2)
  C 2-bg-decode       two such loops (3 in flight > NUM_PARALLEL=2 -> FIM queues)
  D 1-bg-8k-prefill   one looping ~9.8k-token-prompt / 200-token generation (prefill-heavy, agent-shaped)
  E 2-bg-8k-prefill   two such loops

FIM probe: /v1/completions, salted 2 KiB Lua prefix + fixed suffix, max_tokens 64,
temperature 0, streamed; TTFT = first SSE data chunk. Prompts are salted per
request so the prefix cache never replays them (INFO-1145 / INFO-1090 gotcha).

Env: OLLAMA_HOST (default http://192.168.1.19:11434), FIM_MODEL
(default deepseek-coder-v2:fim), REPS via argv[1] (default 8).
Prints a per-condition summary; also writes a JSON summary next to the log if
PROBE_JSON is set to a path.

First run (0.32.13, 2026-08-28) is archived in the compendium under
_sources/2026-08/2026-08-28_fim-chat-contention-probe-timmy.md.
"""

import json
import os
import statistics
import sys
import threading
import time
import urllib.request

HOST = os.environ.get("OLLAMA_HOST", "http://192.168.1.19:11434").rstrip("/")
MODEL = os.environ.get("FIM_MODEL", "deepseek-coder-v2:fim")
# Background load model. Defaults to the FIM tag (same-runner contention, the
# original probe). Set to a different tag for the two-runner cohabitation leg
# (IDEA-1090 option B): the FIM runner and the partner runner then share only
# the GPU, not slots.
BG_MODEL = os.environ.get("BG_MODEL", MODEL)
REPS = int(sys.argv[1]) if len(sys.argv) > 1 else 8
PREFIX_CHARS = 2048
BG_TOKENS = 400

CODE = """
local function parse_config(path)
  local f = assert(io.open(path, "r"))
  local data = f:read("*a")
  f:close()
  local cfg = {}
  for line in data:gmatch("[^\\n]+") do
    local k, v = line:match("^(%w+)%s*=%s*(.+)$")
    if k then cfg[k] = v end
  end
  return cfg
end
"""
SUFFIX = "\n  return result\nend\n"
BIG_PROMPT = (CODE * 400)[:24000]  # ~8-10k tokens of prefill per request

SUMMARY = {
    "host": HOST,
    "model": MODEL,
    "bg_model": BG_MODEL,
    "reps": REPS,
    "conditions": {},
}


def post(path, body):
    req = urllib.request.Request(
        HOST + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    return urllib.request.urlopen(req, timeout=300)


def fim_probe():
    salt = f"-- {time.time_ns()}\n"
    prefix = salt + (CODE * (PREFIX_CHARS // len(CODE) + 1))[:PREFIX_CHARS]
    body = {
        "model": MODEL,
        "prompt": prefix,
        "suffix": SUFFIX,
        "max_tokens": 64,
        "temperature": 0,
        "stream": True,
    }
    t0 = time.perf_counter()
    r = post("/v1/completions", body)
    ttft = None
    for line in r:
        if line.startswith(b"data: ") and b"[DONE]" not in line and ttft is None:
            ttft = time.perf_counter() - t0
    return ttft, time.perf_counter() - t0


class BgGen(threading.Thread):
    """Loops generate requests until stop is set; records per-request stats."""

    def __init__(self, big):
        super().__init__(daemon=True)
        self.big = big
        self.stop = False
        self.runs = []

    def run(self):
        while not self.stop:
            if self.big:
                prompt = (
                    f"-- {time.time_ns()}\n"
                    + BIG_PROMPT
                    + "\n-- Summarize the module above:\n"
                )
                npred = 200
            else:
                prompt = f"-- {time.time_ns()}\n-- A long, well-commented Lua module implementing an LRU cache with tests.\n"
                npred = BG_TOKENS
            options = {"num_predict": npred, "temperature": 0.7}
            # A loaded runner is keyed by num_ctx. Tags with a baked num_ctx
            # (deepseek-coder-v2:fim) reuse the resident runner; a partner tag
            # without one would be RELOADED at OLLAMA_CONTEXT_LENGTH (131072 on
            # timmy) on its first request — a 5–15 s stall that also evicts the
            # FIM runner. Pin it via BG_NUM_CTX for the cohabitation leg.
            if os.environ.get("BG_NUM_CTX"):
                options["num_ctx"] = int(os.environ["BG_NUM_CTX"])
            body = {
                "model": BG_MODEL,
                "prompt": prompt,
                "stream": True,
                "options": options,
                "keep_alive": "15m",
            }
            t0 = time.perf_counter()
            r = post("/api/generate", body)
            first = None
            for line in r:
                d = json.loads(line)
                if first is None:
                    first = time.perf_counter()
                if d.get("done"):
                    self.runs.append(
                        {
                            "ttft": first - t0,
                            "eval_count": d.get("eval_count"),
                            "prompt_tokens": d.get("prompt_eval_count"),
                            "eval_tps": d.get("eval_count", 0)
                            / (d.get("eval_duration", 1) / 1e9),
                            "prompt_eval_s": d.get("prompt_eval_duration", 0) / 1e9,
                            "wall": time.perf_counter() - t0,
                        }
                    )


def q(xs, p):
    return sorted(xs)[min(len(xs) - 1, int(p * len(xs)))]


def run_condition(label, n_bg, big=False):
    bgs = [BgGen(big) for _ in range(n_bg)]
    for b in bgs:
        b.start()
    if n_bg:
        time.sleep(3.0)
    ttfts, totals = [], []
    for _ in range(REPS):
        ttft, total = fim_probe()
        ttfts.append(ttft)
        totals.append(total)
        time.sleep(0.5)
    for b in bgs:
        b.stop = True
    for b in bgs:
        b.join()
    row = {
        "fim_ttft_p50": statistics.median(ttfts),
        "fim_ttft_p95": q(ttfts, 0.95),
        "fim_ttft_max": max(ttfts),
        "fim_full_p50": statistics.median(totals),
        "bg": [],
    }
    print(
        f"{label:18s} FIM ttft p50={row['fim_ttft_p50']:.2f}s p95={row['fim_ttft_p95']:.2f}s "
        f"max={row['fim_ttft_max']:.2f}s | full-64tok p50={row['fim_full_p50']:.2f}s max={max(totals):.2f}s"
    )
    for i, b in enumerate(bgs):
        rs = b.runs
        if not rs:
            print(f"    bg{i}: no completed runs")
            continue
        s = {
            "runs": len(rs),
            "ttft_p50": statistics.median(r["ttft"] for r in rs),
            "prefill_p50": statistics.median(r["prompt_eval_s"] for r in rs),
            "prompt_tokens": rs[0]["prompt_tokens"],
            "decode_p50": statistics.median(r["eval_tps"] for r in rs),
            "wall_p50": statistics.median(r["wall"] for r in rs),
        }
        row["bg"].append(s)
        print(
            f"    bg{i}: {s['runs']} runs | ttft p50={s['ttft_p50']:.2f}s prefill p50={s['prefill_p50']:.2f}s "
            f"({s['prompt_tokens']} tok) decode p50={s['decode_p50']:.1f} tok/s wall p50={s['wall_p50']:.1f}s"
        )
    SUMMARY["conditions"][label] = row


def main():
    ver = json.loads(urllib.request.urlopen(HOST + "/api/version", timeout=10).read())[
        "version"
    ]
    SUMMARY["ollama_version"] = ver
    print(f"host={HOST} fim={MODEL} bg={BG_MODEL} ollama={ver} reps={REPS}")
    fim_probe()  # warm
    run_condition("A idle", 0)
    run_condition("B 1-bg-decode", 1)
    run_condition("C 2-bg-decode", 2)
    run_condition("D 1-bg-8k-prefill", 1, big=True)
    run_condition("E 2-bg-8k-prefill", 2, big=True)
    ps = json.loads(urllib.request.urlopen(HOST + "/api/ps", timeout=10).read())[
        "models"
    ]
    SUMMARY["loaded_after"] = [(m["name"], m.get("size_vram")) for m in ps]
    print("\nloaded after:", SUMMARY["loaded_after"])
    if os.environ.get("PROBE_JSON"):
        with open(os.environ["PROBE_JSON"], "w") as f:
            json.dump(SUMMARY, f, indent=2)


if __name__ == "__main__":
    main()
