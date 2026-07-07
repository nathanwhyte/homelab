#!/usr/bin/env python3
"""Per-prefill-size TTFT/decode breakdown, bucketed by prompt length.

Complements concurrency-bench.py, which only reports pooled percentiles
across a mixed-size prompt list. This tool reuses the same
edit_prediction_zeta prompt ladder (500/2k/8k tokens) but reports TTFT,
prefill tok/s, and decode tok/s per size bucket at concurrency 1 — the
question "does model X have slow prefill, and does it scale with prompt
size" needs the breakdown, not the pooled percentile.

Cache-collision gotcha (found 2026-07-07, benchmarking qwen3.6:35b-mlx):
edit_prediction_workload()'s salts are a pure function of index, so two
runs with the same (count, fim_format) against the same model produce
byte-identical prompts. Ollama/llama.cpp slot caching skips prefill
entirely on an exact-text repeat, so re-running this tool (or
concurrency-bench.py with the same edit_prediction_zeta config) against a
model that already processed the same prompt set reports near-zero
prefill regardless of size. This tool salts each run with a random
per-invocation nonce to guarantee fresh, never-cached prompts.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))
from prompts import _EP_PREFILL_TOKENS, _ep_code_context, _ep_split  # noqa: E402


def build_fresh_prompts(count: int) -> list[str]:
    run_salt = os.urandom(8).hex()
    prompts = []
    for i in range(count):
        salt = f"# benchmark request {i} run {run_salt} nonce {i * 2654435761 % 999999937}\n"
        context = salt + _ep_code_context(_EP_PREFILL_TOKENS[i % len(_EP_PREFILL_TOKENS)])
        prefix, _suffix = _ep_split(context)
        prompts.append(prefix)
    return prompts


async def run_one(session, url, model, prompt, num_ctx, num_predict, temperature):
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "raw": True,
        "options": {"num_ctx": num_ctx, "num_predict": num_predict, "temperature": temperature},
    }
    t0 = time.monotonic()
    async with session.post(f"{url}/api/generate", json=payload, timeout=aiohttp.ClientTimeout(total=300)) as resp:
        data = await resp.json()
    wall = time.monotonic() - t0
    load_ns = data.get("load_duration", 0) or 0
    prefill_ns = data.get("prompt_eval_duration", 0) or 0
    eval_ns = data.get("eval_duration", 0) or 0
    eval_count = data.get("eval_count", 0) or 0
    prompt_count = data.get("prompt_eval_count", 0) or 0
    ttft = (load_ns + prefill_ns) / 1e9
    prefill_s = prefill_ns / 1e9
    return {
        "wall_s": wall,
        "ttft_s": ttft,
        "load_s": load_ns / 1e9,
        "prefill_s": prefill_s,
        "prefill_tok_s": (prompt_count / prefill_s) if prefill_s > 0 else 0.0,
        "decode_tok_s": (eval_count / (eval_ns / 1e9)) if eval_ns else 0.0,
        "prompt_tokens": prompt_count,
        "gen_tokens": eval_count,
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://localhost:11434")
    parser.add_argument("--model", required=True)
    parser.add_argument("--count", type=int, default=27, help="Total requests (cycled across the 3 prefill sizes)")
    parser.add_argument("--num-ctx", type=int, default=16384)
    parser.add_argument("--num-predict", type=int, default=512)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--output", type=Path, default=None, help="Write raw per-request + summary JSON here")
    args = parser.parse_args()

    prompts = build_fresh_prompts(args.count)
    buckets = {size: [] for size in _EP_PREFILL_TOKENS}
    async with aiohttp.ClientSession() as session:
        for i, prompt in enumerate(prompts):
            size = _EP_PREFILL_TOKENS[i % len(_EP_PREFILL_TOKENS)]
            r = await run_one(session, args.url, args.model, prompt, args.num_ctx, args.num_predict, args.temperature)
            buckets[size].append(r)
            print(f"[{i+1}/{args.count}] target={size} actual_prompt_tokens={r['prompt_tokens']} "
                  f"ttft={r['ttft_s']:.3f}s prefill={r['prefill_s']:.3f}s "
                  f"prefill_tok_s={r['prefill_tok_s']:.1f} decode_tok_s={r['decode_tok_s']:.1f}")

    print(f"\n=== Per-prefill-size summary ({args.model}, concurrency=1) ===")
    print(f"{'Target tok':>10} | {'Actual tok (mean)':>18} | {'TTFT p50':>9} | {'TTFT mean':>10} | "
          f"{'Prefill tok/s (mean)':>20} | {'Decode tok/s (mean)':>20}")
    summary = []
    for size in _EP_PREFILL_TOKENS:
        rows = buckets[size]
        ttfts = [r["ttft_s"] for r in rows]
        prefill_tps = [r["prefill_tok_s"] for r in rows]
        decode_tps = [r["decode_tok_s"] for r in rows]
        prompt_toks = [r["prompt_tokens"] for r in rows]
        row = {
            "target_tokens": size,
            "actual_tokens_mean": round(statistics.mean(prompt_toks), 1),
            "ttft_p50": round(statistics.median(ttfts), 3),
            "ttft_mean": round(statistics.mean(ttfts), 3),
            "prefill_tok_s_mean": round(statistics.mean(prefill_tps), 1),
            "decode_tok_s_mean": round(statistics.mean(decode_tps), 1),
        }
        summary.append(row)
        print(f"{row['target_tokens']:>10} | {row['actual_tokens_mean']:>18.0f} | "
              f"{row['ttft_p50']:>9.3f} | {row['ttft_mean']:>10.3f} | "
              f"{row['prefill_tok_s_mean']:>20.1f} | {row['decode_tok_s_mean']:>20.1f}")

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({
            "model": args.model,
            "config": vars(args) | {"output": str(args.output)},
            "raw": {str(size): rows for size, rows in buckets.items()},
            "summary": summary,
        }, indent=2, default=str))
        print(f"\nWrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
