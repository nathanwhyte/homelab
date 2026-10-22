#!/usr/bin/env python3
"""Per-prefill-size TTFT/decode breakdown, bucketed by prompt size tier.

Complements concurrency-bench.py, which only reports pooled percentiles
across a mixed-size prompt list. Two prompt sources:

  synthetic (default, no --corpus-dir): the edit_prediction_zeta ladder
  (500/2k/8k tokens), kept for continuity with the 2026-07-07 qwen3.6
  measurement.

  corpus (--corpus-dir, TASK-1186): real agent payloads — assembled
  system prompts + CLAUDE.md/AGENTS.md tiers (~5k/27k/77k tokens, see
  build-prefill-corpus.py). This is the production prefill profile per
  the OQ-3 scope boundary (Claude Code turn one ~77k prompt tokens,
  prefill 65-85% of wall time).

Corpus modes (--mode):

  cold        salt at the FRONT of the prompt so the slot cache cannot
              shortcut any prefix — measures turn-one context ingestion.
  warm-prefix send the bare tier text once to warm the cache, then
              repeatedly append a freshly-salted file-read payload (resolved
              from the manifest) and measure suffix-only prefill. This is the
              mid-session "agent reads files not in the initial load" case.
              Cache hits are decided by measured PREFILL DURATION, not by
              token count: this engine reports the full prompt_eval_count
              even on a total cache hit (pop, 2026-08-23: 117.8s -> 0.082s
              with the count unchanged). The warmup supplies the cold prefill
              rate, a salted probe supplies the suffix token count, and a row
              is a hit when it skipped at least CACHE_HIT_PREFIX_REUSE of the
              prefix's own prefill time (normalized per tier, because the
              achievable whole-prompt ratio differs by tier: 0.60 on 5k vs
              0.10 on 77k).
              The run FAILS only if NO tier hit anywhere; a single missed
              tier is reported and skipped, not fatal.

              This replaced an `evaluated < 0.5 * manifest_estimate`
              heuristic that was both unsatisfiable on the 5k tier (a perfect
              hit there still evaluates ~57% of the prompt) and blind by
              construction, and that aborted the whole run on tier one.

  NOTE: neither mode exercises a CANCELLED prefill. For ollama#17901-style
  restore-point behavior use prefill-cancel-resume.py, which is the only
  probe here that interrupts a prefill and resumes it.

Cache-collision gotcha (found 2026-07-07, benchmarking qwen3.6:35b-mlx):
byte-identical prompts across runs let Ollama/llama.cpp slot caching skip
prefill entirely, reporting absurd 100k+ tok/s. Cold/synthetic runs salt
every prompt per invocation; warm-prefix salts only the appended suffix —
the prefix cache hit there is the thing being measured, not an artifact.

Fail-closed: every response must be HTTP 200, carry no Ollama "error", and
report a nonzero prompt_eval_count. Any violation aborts the run with a
nonzero exit — a server error must never become a zero-metric success row.
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

CHARS_PER_TOKEN = 4  # must match build-prefill-corpus.py


# A warm row counts as a hit when at least this fraction of the PREFIX's
# prefill time was skipped.
#
# Two earlier attempts failed structurally, both for the same reason — the
# threshold was applied to a quantity whose achievable range depends on the
# tier:
#   1. `evaluated < 0.5 * est_total` (token-based). Best case on 5k is 57% of
#      the prompt, so a perfect hit could never pass.
#   2. `prefill_s <= 0.5 * predicted_cold_s` (duration-based, but whole-prompt).
#      Best case is suffix/(prefix+suffix) = 0.60 on 5k, 0.22 on 27k, 0.10 on
#      77k — so again unreachable on 5k while trivial on 77k.
#
# Normalizing by the prefix's own prefill time makes the metric mean the same
# thing on every tier: "how much of the prefix did we avoid re-computing".
CACHE_HIT_PREFIX_REUSE = 0.5

# Cold prefill rates measured on pop sit at 686-907 tok/s across the tiers. A
# warmup reporting far above that did not prefill cold, so anything derived
# from its rate is meaningless. Deliberately generous: this is a sanity
# ceiling, not a performance assertion.
MAX_PLAUSIBLE_COLD_RATE = 5000


class BenchError(RuntimeError):
    pass


async def evict(model: str) -> None:
    """Drop the model so the next request prefills cold.

    Required, not optional: the warmup must be genuinely cold or the cold
    prefill RATE it measures is garbage, and every prediction derived from it
    is garbage too. This cannot be detected after the fact — prompt_eval_count
    does not drop on a cache hit on this engine, so a "was the warmup cached?"
    check based on token counts can never fire. Guaranteeing coldness by
    eviction is the only reliable route.
    """
    proc = await asyncio.create_subprocess_exec(
        "ollama",
        "stop",
        model,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    await asyncio.sleep(2)


def _salt(tag: str) -> str:
    return f"# bench {tag} nonce {os.urandom(8).hex()}\n"


def build_fresh_prompts(count: int) -> list[str]:
    run_salt = os.urandom(8).hex()
    prompts = []
    for i in range(count):
        salt = f"# benchmark request {i} run {run_salt} nonce {i * 2654435761 % 999999937}\n"
        context = salt + _ep_code_context(
            _EP_PREFILL_TOKENS[i % len(_EP_PREFILL_TOKENS)]
        )
        prefix, _suffix = _ep_split(context)
        prompts.append(prefix)
    return prompts


def load_corpus(corpus_dir: Path) -> tuple[dict[str, str], str, dict]:
    manifest_path = corpus_dir / "manifest.json"
    if not manifest_path.is_file():
        raise BenchError(
            f"no manifest.json in {corpus_dir} — run build-prefill-corpus.py"
        )
    manifest = json.loads(manifest_path.read_text())
    tiers = {}
    for name, meta in manifest["tiers"].items():
        tiers[name] = (corpus_dir / meta["file"]).read_text()
    append_text = (corpus_dir / manifest["append_payload"]["file"]).read_text()
    return tiers, append_text, manifest


async def run_one(
    session, url, model, prompt, num_ctx, num_predict, temperature, label
):
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "raw": True,
        "options": {
            "num_ctx": num_ctx,
            "num_predict": num_predict,
            "temperature": temperature,
        },
    }
    t0 = time.monotonic()
    async with session.post(
        f"{url}/api/generate", json=payload, timeout=aiohttp.ClientTimeout(total=1800)
    ) as resp:
        body = await resp.text()
        if resp.status != 200:
            raise BenchError(
                f"{label}: HTTP {resp.status} from {url}/api/generate: {body[:500]}"
            )
        data = json.loads(body)
    if data.get("error"):
        raise BenchError(f"{label}: server error: {data['error']}")
    wall = time.monotonic() - t0
    load_ns = data.get("load_duration", 0) or 0
    prefill_ns = data.get("prompt_eval_duration", 0) or 0
    eval_ns = data.get("eval_duration", 0) or 0
    eval_count = data.get("eval_count", 0) or 0
    prompt_count = data.get("prompt_eval_count", 0) or 0
    if prompt_count <= 0:
        raise BenchError(
            f"{label}: prompt_eval_count={prompt_count} — refusing to record a zero-metric row"
        )
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


def summarize(name, rows):
    ttfts = [r["ttft_s"] for r in rows]
    return {
        "tier": name,
        "n": len(rows),
        "evaluated_tokens_mean": round(
            statistics.mean(r["prompt_tokens"] for r in rows), 1
        ),
        "ttft_p50": round(statistics.median(ttfts), 3),
        "ttft_mean": round(statistics.mean(ttfts), 3),
        "prefill_tok_s_mean": round(
            statistics.mean(r["prefill_tok_s"] for r in rows), 1
        ),
        "decode_tok_s_mean": round(statistics.mean(r["decode_tok_s"] for r in rows), 1),
    }


def print_summary(model, mode, summary):
    print(f"\n=== Per-tier summary ({model}, mode={mode}, concurrency=1) ===")
    print(
        f"{'Tier':>6} | {'Eval tok (mean)':>15} | {'TTFT p50':>9} | {'TTFT mean':>10} | "
        f"{'Prefill tok/s (mean)':>20} | {'Decode tok/s (mean)':>20}"
    )
    for row in summary:
        print(
            f"{row['tier']:>6} | {row['evaluated_tokens_mean']:>15.0f} | "
            f"{row['ttft_p50']:>9.3f} | {row['ttft_mean']:>10.3f} | "
            f"{row['prefill_tok_s_mean']:>20.1f} | {row['decode_tok_s_mean']:>20.1f}"
        )


async def run_corpus(session, args, server_meta):
    tiers, append_text, manifest = load_corpus(args.corpus_dir)
    append_est = manifest["append_payload"]["est_tokens"]
    buckets: dict[str, list] = {}
    for name, text in tiers.items():
        tier_est = manifest["tiers"][name]["est_tokens"]
        # One num_ctx for warmup and measured requests alike: an options change
        # between them would re-slot the model and drop the cached prefix.
        need = (
            tier_est
            + (append_est if args.mode == "warm-prefix" else 0)
            + args.num_predict
            + 1024
        )
        num_ctx = max(args.num_ctx, need)
        rows = []
        if args.mode == "warm-prefix":
            # MEASURED, DURATION-BASED cache detection.
            #
            # Two premises had to be replaced here. The original code used
            # `prompt_eval_count < 0.5 * manifest_estimate`, which was both
            # (a) unsatisfiable on the 5k tier — a perfect hit still evaluates
            # ~57% of that prompt, and the abort lived inside the tier loop so
            # tier one killed every run — and (b) built on the false premise
            # that prompt_eval_count excludes cached tokens. It does not: on
            # pop 2026-08-23 an uninterrupted repeat collapsed prefill from
            # 117.8s to 0.082s while the count stayed at 64552 both times.
            #
            # So: measure the cold prefill RATE from the warmup, predict how
            # long a full cold prefill of prefix+suffix would take, and call a
            # row a hit when it came in far under that.
            # Suffix probe FIRST, then evict, then the warmup. Running the
            # suffix probe after the warmup risks evicting the very prefix we
            # just warmed (one slot, different prompt), and running the warmup
            # without an eviction lets a previous tier or a previous run leave
            # it warm — which silently inflates the measured cold rate.
            suffix_probe = await run_one(
                session,
                args.url,
                args.model,
                _salt(f"suffix probe {name}") + append_text,
                num_ctx,
                8,
                args.temperature,
                f"{name} suffix probe",
            )
            await evict(args.model)
            warm = await run_one(
                session,
                args.url,
                args.model,
                text,
                num_ctx,
                8,
                args.temperature,
                f"{name} warmup",
            )
            prefix_tokens = warm["prompt_tokens"]
            cold_rate = warm["prefill_tok_s"]
            if cold_rate <= 0:
                raise BenchError(f"{name}: warmup reported a zero prefill rate")
            if cold_rate > MAX_PLAUSIBLE_COLD_RATE:
                raise BenchError(
                    f"{name}: warmup reported {cold_rate:.0f} tok/s, above the plausible "
                    f"cold ceiling of {MAX_PLAUSIBLE_COLD_RATE} tok/s for this machine — the "
                    "warmup itself hit a cache despite the eviction, so its rate cannot be "
                    "used as a cold baseline."
                )
            suffix_tokens = suffix_probe["prompt_tokens"]
            expected_full = prefix_tokens + suffix_tokens
            predicted_cold_s = expected_full / cold_rate
            print(
                f"[{name}] prefix warmed: measured prefix={prefix_tokens} tok "
                f"@ {cold_rate:.0f} tok/s, suffix={suffix_tokens} tok, "
                f"predicted full cold prefill={predicted_cold_s:.2f}s, num_ctx={num_ctx}"
            )
            for i in range(args.repeats):
                prompt = text + "\n" + _salt(f"append {name} {i}") + append_text
                r = await run_one(
                    session,
                    args.url,
                    args.model,
                    prompt,
                    num_ctx,
                    args.num_predict,
                    args.temperature,
                    f"{name} warm r{i}",
                )
                r["measured_prefix_tokens"] = prefix_tokens
                r["measured_suffix_tokens"] = suffix_tokens
                r["expected_full_tokens"] = expected_full
                r["cold_prefill_rate_tok_s"] = round(cold_rate, 1)
                r["predicted_cold_prefill_s"] = round(predicted_cold_s, 3)
                r["prefill_duration_ratio"] = (
                    r["prefill_s"] / predicted_cold_s if predicted_cold_s > 0 else 1.0
                )
                prefix_cold_s = prefix_tokens / cold_rate
                r["prefix_cold_s"] = round(prefix_cold_s, 3)
                r["prefix_reuse_frac"] = (
                    (predicted_cold_s - r["prefill_s"]) / prefix_cold_s
                    if prefix_cold_s > 0
                    else 0.0
                )
                r["prefix_cached"] = r["prefix_reuse_frac"] >= CACHE_HIT_PREFIX_REUSE
                rows.append(r)
                print(
                    f"[{name} r{i}] prefill={r['prefill_s']:.2f}s of predicted "
                    f"{predicted_cold_s:.2f}s — reused {r['prefix_reuse_frac']:.0%} of the "
                    f"{prefix_cold_s:.2f}s prefix, prefix_cached={r['prefix_cached']} "
                    f"(evaluated={r['prompt_tokens']}, ttft={r['ttft_s']:.3f}s)"
                )
            # Record the miss and keep going. The old code raised here, inside
            # the tier loop, which threw away every remaining tier — and with
            # the broken classifier that meant losing the whole run to tier
            # one. Fail-closed now happens once, after all tiers, in
            # run_corpus's caller-visible return.
            if not any(r["prefix_cached"] for r in rows):
                print(
                    f"[{name}] WARNING: no warm row hit the prefix cache — this tier measured "
                    "cold prefill under a warm label and is not comparable.",
                    file=sys.stderr,
                )
        else:
            for i in range(args.repeats):
                prompt = _salt(f"cold {name} {i}") + text
                r = await run_one(
                    session,
                    args.url,
                    args.model,
                    prompt,
                    num_ctx,
                    args.num_predict,
                    args.temperature,
                    f"{name} cold r{i}",
                )
                rows.append(r)
                print(
                    f"[{name} r{i}] prompt_tokens={r['prompt_tokens']} "
                    f"prefill_tok_s={r['prefill_tok_s']:.1f} ttft={r['ttft_s']:.3f}s "
                    f"decode_tok_s={r['decode_tok_s']:.1f}"
                )
        buckets[name] = rows

    if args.mode == "warm-prefix":
        hit_tiers = [
            name
            for name, rows in buckets.items()
            if any(r.get("prefix_cached") for r in rows)
        ]
        if not hit_tiers:
            raise BenchError(
                "every warm-prefix row in every tier evaluated the full prompt — the prefix cache "
                "never hit, so this measured cold prefill under a warm label. Aborting."
            )
        missed = [n for n in buckets if n not in hit_tiers]
        if missed:
            print(
                f"\nNOTE: tiers with no cache hit (not comparable): {', '.join(missed)}",
                file=sys.stderr,
            )

    summary = [summarize(name, rows) for name, rows in buckets.items()]
    print_summary(args.model, args.mode, summary)
    return {
        "mode": args.mode,
        "corpus_manifest": {
            k: {kk: vv for kk, vv in v.items() if kk != "sources"}
            for k, v in manifest["tiers"].items()
        },
        "append_payload_sha256": manifest["append_payload"]["sha256"],
        "server": server_meta,
        "raw": buckets,
        "summary": summary,
    }


async def run_synthetic(session, args, server_meta):
    prompts = build_fresh_prompts(args.count)
    buckets = {size: [] for size in _EP_PREFILL_TOKENS}
    for i, prompt in enumerate(prompts):
        size = _EP_PREFILL_TOKENS[i % len(_EP_PREFILL_TOKENS)]
        r = await run_one(
            session,
            args.url,
            args.model,
            prompt,
            args.num_ctx,
            args.num_predict,
            args.temperature,
            f"synthetic {size} #{i}",
        )
        buckets[size].append(r)
        print(
            f"[{i + 1}/{args.count}] target={size} actual_prompt_tokens={r['prompt_tokens']} "
            f"ttft={r['ttft_s']:.3f}s prefill={r['prefill_s']:.3f}s "
            f"prefill_tok_s={r['prefill_tok_s']:.1f} decode_tok_s={r['decode_tok_s']:.1f}"
        )
    summary = [summarize(str(size), rows) for size, rows in buckets.items()]
    print_summary(args.model, "synthetic", summary)
    return {
        "mode": "synthetic",
        "server": server_meta,
        "raw": {str(size): rows for size, rows in buckets.items()},
        "summary": summary,
    }


async def fetch_server_meta(session, url, model):
    """Endpoint provenance for the result artifact (TASK-1186 reproducibility criterion)."""
    async with session.get(
        f"{url}/api/version", timeout=aiohttp.ClientTimeout(total=30)
    ) as resp:
        if resp.status != 200:
            raise BenchError(
                f"{url}/api/version returned HTTP {resp.status} — endpoint not usable"
            )
        version = (await resp.json()).get("version")
    digest = None
    async with session.get(
        f"{url}/api/tags", timeout=aiohttp.ClientTimeout(total=30)
    ) as resp:
        if resp.status == 200:
            for entry in (await resp.json()).get("models") or []:
                if entry.get("name") == model:
                    digest = entry.get("digest")
                    break
    return {
        "base_url": url,
        "proxy_bypassed": True,
        "ollama_version": version,
        "model_digest": digest,
    }


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--url", default="http://localhost:11434")
    parser.add_argument("--model", required=True)
    parser.add_argument(
        "--count",
        type=int,
        default=27,
        help="Synthetic mode: total requests (cycled across the 3 sizes)",
    )
    parser.add_argument(
        "--corpus-dir",
        type=Path,
        default=None,
        help="Real-payload corpus dir (see build-prefill-corpus.py); replaces the synthetic ladder",
    )
    parser.add_argument(
        "--mode",
        choices=["cold", "warm-prefix"],
        default="cold",
        help="Corpus mode: cold full prefill (front-salted) or warm-prefix incremental",
    )
    parser.add_argument(
        "--repeats", type=int, default=3, help="Corpus mode: measured requests per tier"
    )
    parser.add_argument(
        "--num-ctx",
        type=int,
        default=16384,
        help="Floor; corpus mode raises it per tier to fit the payload",
    )
    parser.add_argument("--num-predict", type=int, default=512)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Write raw per-request + summary JSON here",
    )
    args = parser.parse_args()

    try:
        async with aiohttp.ClientSession() as session:
            server_meta = await fetch_server_meta(session, args.url, args.model)
            if args.corpus_dir:
                result = await run_corpus(session, args, server_meta)
            else:
                result = await run_synthetic(session, args, server_meta)
    except BenchError as e:
        print(f"\nFATAL: {e}", file=sys.stderr)
        return 1

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(
                {
                    "model": args.model,
                    "config": vars(args)
                    | {"output": str(args.output), "corpus_dir": str(args.corpus_dir)},
                    **result,
                },
                indent=2,
                default=str,
            )
        )
        print(f"\nWrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
