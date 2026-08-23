#!/usr/bin/env python3
"""Cancelled-and-resumed prefill probe (PROJ-1003, TASK-1190).

WHY THIS EXISTS. prefill-size-breakdown.py --mode warm-prefix measures
ordinary prefix-cache behavior: it warms a prefix with a request that runs to
COMPLETION, then sends successful extensions. Nothing is ever interrupted. It
therefore cannot say anything about ollama#17901, which fixes prefix cache
restore points surviving *cancelled and resumed* prefills. Both a fixed and an
unfixed server look identical under that probe.

This probe interrupts a prefill and then resumes it, which is the only shape
that can observe the fix.

THE MEASUREMENT. Each trial uses its own freshly-salted prompt P, and the
model is evicted BEFORE the trial but never during it (an eviction inside the
trial destroys the very cache being measured):

  cancelled arm
    1. P sent, then aborted mid-prefill before any token is emitted. Nothing
       is measured; it exists to leave the server holding a partial prefill.
    2. P sent again. THIS is the measured row. Any reuse comes from a restore
       point that survived the cancellation.
    3. evict, then P sent once more, fully cold — the same-prompt reference
       for both evaluated tokens and output text.

  control arm
    1. P sent to completion. This is the same-prompt cold reference.
    2. P sent again. Reuse here comes from an ordinary completed-prefill
       cache and is the UPPER BOUND: if the control shows no reuse, the
       server has no usable prefix cache at all and the cancelled arm's
       number carries no information.

INTERPRETATION.

  A server that preserves restore points across cancellation should show the
  resumed request evaluating materially fewer tokens than cold — ideally close
  to the control's second request. A server that discards them re-evaluates
  the whole prompt, so resumed ~= cold.

  The headline metric is PREFILL DURATION, not evaluated tokens.

  Measured on pop 2026-08-23 against 0.32.15 (qwen3.8:27b-mlx, 77k tier):
  an uninterrupted repeat of the same prompt collapsed prefill from 117.8s to
  0.082s while prompt_eval_count stayed at 64552 in BOTH requests. This
  engine does not subtract cached tokens from prompt_eval_count — a cache hit
  is visible only as a duration collapse. (The same effect is what produced
  the "absurd 100k+ tok/s" prefill rates noted on 2026-07-07: the count stays
  full while the duration goes to ~0.)

  Token counts are still recorded, as a diagnostic and to catch a prompt that
  changed length unexpectedly, but they cannot detect reuse on this engine.

  Thermal drift (~8% on this machine) is not a threat to this comparison: the
  signal here is a 1400x duration ratio, not a few percent.

CORRECTNESS, NOT ONLY SPEED. A resumed prefill that reuses a STALE restore
point could be fast and wrong. Every trial therefore runs at temperature 0
with a fixed seed and compares the resumed completion text against the cold
completion text. A mismatch is reported as `output_divergence` and fails the
run — a cache that returns different content for the same prompt is a
correctness bug regardless of how fast it is.

FAIL-CLOSED. Every response must be HTTP 200, carry no "error", and report a
nonzero prompt_eval_count. The cancellation must actually land before the
first token, or the trial is discarded rather than silently recorded as a
resume.
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


class BenchError(RuntimeError):
    pass


def _salt(tag: str) -> str:
    return f"# bench {tag} nonce {os.urandom(8).hex()}\n"


def load_prompt(corpus_dir: Path, tier: str) -> tuple[str, int]:
    manifest_path = corpus_dir / "manifest.json"
    if not manifest_path.is_file():
        raise BenchError(
            f"no manifest.json in {corpus_dir} — run build-prefill-corpus.py"
        )
    manifest = json.loads(manifest_path.read_text())
    if tier not in manifest["tiers"]:
        raise BenchError(
            f"tier {tier!r} not in manifest (have: {', '.join(manifest['tiers'])})"
        )
    meta = manifest["tiers"][tier]
    return (corpus_dir / meta["file"]).read_text(), meta["est_tokens"]


def _payload(model, prompt, num_ctx, num_predict, seed):
    return {
        "model": model,
        "prompt": prompt,
        "stream": True,
        "raw": True,
        "options": {
            "num_ctx": num_ctx,
            "num_predict": num_predict,
            "temperature": 0.0,
            "seed": seed,
            "top_k": 1,
        },
    }


async def run_streaming(session, url, model, prompt, num_ctx, num_predict, seed, label):
    """Run to completion, returning metrics plus the generated text."""
    t0 = time.monotonic()
    ttft = None
    text_parts: list[str] = []
    final = None
    async with session.post(
        f"{url}/api/generate",
        json=_payload(model, prompt, num_ctx, num_predict, seed),
        timeout=aiohttp.ClientTimeout(total=1800),
    ) as resp:
        if resp.status != 200:
            body = await resp.text()
            raise BenchError(f"{label}: HTTP {resp.status}: {body[:500]}")
        async for line in resp.content:
            if not line.strip():
                continue
            chunk = json.loads(line)
            if chunk.get("error"):
                raise BenchError(f"{label}: server error: {chunk['error']}")
            if chunk.get("response"):
                if ttft is None:
                    ttft = time.monotonic() - t0
                text_parts.append(chunk["response"])
            if chunk.get("done"):
                final = chunk
    if final is None:
        raise BenchError(f"{label}: stream ended without a done chunk")
    prompt_count = final.get("prompt_eval_count", 0) or 0
    if prompt_count <= 0:
        raise BenchError(
            f"{label}: prompt_eval_count={prompt_count} — refusing to record a zero-metric row"
        )
    prefill_ns = final.get("prompt_eval_duration", 0) or 0
    return {
        "label": label,
        "wall_s": time.monotonic() - t0,
        "ttft_s": ttft if ttft is not None else 0.0,
        "prefill_s": prefill_ns / 1e9,
        "prompt_tokens": prompt_count,
        "gen_tokens": final.get("eval_count", 0) or 0,
        "text": "".join(text_parts),
    }


async def cancel_midprefill(
    session, url, model, prompt, num_ctx, seed, cancel_after_s, label
):
    """Start a generate and abort it before the first token.

    Returns True when the cancellation landed during prefill (no token seen).
    A trial whose cancellation arrived too late is not a cancelled-prefill
    test at all, so the caller discards it rather than recording it.
    """
    saw_token = False
    try:
        async with session.post(
            f"{url}/api/generate",
            json=_payload(model, prompt, num_ctx, 64, seed),
            timeout=aiohttp.ClientTimeout(total=cancel_after_s),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise BenchError(f"{label}: HTTP {resp.status}: {body[:500]}")
            async for line in resp.content:
                if not line.strip():
                    continue
                chunk = json.loads(line)
                if chunk.get("response"):
                    saw_token = True
                    break
    except asyncio.TimeoutError:
        # This is the intended path: the deadline fired while the server was
        # still prefilling, and aiohttp tore the connection down.
        return True
    except aiohttp.ClientError as e:
        raise BenchError(f"{label}: transport error during cancellation: {e}") from e
    return not saw_token


async def one_trial(session, args, base_text, seed, cancelled: bool, idx: int):
    """One trial: cancel-then-resume, or an uninterrupted control.

    STRUCTURE (and why it is not "cold, evict, resume"):

    An earlier revision measured a cold leg, EVICTED, then resumed. That
    guaranteed 0% reuse in every arm — including the control — because
    `ollama stop` drops the model and its whole cache, so there was nothing
    left to reuse and the probe could not observe #17901 even in principle.
    The 2026-08-23 shakeout showed exactly that: reused=0.0% on both arms.

    The eviction therefore happens BEFORE a trial, never inside it:

      cancelled: fresh prompt -> cancel mid-prefill -> resend the same prompt.
                 Any reuse comes from a restore point that survived the
                 cancellation, which is the thing #17901 changes.
      control:   fresh prompt -> send it to completion -> resend it.
                 Reuse here comes from an ordinary completed-prefill cache and
                 is the upper bound: if the control shows no reuse, the server
                 has no usable prefix cache at all and the cancelled arm's
                 number means nothing.

    Each arm gets a SAME-PROMPT cold reference, so reuse and output equality
    are always measured against the identical prompt:

      control:   the first (completed) send is the reference.
      cancelled: after the resumed send, the model is evicted and the same
                 prompt is sent once more, fully cold. A run-level reference
                 would not do here — every trial carries its own salt, so a
                 cross-trial text comparison would differ for legitimate
                 reasons and make the divergence check meaningless.
    """
    tag = "cancelled" if cancelled else "control"
    prompt = _salt(f"cancel-resume {tag} {idx}") + base_text

    # Clean residency state for the trial as a whole. Nothing is evicted once
    # the trial has started.
    await evict(args.url, args.model)

    first = None
    if cancelled:
        landed = await cancel_midprefill(
            session,
            args.url,
            args.model,
            prompt,
            args.num_ctx,
            seed,
            args.cancel_after,
            f"{tag}#{idx} cancel",
        )
        if not landed:
            print(
                f"[{tag}#{idx}] cancellation arrived after the first token — discarding trial",
                file=sys.stderr,
            )
            return None
    else:
        first = await run_streaming(
            session,
            args.url,
            args.model,
            prompt,
            args.num_ctx,
            args.num_predict,
            seed,
            f"{tag}#{idx} first",
        )

    resumed = await run_streaming(
        session,
        args.url,
        args.model,
        prompt,
        args.num_ctx,
        args.num_predict,
        seed,
        f"{tag}#{idx} resumed",
    )

    if first is not None:
        baseline = first
    else:
        # Same-prompt cold reference for the cancelled arm.
        await evict(args.url, args.model)
        baseline = await run_streaming(
            session,
            args.url,
            args.model,
            prompt,
            args.num_ctx,
            args.num_predict,
            seed,
            f"{tag}#{idx} verify-cold",
        )

    cold_tokens = baseline["prompt_tokens"]
    # Reuse is measured in TIME, because prompt_eval_count does not drop on a
    # cache hit on this engine (see the module docstring).
    cold_prefill = baseline["prefill_s"]
    reused_frac = (
        1.0 - (resumed["prefill_s"] / cold_prefill) if cold_prefill > 0 else 0.0
    )
    token_delta = cold_tokens - resumed["prompt_tokens"]
    row = {
        "trial": idx,
        "arm": tag,
        "cold_prompt_tokens": cold_tokens,
        "resumed_prompt_tokens": resumed["prompt_tokens"],
        "token_delta": token_delta,
        "reused_frac": reused_frac,
        "prefill_speedup_x": (
            round(cold_prefill / resumed["prefill_s"], 1)
            if resumed["prefill_s"] > 0
            else None
        ),
        "cold_ttft_s": round(baseline["ttft_s"], 3),
        "resumed_ttft_s": round(resumed["ttft_s"], 3),
        "cold_prefill_s": round(baseline["prefill_s"], 3),
        "resumed_prefill_s": round(resumed["prefill_s"], 3),
        # Same prompt, same seed, temperature 0: any text difference means the
        # resumed answer changed, which is the failure mode worth catching —
        # a fast reuse of a STALE restore point.
        "output_divergence": baseline["text"] != resumed["text"],
        "divergence_reference": "trial-first" if first else "trial-verify-cold",
    }
    print(
        f"[{tag}#{idx}] prefill {row['cold_prefill_s']:.2f}s -> "
        f"{row['resumed_prefill_s']:.2f}s "
        f"(reused {row['reused_frac']:.1%}, {row['prefill_speedup_x']}x) "
        f"eval_tok {row['cold_prompt_tokens']}->{row['resumed_prompt_tokens']} "
        f"divergence={row['output_divergence']}"
    )
    return row


async def evict(url, model):
    """Drop the model so the next request starts from a known residency state."""
    proc = await asyncio.create_subprocess_exec(
        "ollama",
        "stop",
        model,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    await asyncio.sleep(2)


def summarize(rows):
    out = {}
    for arm in ("cancelled", "control"):
        arm_rows = [r for r in rows if r["arm"] == arm]
        if not arm_rows:
            continue
        out[arm] = {
            "n": len(arm_rows),
            "reused_frac_mean": round(
                statistics.mean(r["reused_frac"] for r in arm_rows), 4
            ),
            "cold_prefill_s_mean": round(
                statistics.mean(r["cold_prefill_s"] for r in arm_rows), 3
            ),
            "resumed_prefill_s_mean": round(
                statistics.mean(r["resumed_prefill_s"] for r in arm_rows), 3
            ),
            "resumed_eval_mean": round(
                statistics.mean(r["resumed_prompt_tokens"] for r in arm_rows), 1
            ),
            "cold_eval_mean": round(
                statistics.mean(r["cold_prompt_tokens"] for r in arm_rows), 1
            ),
            "resumed_ttft_mean": round(
                statistics.mean(r["resumed_ttft_s"] for r in arm_rows), 3
            ),
            "divergences": sum(1 for r in arm_rows if r["output_divergence"]),
        }
    return out


async def amain(args):
    base_text, est = load_prompt(args.corpus_dir, args.tier)
    need = est + args.num_predict + 1024
    args.num_ctx = max(args.num_ctx, need)
    print(
        f"prompt tier={args.tier} (~{est} tok), num_ctx={args.num_ctx}, "
        f"cancel_after={args.cancel_after}s, trials={args.trials}"
    )

    rows = []
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{args.url}/api/version", timeout=aiohttp.ClientTimeout(total=30)
        ) as resp:
            server = await resp.json()
        print(f"server version: {server.get('version')}")

        for i in range(args.trials):
            for cancelled in (True, False):
                row = await one_trial(session, args, base_text, args.seed, cancelled, i)
                if row is not None:
                    rows.append(row)
                await evict(args.url, args.model)

    if not any(r["arm"] == "cancelled" for r in rows):
        raise BenchError(
            "no cancelled trial landed during prefill — every cancellation arrived after the "
            f"first token. Lower --cancel-after (currently {args.cancel_after}s) or use a larger "
            "tier. Refusing to report a cancel-resume result that never cancelled a prefill."
        )

    summary = summarize(rows)
    print("\n=== cancel/resume summary ===")
    for arm, s in summary.items():
        print(
            f"{arm:>10}: n={s['n']} prefill {s['cold_prefill_s_mean']:.2f}s -> "
            f"{s['resumed_prefill_s_mean']:.3f}s "
            f"reused={s['reused_frac_mean']:.1%} "
            f"divergences={s['divergences']}"
        )

    diverged = sum(s["divergences"] for s in summary.values())
    result = {
        "probe": "prefill-cancel-resume",
        "tier": args.tier,
        "model": args.model,
        "server": server,
        "config": {
            "trials": args.trials,
            "cancel_after_s": args.cancel_after,
            "num_ctx": args.num_ctx,
            "num_predict": args.num_predict,
            "seed": args.seed,
        },
        "raw": rows,
        "summary": summary,
        "output_divergences": diverged,
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2))
        print(f"\nWrote {args.output}")

    if diverged:
        raise BenchError(
            f"{diverged} trial(s) produced different text after a resumed prefill than when cold. "
            "A resumed prefill that changes the answer is a correctness failure, not a speedup."
        )
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--url", default="http://localhost:11434")
    ap.add_argument("--model", required=True)
    ap.add_argument("--corpus-dir", type=Path, required=True)
    ap.add_argument(
        "--tier", default="77k", help="corpus tier to use as the long prompt"
    )
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument(
        "--cancel-after",
        type=float,
        default=1.5,
        help="seconds before aborting the prefill; must be under the cold TTFT",
    )
    ap.add_argument(
        "--num-ctx", type=int, default=16384, help="floor; raised to fit the tier"
    )
    ap.add_argument("--num-predict", type=int, default=64)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--output", type=Path, default=None)
    args = ap.parse_args()
    try:
        return asyncio.run(amain(args))
    except BenchError as e:
        print(f"\nFATAL: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
