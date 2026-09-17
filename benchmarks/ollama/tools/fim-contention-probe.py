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
PROBE_GAP_MIN/PROBE_GAP_MAX (inter-probe gap bounds, default 0.25/1.75),
REPS via argv[1] (default 8).
Prints a per-condition summary; also writes a JSON summary next to the log if
PROBE_JSON is set to a path.

Overlap attribution (2026-09-17). Every FIM and background request records
t_start / t_first / t_end against one process-wide clock, and the RAW samples
are persisted, not just p50/p95/max. Earlier runs discarded the raw lists and
kept three order statistics at REPS=8, where the p95 is just the maximum.
Inter-probe gaps are randomized so a fixed gap cannot alias with the background
cycle and sample one phase over and over.

*** READ THIS BEFORE QUOTING THE PER-SAMPLE `phase` LABEL. ***

The label is a weak hint, not a mechanism finding. Two known defects:

1. `[t_start, t_first]` on a background request is its CLIENT-OBSERVED TIME TO
   FIRST CHUNK, not its prefill. It also contains queueing, transport, and the
   first token's own generation, and the first chunk is taken as-is without
   checking for token content. The server's own `prompt_eval_duration` is kept
   per request as `prompt_eval_s`; a tighter window is
   `[t_first - prompt_eval_s, t_first]`, which analyze-fim-overlap.py uses.
2. Labelling a whole WAIT WINDOW selects for length. A request slowed for any
   reason has a longer window, so it is likelier to touch some prefill and be
   labelled `prefill` -- which means the fast `decode`-only survivors are fast
   partly by construction. Classifying at the ARRIVAL INSTANT removes that bias
   but ignores load that arrives during the wait. Neither is authoritative.

What IS sound is the BETWEEN-CONDITION comparison. B/C (49-token prompts, long
outputs) generate decode pressure with almost no prefill; F/G/H add ~5.2k-token
prefills. Measured 2026-09-17 at num_batch 512: C sustains MORE aggregate decode
than H (152 vs 72 tok/s) and leaves FIM untouched (p50 0.26 s, max 0.39 s, 0%
over 0.5 s), while H degrades it (p50 0.46 s, max 1.82 s, 30% over 1 s). Large
prefills are what hurt FIM; decode contention is not the driver. Run B/C
alongside F/G/H so that control is present in the results.

Thresholds come from INFO-1091: ~200-500 ms "feels instant", >1 s "feels
broken". `frac_over_0_5s` / `frac_over_1_0s` report against those directly.
(IDEA-1105 previously cited "INFO-1090 p95 <= 0.30 s"; that is a misquote of the
INFO-1091 band and INFO-1090 states no such budget.)

First run (0.32.13, 2026-08-28) is archived in the compendium under
_sources/2026-08/2026-08-28_fim-chat-contention-probe-timmy.md.
"""

import json
import math
import os
import random
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request

# One process-wide clock origin. Every FIM and background timestamp is recorded
# as an offset from this, so a FIM request's wait window can be intersected with
# the background requests that were actually in flight during it. Without a
# shared origin the two sides cannot be correlated at all, which is the gap that
# made the 2026-09 runs unable to separate blocking from compute sharing.
T0 = time.perf_counter()


def now():
    return time.perf_counter() - T0


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
# Inter-probe gap, uniformly sampled from [GAP_MIN, GAP_MAX]. A FIXED gap can
# alias with the background request cycle (~1.1 s per summarize), so a probe can
# systematically land in the same phase every time and the sample says nothing
# about the others. Randomizing spreads arrivals across prefill and decode
# phases, which is what makes the overlap attribution below meaningful.
GAP_MIN = float(os.environ.get("PROBE_GAP_MIN", "0.25"))
GAP_MAX = float(os.environ.get("PROBE_GAP_MAX", "1.75"))

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
    t_start = now()
    r = post("/v1/completions", body)
    t_first = None
    text = []
    for line in r:
        if line.startswith(b"data: ") and b"[DONE]" not in line:
            if t_first is None:
                t_first = now()
            choices = json.loads(line[6:]).get("choices") or [{}]
            text.append(choices[0].get("text", ""))
    t_end = now()
    return {
        "t_start": t_start,
        "t_first": t_first,
        "t_end": t_end,
        "ttft": None if t_first is None else t_first - t_start,
        "total": t_end - t_start,
        "text": "".join(text),
    }


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
        t_start = now()
        r = post(path, body)
        t_first = None
        text = []
        for line in r:
            d = json.loads(line)
            if d.get("error"):
                raise RuntimeError(f"{path} stream error: {d['error']}")
            if t_first is None:
                # First streamed token: the server has finished this request's
                # prefill. [t_start, t_first] is therefore its PREFILL window and
                # [t_first, t_end] its DECODE window — the split a FIM sample is
                # attributed against.
                t_first = now()
            chunk = d.get(field) or ""
            text.append(chunk.get("content", "") if isinstance(chunk, dict) else chunk)
            if d.get("done"):
                t_end = now()
                if self.sample is None:
                    self.sample = "".join(text)
                self.runs.append(
                    {
                        "t_start": t_start,
                        "t_first": t_first,
                        "t_end": t_end,
                        "ttft": t_first - t_start,
                        "eval_count": d.get("eval_count"),
                        "prompt_tokens": d.get("prompt_eval_count"),
                        "eval_tps": d.get("eval_count", 0)
                        / (d.get("eval_duration", 1) / 1e9),
                        "prompt_eval_s": d.get("prompt_eval_duration", 0) / 1e9,
                        "wall": t_end - t_start,
                    }
                )


def q(xs, p):
    """Nearest-rank percentile: the smallest value at or above rank ceil(p*n).

    At small n this is a very wide-error-bar estimate of the population
    percentile -- at n=8 it simply returns the maximum, which is correct and
    also nearly uninformative. Raw samples are persisted alongside it so any
    percentile can be recomputed, and so the distribution can be inspected
    rather than trusted through three order statistics.
    """
    xs = sorted(xs)
    return xs[min(len(xs) - 1, max(0, math.ceil(p * len(xs)) - 1))]


def overlap(a0, a1, b0, b1):
    """Seconds of intersection between intervals [a0,a1] and [b0,b1]."""
    return max(0.0, min(a1, b1) - max(a0, b0))


def attribute(fim, bg_runs):
    """Classify one FIM request's wait against concurrent background work.

    The wait window is [t_start, t_first] -- from issuing the request to its
    first token. Each background request contributes a prefill window
    [t_start, t_first] and a decode window [t_first, t_end]. Intersecting them
    answers the question the earlier runs could not: was this FIM request slow
    while a summarize prefill was running, while only decode was running, or
    with nothing else in flight?
    """
    w0, w1 = fim["t_start"], fim["t_first"]
    if w1 is None:
        return None
    span = max(1e-9, w1 - w0)
    prefill_s = decode_s = 0.0
    n_prefill = n_decode = 0
    for r in bg_runs:
        p = overlap(w0, w1, r["t_start"], r["t_first"])
        d = overlap(w0, w1, r["t_first"], r["t_end"])
        prefill_s += p
        decode_s += d
        n_prefill += p > 0
        n_decode += d > 0
    if n_prefill:
        phase = "prefill"
    elif n_decode:
        phase = "decode"
    else:
        phase = "idle"
    return {
        "phase": phase,
        "prefill_overlap_s": prefill_s,
        "decode_overlap_s": decode_s,
        # Fraction of the wait spent with at least one summarize prefill in
        # flight. Near 1.0 across slow samples points at prompt-scheduling
        # interference; near 0 with a slow wait points elsewhere.
        "prefill_frac": min(1.0, prefill_s / span),
        "n_bg_prefill": n_prefill,
        "n_bg_decode": n_decode,
    }


def run_condition(label, n_bg, kind="decode"):
    bgs = [BgGen(kind) for _ in range(n_bg)]
    for b in bgs:
        b.start()
    if n_bg:
        time.sleep(3.0)
    samples, fim_sample = [], None
    for _ in range(REPS):
        s = fim_probe()
        if fim_sample is None:
            fim_sample = s.pop("text")
        else:
            s.pop("text")
        samples.append(s)
        time.sleep(random.uniform(GAP_MIN, GAP_MAX))
    for b in bgs:
        b.stop = True
    for b in bgs:
        b.join()
    ttfts = [s["ttft"] for s in samples]
    totals = [s["total"] for s in samples]
    # Attribute every FIM sample to what the runner was doing during its wait.
    bg_runs = [r for b in bgs for r in b.runs]
    for s in samples:
        s["overlap"] = attribute(s, bg_runs)
    by_phase = {}
    for s in samples:
        ph = (s["overlap"] or {}).get("phase", "unknown")
        by_phase.setdefault(ph, []).append(s["ttft"])
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
        "reps": REPS,
        "fim_ttft_p50": statistics.median(ttfts),
        "fim_ttft_p90": q(ttfts, 0.90),
        "fim_ttft_p95": q(ttfts, 0.95),
        "fim_ttft_p99": q(ttfts, 0.99),
        "fim_ttft_max": max(ttfts),
        "fim_full_p50": statistics.median(totals),
        "fim_sample": fim_sample,
        # Fraction of probes over the INFO-1091 perceptual thresholds:
        # ~200-500 ms "feels instant", >1 s "feels broken".
        "frac_over_0_5s": sum(t > 0.5 for t in ttfts) / len(ttfts),
        "frac_over_1_0s": sum(t > 1.0 for t in ttfts) / len(ttfts),
        # RAW per-request samples: t_start/t_first/t_end offsets from T0, plus
        # the overlap attribution. This is the whole point of the rerun -- every
        # percentile above is recomputable from here, and the distribution can
        # be inspected instead of inferred.
        "fim_samples": samples,
        "ttft_by_phase": {
            ph: {"n": len(v), "p50": statistics.median(v), "max": max(v)}
            for ph, v in sorted(by_phase.items())
        },
        "bg": [],
    }
    print(
        f"{label:18s} FIM ttft p50={row['fim_ttft_p50']:.2f}s p90={row['fim_ttft_p90']:.2f}s "
        f"p95={row['fim_ttft_p95']:.2f}s max={row['fim_ttft_max']:.2f}s "
        f"| >0.5s {row['frac_over_0_5s']:.0%} >1s {row['frac_over_1_0s']:.0%} "
        f"| full-64tok p50={row['fim_full_p50']:.2f}s"
        + ("" if row["valid"] else "  INVALID: background load not sustained")
    )
    for ph, st in row["ttft_by_phase"].items():
        print(
            f"    overlap {ph:8s} n={st['n']:3d} ttft p50={st['p50']:.2f}s max={st['max']:.2f}s"
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
            # Raw per-request windows, same clock origin as the FIM samples.
            # Required to recompute or audit the overlap attribution.
            "runs_raw": rs,
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
