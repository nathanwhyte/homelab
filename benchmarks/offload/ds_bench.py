"""Speed benchmark for an oMLX model with expert offload: decode and prefill, repeated, in randomized order.

For each resident fraction (fractions in shuffled order): apply settings through the admin API
and fail unless the response echoes them, load, confirm the model is loaded, then run every
request kind `--repeats` times in shuffled order. Each request records time to first streamed
token (request start to first content or reasoning chunk), the first-to-last chunk stream rate,
oMLX's own server-side TTFT and prompt/generation tok/s, completion and prompt tokens, reasoning
and content characters, end-to-end time, and swap and memory-pressure snapshots before and after.
A load counts only if the server log shows the offload adapter wrapped `--expect-layers` layers
with no skips. Prefill kinds are end-to-end long-prompt requests (one output token); use
server_prompt_tok_s for prompt-processing speed. One JSON line per event goes to stdout.
"""

import argparse
import json
import os
import random
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.environ.get("OMLX_BASE") or "http://127.0.0.1:8000"
DECODE_PROMPT = (
    "You are reviewing a Python service. Write a function `merge_intervals(intervals)` that merges "
    "overlapping closed intervals given as (start, end) tuples, returns them sorted, and handles empty "
    "input, single intervals, nested intervals and touching endpoints. Then write five pytest tests that "
    "cover those cases, and briefly explain the time and space complexity. Keep the answer under 250 words."
)
WORDS = [
    "the",
    "scheduler",
    "routes",
    "each",
    "request",
    "through",
    "the",
    "expert",
    "cache",
    "while",
    "the",
    "page",
    "cache",
    "serves",
    "repeated",
    "reads",
    "and",
    "the",
    "router",
    "selects",
    "six",
    "experts",
    "per",
    "token",
    "across",
    "forty",
    "three",
    "layers",
    "of",
    "the",
    "model",
]


def http(method, path, body=None, timeout=3600):
    req = urllib.request.Request(
        BASE + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:600]


def engine_memory(pid):
    """The engine's phys_footprint (its own memory) and RSS (which includes mmapped file pages)."""
    if not pid:
        return {}
    fp = subprocess.run(
        ["footprint", "-p", str(pid)], capture_output=True, text=True, check=False
    ).stdout
    m = re.search(r"Footprint: ([\d.]+) (KB|MB|GB)", fp)
    scale = {"KB": 1 / 1024**2, "MB": 1 / 1024, "GB": 1}
    rss = subprocess.run(
        ["ps", "-o", "rss=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    return {
        "engine_footprint_gib": round(float(m.group(1)) * scale[m.group(2)], 2)
        if m
        else None,
        "engine_rss_gib": round(int(rss) / 1024**2, 2) if rss else None,
    }


def memory_snapshot(pid=None):
    vm = subprocess.run(["vm_stat"], capture_output=True, text=True, check=False).stdout
    swap = subprocess.run(
        ["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True, check=False
    ).stdout
    mp = subprocess.run(
        ["memory_pressure"], capture_output=True, text=True, check=False
    ).stdout

    def page(name):
        m = re.search(rf"{name}:\s+(\d+)", vm)
        return int(m.group(1)) if m else None

    used = re.search(r"used = ([\d.]+)M", swap)
    free = re.search(r"free percentage: (\d+)%", mp)
    size = re.search(r"page size of (\d+)", vm)
    psize = int(size.group(1)) if size else 16384

    def gib(name):
        n = page(name)
        return round(n * psize / 1024**3, 2) if n is not None else None

    return {
        "swapins": page("Swapins"),
        "pageouts": page("Pageouts"),
        "swap_used_mb": float(used.group(1)) if used else None,
        "mem_free_pct": int(free.group(1)) if free else None,
        "free_gib": gib("Pages free"),
        "file_backed_gib": gib("File-backed pages"),
        "wired_gib": gib("Pages wired down"),
        "compressed_gib": gib("Pages occupied by compressor"),
        **engine_memory(pid),
    }


def filler(n_words, seed):
    rng = random.Random(seed)
    body = " ".join(rng.choice(WORDS) for _ in range(n_words))
    return f"Nonce {seed}. Summarize the following text in one word.\n\n{body}"


def stream_request(model, prompt, max_tokens, chat_kwargs):
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": True,
        "stream_options": {"include_usage": True},
        "chat_template_kwargs": chat_kwargs,
    }
    req = urllib.request.Request(
        BASE + "/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    t0 = time.monotonic()
    first = last = None
    content = reasoning = 0
    usage = None
    timings = {}
    finish = None
    with urllib.request.urlopen(req, timeout=3600) as resp:
        for raw in resp:
            line = raw.decode().strip()
            if not line.startswith("data:") or line == "data: [DONE]":
                continue
            ev = json.loads(line[5:])
            if ev.get("usage"):
                usage = ev["usage"]
            if ev.get("timings"):  # llama-server's own timing block
                timings = ev["timings"]
            for ch in ev.get("choices", []):
                finish = ch.get("finish_reason") or finish
                d = ch.get("delta", {})
                c = d.get("content") or ""
                r = d.get("reasoning_content") or d.get("reasoning") or ""
                if c or r:
                    now = time.monotonic()
                    first = first or now
                    last = now
                    content += len(c)
                    reasoning += len(r)
    usage = usage or {}
    gen = usage.get("completion_tokens")
    return {
        # Client-side: request start to first nonempty chunk (includes HTTP and queueing).
        "client_ttft_s": round(first - t0, 3) if first else None,
        # Client-side: all generated tokens (reasoning included) over the first-to-last chunk interval.
        "stream_tok_s": round((gen - 1) / (last - first), 2)
        if gen and first and last > first
        else None,
        # Server-side timing: oMLX's usage extension (api/openai_models.py Usage), or
        # llama-server's `timings` block (prompt_ms, prompt_per_second, predicted_per_second).
        "server_ttft_s": usage.get("time_to_first_token")
        or (round(timings["prompt_ms"] / 1000, 3) if "prompt_ms" in timings else None),
        "server_prompt_tok_s": usage.get("prompt_tokens_per_second")
        or timings.get("prompt_per_second"),
        "server_gen_tok_s": usage.get("generation_tokens_per_second")
        or timings.get("predicted_per_second"),
        "server_prompt_eval_s": usage.get("prompt_eval_duration")
        or (round(timings["prompt_ms"] / 1000, 3) if "prompt_ms" in timings else None),
        "completion_tokens": gen,
        "prompt_tokens": usage.get("prompt_tokens"),
        "content_chars": content,
        "reasoning_chars": reasoning,
        "finish_reason": finish,
        "total_s": round(time.monotonic() - t0, 3),
    }


def wrap_report(log_path, offset):
    """Wrapped-layer count and skip lines the offload adapter logged after `offset`."""
    with open(log_path, errors="replace") as fh:
        fh.seek(offset)
        text = fh.read()
    counts = re.findall(r"moe expert offload: wrapped (\d+) layers", text)
    skipped = re.findall(r"moe expert offload: skipping (\S+) \((.*?)\)", text)
    return (int(counts[-1]) if counts else 0), skipped


def emit(rec):
    print(json.dumps(rec), flush=True)


def run_trials(a, rng, kinds, chat_kwargs, label):
    trials = [k for k in kinds for _ in range(a.repeats)]
    rng.shuffle(trials)
    for i, kind in enumerate(trials):
        prompt, n = kinds[kind]
        if prompt is None:
            prompt, max_tokens = filler(n, rng.randrange(10**9)), 1
        else:
            max_tokens = n
        before = memory_snapshot(a.engine_pid)
        try:
            res = stream_request(a.model, prompt, max_tokens, chat_kwargs)
        except Exception as e:  # noqa: BLE001 - record and continue
            res = {"error": str(e)[:300]}
        after = memory_snapshot(a.engine_pid)
        emit(
            {
                "fraction": label,
                "event": "request",
                "kind": kind,
                "seq": i,
                **res,
                "mem_before": before,
                "mem_after": after,
            }
        )


def main():
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument(
        "--fractions", help="comma-separated resident fractions (oMLX admin mode)"
    )
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument(
        "--server-log",
        help="oMLX serve log, checked for the wrap summary",
    )
    ap.add_argument(
        "--expect-layers",
        type=int,
        help="MoE layers that must be wrapped",
    )
    ap.add_argument("--base", default=BASE, help="server base URL ($OMLX_BASE)")
    ap.add_argument(
        "--no-admin",
        metavar="LABEL",
        help="skip oMLX admin calls (e.g. llama-server) and record results under LABEL",
    )
    ap.add_argument("--engine-pid", type=int, help="server PID for footprint/RSS")
    ap.add_argument("--seed", type=int, default=20261009)
    ap.add_argument(
        "--extra", default="{}", help="JSON model settings applied with every fraction"
    )
    ap.add_argument("--chat-kwargs", default='{"enable_thinking": false}')
    a = ap.parse_args()
    BASE = a.base
    rng = random.Random(a.seed)
    extra = json.loads(a.extra)
    chat_kwargs = json.loads(a.chat_kwargs)
    kinds = {
        "decode": (DECODE_PROMPT, 256),
        "prefill_3k": (None, 3000),
        "prefill_12k": (None, 12000),
    }
    if a.no_admin:
        emit(
            {
                "fraction": a.no_admin,
                "event": "start",
                "mem": memory_snapshot(a.engine_pid),
            }
        )
        run_trials(a, rng, kinds, chat_kwargs, a.no_admin)
        return
    if not (a.fractions and a.server_log and a.expect_layers):
        ap.error(
            "--fractions, --server-log and --expect-layers are required without --no-admin"
        )
    fractions = [float(x) for x in a.fractions.split(",")]
    rng.shuffle(fractions)
    for f in fractions:
        http("POST", f"/admin/api/models/{a.model}/unload", {})
        time.sleep(5)
        settings = {
            **extra,
            "moe_expert_offload_enabled": True,
            "moe_expert_offload_resident_fraction": f,
        }
        st, r = http("PUT", f"/admin/api/models/{a.model}/settings", settings)
        applied = r.get("settings", {}) if isinstance(r, dict) else {}
        mismatch = {
            k: (v, applied.get(k)) for k, v in settings.items() if applied.get(k) != v
        }
        if st != 200 or mismatch:
            emit(
                {
                    "fraction": f,
                    "event": "settings_failed",
                    "status": st,
                    "mismatch": mismatch,
                }
            )
            continue
        log_offset = Path(a.server_log).stat().st_size
        t = time.monotonic()
        st, r = http("POST", f"/admin/api/models/{a.model}/load", {})
        load_s = round(time.monotonic() - t, 1)
        wrapped, skipped = wrap_report(a.server_log, log_offset)
        list_st, models = http("GET", "/admin/api/models")
        row = next(
            (
                m
                for m in (models.get("models", []) if isinstance(models, dict) else [])
                if m.get("id") == a.model
            ),
            {},
        )
        emit(
            {
                "fraction": f,
                "event": "load",
                "status": st,
                "list_status": list_st,
                "load_s": load_s,
                "wrapped_layers": wrapped,
                "skipped": skipped,
                "loaded": row.get("loaded"),
                "actual_size": row.get("actual_size"),
                "applied": {k: applied.get(k) for k in settings},
                "detail": r if st != 200 else None,
            }
        )
        if st != 200 or not row.get("loaded"):
            continue
        if wrapped != a.expect_layers or skipped:
            emit({"fraction": f, "event": "offload_incomplete"})
            http("POST", f"/admin/api/models/{a.model}/unload", {})
            continue
        run_trials(a, rng, kinds, chat_kwargs, f)
    http("POST", f"/admin/api/models/{a.model}/unload", {})


if __name__ == "__main__":
    main()
