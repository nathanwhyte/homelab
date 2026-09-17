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
  F 1-bg-vlm          one looping OpenViking-shaped summarize request (/api/chat,
                      system + ~4k-token markdown document -> abstract)
  G 2-bg-vlm          two such loops (3 in flight > NUM_PARALLEL=2 -> FIM queues)
  H 3-bg-vlm          three such loops (the 3 VLM + 1 FIM budget at NUM_PARALLEL=4)

F/G answer "can one resident model serve FIM and the OV VLM role at once?"
(IDEA-1105). Two deepseek-coder-v2 16B-lite tags cannot co-reside on the 16 GB
RX 9070 XT (8.2 GiB weights each), so the dual-use leg points FIM_MODEL and
BG_MODEL at the same instruct tag (deepseek-coder-v2:instruct), whose template
carries the FIM suffix branch.

FIM probe: /v1/completions, salted 2 KiB Lua prefix + fixed suffix, max_tokens 64,
temperature 0, streamed; TTFT = first SSE data chunk. Prompts are salted per
request so the prefix cache never replays them (INFO-1145 / INFO-1090 gotcha).
The first FIM completion and first background output of each condition are kept
as samples, so a quality regression shows up beside the latency numbers.

Env: OLLAMA_HOST (default http://192.168.1.19:11434), FIM_MODEL
(default deepseek-coder-v2:fim), BG_MODEL (default FIM_MODEL), BG_NUM_CTX,
CONDITIONS (comma-separated letters, default A,B,C,D,E), VLM_DOC (markdown file
to summarize; default a built-in ~4k-token document), VLM_TOKENS (default 256),
REPS via argv[1] (default 8).
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
import urllib.error
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

VLM_TOKENS = int(os.environ.get("VLM_TOKENS", "256"))
# Mirrors the OV L0/L1 abstract task: a system instruction plus one document.
VLM_SYSTEM = (
    "You summarize documents for a knowledge base. Write a concise abstract "
    "(3-5 sentences) stating what the document covers, its key decisions, and "
    "its current status. Output only the abstract."
)
DOC_SECTION = """## Section {n}: ollama serving posture on the RX 9070 XT

The host daemon runs as a systemd unit with a 16 GiB memory ceiling. The FIM
runner is pinned with keep_alive -1 and a baked num_ctx of 16384, which keeps
the q8_0 KV cache at 4.6 GiB across two slots. Loading a tag without a baked
context inherits the 131072 server default, spills KV to host memory, and gets
the daemon OOM-killed. Decision: every tag served on timmy bakes num_ctx.

| Setting | Value | Reason |
| --- | --- | --- |
| OLLAMA_NUM_PARALLEL | 2 | edit prediction is 1-2 requests per editor |
| OLLAMA_KV_CACHE_TYPE | q8_0 | near-lossless, halves KV versus f16 |
| OLLAMA_MAX_LOADED_MODELS | 1 | one pinned runner fills the card |

Status: open follow-up to reclaim VRAM by lowering num_ctx to 8192.

"""
if os.environ.get("VLM_DOC"):
    with open(os.environ["VLM_DOC"]) as f:
        VLM_DOC = f.read()
else:
    VLM_DOC = "# Timmy inference notes\n\n" + "".join(
        DOC_SECTION.format(n=n) for n in range(1, 21)
    )  # ~4k tokens

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
    text = []
    for line in r:
        if line.startswith(b"data: ") and b"[DONE]" not in line:
            if ttft is None:
                ttft = time.perf_counter() - t0
            choices = json.loads(line[6:]).get("choices") or [{}]
            text.append(choices[0].get("text", ""))
    return ttft, time.perf_counter() - t0, "".join(text)


class BgGen(threading.Thread):
    """Loops background requests until stop is set; records per-request stats.

    kind: "decode" (short prompt, long output), "prefill" (~9.8k-token prompt),
    or "vlm" (OV-shaped summarize over /api/chat).

    A failed request (HTTP error, streamed {"error": ...}, bad JSON) stops the
    worker and is kept in `error`; run_condition marks the condition invalid
    rather than reporting FIM latency measured without the requested load.
    """

    def __init__(self, kind):
        super().__init__(daemon=True)
        self.kind = kind
        self.stop = False
        self.runs = []
        self.sample = None
        self.error = None

    def run(self):
        try:
            self.loop()
        # HTTPError/URLError, socket timeouts (OSError), bad JSON (ValueError),
        # and streamed {"error": ...} (RuntimeError): recorded, surfaced by
        # run_condition.
        except (urllib.error.URLError, OSError, ValueError, RuntimeError) as exc:
            detail = (
                exc.read().decode(errors="replace")[:200]
                if hasattr(exc, "read")
                else ""
            )
            self.error = f"{type(exc).__name__}: {exc} {detail}".strip()

    def loop(self):
        while not self.stop:
            if self.kind == "vlm":
                self.run_vlm()
                continue
            if self.kind == "prefill":
                prompt = (
                    f"-- {time.time_ns()}\n"
                    + BIG_PROMPT
                    + "\n-- Summarize the module above:\n"
                )
                npred = 200
            else:
                prompt = f"-- {time.time_ns()}\n-- A long, well-commented Lua module implementing an LRU cache with tests.\n"
                npred = BG_TOKENS
            body = {
                "model": BG_MODEL,
                "prompt": prompt,
                "stream": True,
                "options": self.options(npred, 0.7),
                "keep_alive": "15m",
            }
            self.stream("/api/generate", body, "response")

    def options(self, npred, temperature):
        options = {"num_predict": npred, "temperature": temperature}
        # A loaded runner is keyed by num_ctx. Tags with a baked num_ctx
        # (deepseek-coder-v2:fim) reuse the resident runner; a partner tag
        # without one would be RELOADED at OLLAMA_CONTEXT_LENGTH (131072 on
        # timmy) on its first request — a 5–15 s stall that also evicts the
        # FIM runner. Pin it via BG_NUM_CTX for the cohabitation leg.
        if os.environ.get("BG_NUM_CTX"):
            options["num_ctx"] = int(os.environ["BG_NUM_CTX"])
        return options

    def run_vlm(self):
        body = {
            "model": BG_MODEL,
            "messages": [
                {"role": "system", "content": VLM_SYSTEM},
                {
                    "role": "user",
                    "content": f"<!-- {time.time_ns()} -->\n{VLM_DOC}",
                },
            ],
            "stream": True,
            "options": self.options(VLM_TOKENS, 0.2),
            "keep_alive": "15m",
        }
        self.stream("/api/chat", body, "message")

    def stream(self, path, body, field):
        t0 = time.perf_counter()
        r = post(path, body)
        first = None
        text = []
        for line in r:
            d = json.loads(line)
            if d.get("error"):
                raise RuntimeError(f"{path} stream error: {d['error']}")
            if first is None:
                first = time.perf_counter()
            chunk = d.get(field) or ""
            text.append(chunk.get("content", "") if isinstance(chunk, dict) else chunk)
            if d.get("done"):
                if self.sample is None:
                    self.sample = "".join(text)
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


def run_condition(label, n_bg, kind="decode"):
    bgs = [BgGen(kind) for _ in range(n_bg)]
    for b in bgs:
        b.start()
    if n_bg:
        time.sleep(3.0)
    ttfts, totals, fim_sample = [], [], None
    for _ in range(REPS):
        ttft, total, text = fim_probe()
        ttfts.append(ttft)
        totals.append(total)
        if fim_sample is None:
            fim_sample = text
        time.sleep(0.5)
    for b in bgs:
        b.stop = True
    for b in bgs:
        b.join()
    # The FIM numbers only mean "under load" if every requested background
    # worker stayed healthy and completed at least one request.
    invalid = [
        f"bg{i}: {b.error or 'no completed runs'}"
        for i, b in enumerate(bgs)
        if b.error or not b.runs
    ]
    row = {
        "valid": not invalid,
        "invalid_reasons": invalid,
        "fim_ttft_p50": statistics.median(ttfts),
        "fim_ttft_p95": q(ttfts, 0.95),
        "fim_ttft_max": max(ttfts),
        "fim_full_p50": statistics.median(totals),
        "fim_sample": fim_sample,
        "bg": [],
    }
    print(
        f"{label:18s} FIM ttft p50={row['fim_ttft_p50']:.2f}s p95={row['fim_ttft_p95']:.2f}s "
        f"max={row['fim_ttft_max']:.2f}s | full-64tok p50={row['fim_full_p50']:.2f}s max={max(totals):.2f}s"
        + ("" if row["valid"] else "  INVALID: background load not sustained")
    )
    for i, b in enumerate(bgs):
        rs = b.runs
        if not rs:
            row["bg"].append({"runs": 0, "error": b.error})
            print(f"    bg{i}: no completed runs{f' ({b.error})' if b.error else ''}")
            continue
        s = {
            "runs": len(rs),
            "ttft_p50": statistics.median(r["ttft"] for r in rs),
            "prefill_p50": statistics.median(r["prompt_eval_s"] for r in rs),
            "prompt_tokens": rs[0]["prompt_tokens"],
            "decode_p50": statistics.median(r["eval_tps"] for r in rs),
            "wall_p50": statistics.median(r["wall"] for r in rs),
            "sample": b.sample,
            "error": b.error,
        }
        row["bg"].append(s)
        print(
            f"    bg{i}: {s['runs']} runs | ttft p50={s['ttft_p50']:.2f}s prefill p50={s['prefill_p50']:.2f}s "
            f"({s['prompt_tokens']} tok) decode p50={s['decode_p50']:.1f} tok/s wall p50={s['wall_p50']:.1f}s"
        )
    print(f"    fim sample: {fim_sample!r}")
    SUMMARY["conditions"][label] = row


CONDITIONS = {
    "A": ("A idle", 0, "decode"),
    "B": ("B 1-bg-decode", 1, "decode"),
    "C": ("C 2-bg-decode", 2, "decode"),
    "D": ("D 1-bg-8k-prefill", 1, "prefill"),
    "E": ("E 2-bg-8k-prefill", 2, "prefill"),
    "F": ("F 1-bg-vlm", 1, "vlm"),
    "G": ("G 2-bg-vlm", 2, "vlm"),
    "H": ("H 3-bg-vlm", 3, "vlm"),
}


def main():
    ver = json.loads(urllib.request.urlopen(HOST + "/api/version", timeout=10).read())[
        "version"
    ]
    SUMMARY["ollama_version"] = ver
    print(f"host={HOST} fim={MODEL} bg={BG_MODEL} ollama={ver} reps={REPS}")
    selected = os.environ.get("CONDITIONS", "A,B,C,D,E").upper().split(",")
    SUMMARY["conditions_run"] = selected
    fim_probe()  # warm
    for key in selected:
        run_condition(*CONDITIONS[key.strip()])
    ps = json.loads(urllib.request.urlopen(HOST + "/api/ps", timeout=10).read())[
        "models"
    ]
    SUMMARY["loaded_after"] = [(m["name"], m.get("size_vram")) for m in ps]
    print("\nloaded after:", SUMMARY["loaded_after"])
    invalid = [k for k, v in SUMMARY["conditions"].items() if not v["valid"]]
    SUMMARY["valid"] = not invalid
    if os.environ.get("PROBE_JSON"):
        with open(os.environ["PROBE_JSON"], "w") as f:
            json.dump(SUMMARY, f, indent=2)
    if invalid:
        # Results are still written for diagnosis, but the run fails.
        print(f"INVALID conditions: {', '.join(invalid)}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
