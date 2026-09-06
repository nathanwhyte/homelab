#!/usr/bin/env python3
"""Is a diverging prefix-cache hit *deterministically* wrong, or just noisy? (BUG-1077)

BUG-1077 established that a cache-hit request can return different text than the
cold prefill of the same prompt, at temperature 0 / top_k 1 / fixed seed. Two
hypotheses survive, and neither existing arm of `prefill-cancel-resume.py` can
separate them:

  H1  numeric non-determinism — the restored KV state is *equivalent but not
      bit-identical*, and a near-tie at some position flips the argmax.
  H2  a genuine cache defect — the restored KV state is materially wrong, so the
      cache-hit answer is deterministically different.

The discriminator is repetition. Ask the same cached question several times:

  every hit identical to the others, and different from cold  -> H2
  hits differ from each other                                 -> H1
  every hit identical to cold                                 -> no divergence

`prefill-cancel-resume.py` salts each trial with a fresh nonce precisely so each
prefill is cold. This tool does the reverse: ONE salt for the whole run, so
request 1 is cold and every later request should hit the same cached prefix.

Confirming the hit is the point, not an aside. A "hit" that silently ran cold
would make identical text prove nothing, so every repeat records its prefill
duration and the run fails loudly if a repeat was not dramatically faster than
the cold baseline.
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

# Reuse the sibling probe's corpus loader and payload shape rather than
# re-deriving them — the two tools must agree on sampling options exactly or
# their results are not comparable. The filename is hyphenated, so a plain
# import will not do.
_SIB = Path(__file__).with_name("prefill-cancel-resume.py")
_spec = importlib.util.spec_from_file_location("prefill_cancel_resume", _SIB)
_pcr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_pcr)

load_prompt = _pcr.load_prompt
_payload = _pcr._payload


async def one_request(session, url, model, prompt, num_ctx, num_predict, seed, label):
    """Run one generate to completion; return text, prefill seconds and counts."""
    t0 = time.monotonic()
    first_token_at = None
    chunks = []
    meta = {}
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
    prefill_s = (first_token_at - t0) if first_token_at else None
    return {
        "label": label,
        "text": text,
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
        "chars": len(text),
        "prefill_s": prefill_s,
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
    # ONE salt for the whole run — the inverse of the sibling probe.
    salt = f"# bench stability nonce {os.urandom(8).hex()}\n"
    prompt = salt + corpus

    print(f"tier={args.tier} (~{est} tok) num_ctx={num_ctx} repeats={args.repeats}")
    timeout = aiohttp.ClientTimeout(total=args.timeout)
    rows = []
    async with aiohttp.ClientSession(timeout=timeout) as session:
        ver = await session.get(f"{args.url}/api/version")
        server_version = (await ver.json()).get("version")
        print(f"server version: {server_version}")

        cold = await one_request(
            session,
            args.url,
            args.model,
            prompt,
            num_ctx,
            args.num_predict,
            args.seed,
            "cold",
        )
        print(f"  cold      prefill={cold['prefill_s']:.2f}s sha={cold['sha256'][:12]}")
        rows.append(cold)

        for i in range(args.repeats):
            hit = await one_request(
                session,
                args.url,
                args.model,
                prompt,
                num_ctx,
                args.num_predict,
                args.seed,
                f"hit{i}",
            )
            speedup = cold["prefill_s"] / hit["prefill_s"] if hit["prefill_s"] else None
            print(
                f"  hit{i}      prefill={hit['prefill_s']:.3f}s "
                f"({speedup:.0f}x faster) sha={hit['sha256'][:12]}"
            )
            hit["speedup_vs_cold"] = speedup
            rows.append(hit)

    hits = rows[1:]
    # Guard: an unconfirmed hit makes every downstream comparison meaningless.
    not_hits = [
        h["label"] for h in hits if (h.get("speedup_vs_cold") or 0) < args.min_speedup
    ]
    hit_hashes = {h["sha256"] for h in hits}
    hits_agree = len(hit_hashes) == 1
    hits_match_cold = hits_agree and hits[0]["sha256"] == cold["sha256"]

    if not_hits:
        verdict = f"INVALID — these repeats were not cache hits: {', '.join(not_hits)}"
    elif hits_match_cold:
        verdict = "NO DIVERGENCE — every cache hit reproduced the cold text"
    elif hits_agree:
        verdict = "H2 — hits are identical to each other but differ from cold (deterministic, wrong)"
    else:
        verdict = "H1 — hits differ from EACH OTHER (numeric non-determinism)"

    result = {
        "server_version": server_version,
        "model": args.model,
        "tier": args.tier,
        "num_ctx": num_ctx,
        "seed": args.seed,
        "num_predict": args.num_predict,
        "repeats": args.repeats,
        "distinct_hit_hashes": len(hit_hashes),
        "hits_agree": hits_agree,
        "hits_match_cold": hits_match_cold,
        "cold_vs_hit_first_diff": first_diff(cold["text"], hits[0]["text"]),
        "verdict": verdict,
        "rows": rows,
    }
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2))
        print(f"wrote {out}")
    print(f"\nVERDICT: {verdict}")
    print(f"  distinct hit hashes: {len(hit_hashes)} across {len(hits)} hits")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default="http://127.0.0.1:11434")
    ap.add_argument("--model", required=True)
    ap.add_argument("--corpus-dir", required=True)
    ap.add_argument("--tier", default="77k")
    ap.add_argument(
        "--repeats", type=int, default=4, help="cache-hit requests after the cold one"
    )
    ap.add_argument(
        "--num-ctx", type=int, default=8192, help="floor; raised to fit the tier"
    )
    ap.add_argument("--num-predict", type=int, default=64)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--timeout", type=float, default=1800)
    ap.add_argument(
        "--min-speedup",
        type=float,
        default=10.0,
        help="a repeat slower than cold/this is not treated as a cache hit",
    )
    ap.add_argument("--output")
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
