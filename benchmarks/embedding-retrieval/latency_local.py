#!/usr/bin/env python3
"""TASK-1218 same-hardware embedding latency: one endpoint, one request at a time.

Sends the 44-text latency sample (300 / 1,000 / 3,000 / 6,000 / 7,600 target
tokens) to an Ollama /api/embed or OpenAI-compatible /v1/embeddings endpoint and
records wall-clock latency per request. Every repeat gets a fresh random
16-character prefix, so no request can hit a prompt cache.

  first     the first request after start, recorded separately; model-cold for
            Ollama when the model was unloaded beforehand
  sizes     every sample text, --repeats times, sequentially
  ordering  a short text and a long text sent 100 ms apart, in both orders,
            recording each one's latency (short-behind-long vs short-first)

Numbers are only comparable across endpoints measured on the same machine.

  python3 latency_local.py --backend ollama \\
    --url http://127.0.0.1:11434/api/embed \\
    --model embeddinggemma-2:270m-mxfp8-text --num-ctx 8192 \\
    --sample sample.json --out latency-eg2.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import statistics
import string
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

GAP_S = 0.1  # delay between the two requests of an ordering pair


def fresh(text: str, rng: random.Random) -> str:
    return "".join(rng.choices(string.ascii_letters + string.digits, k=16)) + " " + text


def embed(args, text: str) -> dict:
    """One request; returns latency, token count, and any error."""
    if args.backend == "ollama":
        body = {"model": args.model, "input": [text], "truncate": False}
        if args.num_ctx:
            body["options"] = {"num_ctx": args.num_ctx}
    else:
        body = {"model": args.model, "input": [text], "cache_prompt": False}
    req = urllib.request.Request(
        args.url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=300) as response:
            data = json.load(response)
    except urllib.error.HTTPError as error:
        return {
            "ms": (time.monotonic() - started) * 1000,
            "error": f"HTTP {error.code}: {error.read().decode()[:200]}",
        }
    ms = (time.monotonic() - started) * 1000
    if args.backend == "ollama":
        tokens = data.get("prompt_eval_count")
    else:
        tokens = (data.get("usage") or {}).get("prompt_tokens")
    return {"ms": ms, "tokens": tokens}


def pair(args, first: str, second: str) -> tuple[dict, dict]:
    """Send `first`, then `second` GAP_S later, concurrently; return both results."""
    out: dict[str, dict] = {}
    thread = threading.Thread(
        target=lambda: out.__setitem__("first", embed(args, first))
    )
    thread.start()
    time.sleep(GAP_S)
    out["second"] = embed(args, second)
    thread.join()
    return out["first"], out["second"]


def median(values: list[float]) -> float | None:
    return round(statistics.median(values), 1) if values else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--backend", required=True, choices=["ollama", "openai"])
    ap.add_argument("--url", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--num-ctx", type=int, help="Ollama options.num_ctx")
    ap.add_argument("--sample", required=True, type=Path)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--seed", type=int, default=1218)
    ap.add_argument(
        "--long-index",
        type=int,
        help="sample index of the ordering test's long text (default: the "
        "longest); pick one that fits every compared model's window",
    )
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()
    rng = random.Random(args.seed)
    sample = json.loads(args.sample.read_text())

    first = embed(args, fresh("first request", rng))
    print(f"first request: {first['ms']:.0f} ms", flush=True)

    rows = []
    for repeat in range(args.repeats):
        for index, item in enumerate(sample):
            result = embed(args, fresh(item["text"], rng))
            rows.append(
                {"repeat": repeat, "index": index, "target": item["target"], **result}
            )
        print(f"sizes: repeat {repeat + 1}/{args.repeats} done", flush=True)

    short = min(sample, key=lambda s: s["tokens"] if s["target"] == 300 else 1e9)
    if args.long_index is None:
        long = max(sample, key=lambda s: s["tokens"])
    else:
        long = sample[args.long_index]
    ordering = []
    for repeat in range(args.repeats):
        l1, s1 = pair(args, fresh(long["text"], rng), fresh(short["text"], rng))
        s2, l2 = pair(args, fresh(short["text"], rng), fresh(long["text"], rng))
        ordering.append(
            {
                "repeat": repeat,
                "short_behind_long_ms": s1["ms"],
                "long_ms_when_first": l1["ms"],
                "short_first_ms": s2["ms"],
                "long_ms_when_second": l2["ms"],
                "errors": [r["error"] for r in (l1, s1, s2, l2) if "error" in r],
            }
        )

    buckets = {}
    for target in sorted({r["target"] for r in rows}):
        ok = [r for r in rows if r["target"] == target and "error" not in r]
        buckets[str(target)] = {
            "requests": sum(r["target"] == target for r in rows),
            "errors": sum(r["target"] == target and "error" in r for r in rows),
            "median_ms": median([r["ms"] for r in ok]),
            "median_tokens": median([r["tokens"] for r in ok if r.get("tokens")]),
        }
    summary = {
        "backend": args.backend,
        "url": args.url,
        "model": args.model,
        "num_ctx": args.num_ctx,
        "repeats": args.repeats,
        "seed": args.seed,
        "sample": str(args.sample),
        "sample_sha256": hashlib.sha256(args.sample.read_bytes()).hexdigest(),
        "first_request": first,
        "buckets": buckets,
        # Medians use only runs where all four requests succeeded: a long text
        # that overflows fails at once, and the short one never waits.
        "ordering": {
            "short_index": sample.index(short),
            "long_index": sample.index(long),
            "short_target_tokens": short["tokens"],
            "long_target_tokens": long["tokens"],
            "runs_with_errors": sum(bool(o["errors"]) for o in ordering),
            "median_short_behind_long_ms": median(
                [o["short_behind_long_ms"] for o in ordering if not o["errors"]]
            ),
            "median_short_first_ms": median(
                [o["short_first_ms"] for o in ordering if not o["errors"]]
            ),
            "median_long_first_ms": median(
                [o["long_ms_when_first"] for o in ordering if not o["errors"]]
            ),
            "runs": ordering,
        },
        "rows": rows,
    }
    args.out.write_text(json.dumps(summary, indent=2) + "\n")
    for target, b in buckets.items():
        print(
            f"  {target:>5} target: median {b['median_ms']} ms "
            f"({b['median_tokens']} tok, {b['errors']}/{b['requests']} errors)"
        )
    o = summary["ordering"]
    print(
        f"  short behind long {o['median_short_behind_long_ms']} ms, "
        f"short first {o['median_short_first_ms']} ms, long first "
        f"{o['median_long_first_ms']} ms ({o['runs_with_errors']} runs dropped "
        f"for errors) -> {args.out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
