#!/usr/bin/env python3
"""Is a COLD prefill of the same prompt even reproducible? (BUG-1077 control)

BUG-1077 measured that a prefix-cache hit can return different text than a cold
prefill of the same prompt, at temperature 0 / top_k 1 / fixed seed, on the MLX
runner but not on llama.cpp. The natural reading is "the cache restores wrong
state". That reading has never been controlled.

Every divergence measured so far compares a cache hit against ONE cold run. If
cold prefills of the same prompt are not reproducible either, then nothing
observed is cache-specific and there is no cache defect to report -- only an
engine that is not deterministic at temperature 0. `prefill-cache-stability.py`
cannot answer this: it repeats the CACHED path and holds the cold baseline
fixed, which is the opposite control.

So this tool repeats the COLD path. One prompt, fixed for the whole run, and
the model is unloaded before every request so each prefill is genuinely cold:

  every cold run identical  -> cold IS reproducible, so a hit that differs from
                               cold is cache-specific. A real cache defect.
  cold runs differ          -> the engine is not reproducible at all, and the
                               cache-vs-cold divergence is a symptom of that,
                               not evidence about the cache.

Unloading between requests is what makes the run cold, and it is also the thing
most likely to go wrong silently: if an unload does not take, the next request
is a cache hit that looks like a fast cold run, and identical text would then
prove nothing. Every iteration therefore records its prefill duration and the
run is marked INVALID if any repeat was suspiciously fast (--min-prefill).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import aiohttp

# Reuse the corpus loader and payload shape from the sibling probes so the three
# tools cannot drift apart on sampling options -- results are only comparable if
# temperature, top_k, seed and num_predict are byte-identical across them.
_SIB = Path(__file__).with_name("prefill-cancel-resume.py")
_spec = importlib.util.spec_from_file_location("prefill_cancel_resume", _SIB)
_pcr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_pcr)

load_prompt = _pcr.load_prompt
_payload = _pcr._payload


async def unload(session, url, model) -> None:
    """Evict the model, dropping its weights and its prefix cache with them."""
    async with session.post(
        f"{url}/api/generate", json={"model": model, "prompt": "", "keep_alive": 0}
    ) as resp:
        await resp.read()


async def resident(session, url) -> list[str]:
    async with session.get(f"{url}/api/ps") as resp:
        data = await resp.json()
    return [m.get("name", "?") for m in data.get("models", [])]


async def one_cold(session, url, model, prompt, num_ctx, num_predict, seed, label):
    """Unload, then run one generate to completion. Returns text and timings."""
    await unload(session, url, model)

    # Unload is asynchronous server-side; proceeding immediately can catch the
    # model still resident and produce a warm run labelled cold.
    for _ in range(60):
        if model not in await resident(session, url):
            break
        await asyncio.sleep(0.5)

    t0 = time.monotonic()
    first_token_at = None
    chunks: list[str] = []
    meta: dict = {}
    async with session.post(
        f"{url}/api/generate",
        json=_payload(model, prompt, num_ctx, num_predict, seed),
    ) as resp:
        resp.raise_for_status()
        async for raw in resp.content:
            if not raw.strip():
                continue
            obj = json.loads(raw)
            piece = obj.get("response", "")
            if piece and first_token_at is None:
                first_token_at = time.monotonic()
            if piece:
                chunks.append(piece)
            if obj.get("done"):
                meta = obj
    text = "".join(chunks)
    return {
        "label": label,
        "text": text,
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
        "chars": len(text),
        # Includes model load as well as prefill, since the model was evicted.
        # That is the point -- it is the cost of a genuinely cold run.
        "cold_s": (first_token_at - t0) if first_token_at else None,
        "prompt_eval_count": meta.get("prompt_eval_count"),
        "eval_count": meta.get("eval_count"),
    }


def first_diff(a: str, b: str) -> int | None:
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return i
    return None if a == b else min(len(a), len(b))


async def main_async(args) -> int:
    corpus, est = load_prompt(Path(args.corpus_dir), args.tier)
    num_ctx = max(args.num_ctx, est + 1024)
    # ONE salt for the whole run: every repeat must be the SAME prompt, or
    # differing outputs would be explained by differing inputs.
    salt = f"# bench cold-repeat nonce {os.urandom(8).hex()}\n"
    prompt = salt + corpus

    print(f"tier={args.tier} (~{est} tok) num_ctx={num_ctx} repeats={args.repeats}")
    timeout = aiohttp.ClientTimeout(total=args.timeout)
    rows = []
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(f"{args.url}/api/version") as r:
            server_version = (await r.json()).get("version")
        print(f"server version: {server_version}")

        for i in range(args.repeats):
            row = await one_cold(
                session,
                args.url,
                args.model,
                prompt,
                num_ctx,
                args.num_predict,
                args.seed,
                f"cold{i}",
            )
            print(f"  cold{i}     {row['cold_s']:.2f}s sha={row['sha256'][:12]}")
            rows.append(row)

    hashes = {r["sha256"] for r in rows}
    too_fast = [r["label"] for r in rows if (r["cold_s"] or 0) < args.min_prefill]

    if too_fast:
        verdict = (
            f"INVALID — these repeats were too fast to be cold "
            f"(<{args.min_prefill}s), the unload did not take: {', '.join(too_fast)}"
        )
    elif len(hashes) == 1:
        verdict = (
            "COLD IS REPRODUCIBLE — every cold prefill produced identical text, "
            "so a cache hit that differs from cold is cache-specific"
        )
    else:
        verdict = (
            f"COLD IS NOT REPRODUCIBLE — {len(hashes)} distinct outputs across "
            f"{len(rows)} cold runs; the engine is non-deterministic and "
            "cache-vs-cold divergence is not by itself evidence about the cache"
        )

    result = {
        "server_version": server_version,
        "model": args.model,
        "tier": args.tier,
        "num_ctx": num_ctx,
        "seed": args.seed,
        "num_predict": args.num_predict,
        "repeats": args.repeats,
        "distinct_hashes": len(hashes),
        "first_diff_vs_first": [
            first_diff(rows[0]["text"], r["text"]) for r in rows[1:]
        ],
        "verdict": verdict,
        "rows": rows,
    }
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2))
        print(f"wrote {out}")
    print(f"\nVERDICT: {verdict}")
    print(f"  distinct outputs: {len(hashes)} across {len(rows)} cold runs")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default="http://127.0.0.1:11434")
    ap.add_argument("--model", required=True)
    ap.add_argument("--corpus-dir", required=True)
    ap.add_argument("--tier", default="77k")
    ap.add_argument(
        "--repeats", type=int, default=4, help="cold runs of the same prompt"
    )
    ap.add_argument(
        "--num-ctx", type=int, default=8192, help="floor; raised to fit the tier"
    )
    ap.add_argument("--num-predict", type=int, default=64)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--timeout", type=float, default=3600)
    ap.add_argument(
        "--min-prefill",
        type=float,
        default=20.0,
        help="a repeat faster than this was not cold",
    )
    ap.add_argument("--output")
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
