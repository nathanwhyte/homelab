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
              Relies on prompt_eval_count excluding cached prefix tokens.
              Cache hits are decided by MEASURED accounting: the warmup
              request supplies prefix_tokens, a small salted probe supplies
              suffix_tokens, and a row is a hit only when
              (prefix+suffix) - evaluated covers >= CACHE_HIT_FRACTION of the
              measured prefix. The run FAILS only if NO tier hit anywhere;
              a single missed tier is reported and skipped, not fatal.

              This replaced a `evaluated < 0.5 * manifest_estimate` heuristic
              that was unsound for the 5k tier (a perfect hit there still
              evaluates ~57% of the prompt) and aborted the whole run on the
              first tier every time.

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


# A warm row counts as a prefix-cache hit only when the server skipped at least
# this fraction of the MEASURED prefix. Not a fraction of the whole prompt, and
# not a fraction of a manifest estimate — see the accounting note in run_corpus.
CACHE_HIT_FRACTION = 0.9


class BenchError(RuntimeError):
    pass


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
            # MEASURED cached-token accounting (replaces the old
            # `prompt_tokens < 0.5 * est_total` heuristic, which was unsound).
            #
            # That heuristic compared evaluated tokens against half of a
            # MANIFEST ESTIMATE of the whole prompt. For the 5k tier the
            # estimates are prefix=4968 / suffix=6629, so a PERFECT prefix hit
            # still evaluates the 6629-token suffix — 57% of the total — and
            # was classified as a miss. Since the tiers are walked smallest
            # first and the abort lived inside this loop, the probe aborted on
            # tier one on every run and never reached 27k/77k.
            #
            # Instead, measure both halves against the server's own
            # prompt_eval_count and compare like with like:
            #   prefix_tokens  <- warmup request (tier text alone)
            #   suffix_tokens  <- salted append payload alone
            #   cached_tokens  <- (prefix + suffix) - evaluated
            # A hit means we actually skipped ~the whole prefix, not that we
            # happened to land under an arbitrary fraction of a guess.
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
            # Guard the guard: if the warmup itself came back mostly cached,
            # prefix_tokens is an undercount and every later hit ratio is
            # inflated. Only the first warmup of a given tier text is cold.
            if prefix_tokens < 0.5 * tier_est:
                raise BenchError(
                    f"{name}: warmup evaluated only {prefix_tokens} tokens against an estimated "
                    f"{tier_est} — the prefix was already cached, so measured prefix_tokens would "
                    "understate the real prefix. Restart the server or evict the model first."
                )
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
            suffix_tokens = suffix_probe["prompt_tokens"]
            expected_full = prefix_tokens + suffix_tokens
            print(
                f"[{name}] prefix warmed: measured prefix={prefix_tokens} tok, "
                f"suffix={suffix_tokens} tok, expected full={expected_full} tok, num_ctx={num_ctx}"
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
                r["cached_tokens"] = expected_full - r["prompt_tokens"]
                r["cached_frac_of_prefix"] = (
                    r["cached_tokens"] / prefix_tokens if prefix_tokens else 0.0
                )
                r["prefix_cached"] = r["cached_frac_of_prefix"] >= CACHE_HIT_FRACTION
                rows.append(r)
                print(
                    f"[{name} r{i}] evaluated={r['prompt_tokens']} of {expected_full} "
                    f"cached={r['cached_tokens']} ({r['cached_frac_of_prefix']:.1%} of prefix) "
                    f"prefix_cached={r['prefix_cached']} "
                    f"suffix_prefill_tok_s={r['prefill_tok_s']:.1f} ttft={r['ttft_s']:.3f}s"
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
