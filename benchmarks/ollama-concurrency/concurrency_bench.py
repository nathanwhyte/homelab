#!/usr/bin/env python3
"""Ollama concurrency benchmark: same-model parallelism and multi-model cohabitation.

Successor to llama/tools/ollama-concurrency-benchmark.py (INFO-047 lineage),
adapted for pop (M5 Max, 64 GB unified memory, Ollama.app with MLX engine).

Differences from the old script:
- Streams responses, so TTFT includes queue wait under contention (the old
  non-streaming script derived TTFT from load+prefill ns, which hides
  scheduler queueing entirely).
- Adds a multi-model mode: solo baseline per model, then concurrent fire at
  both models (requires OLLAMA_MAX_LOADED_MODELS >= 2 on the server).
- Prompts get a unique per-request preamble so llama.cpp/MLX prompt-prefix
  caching cannot collapse concurrent requests into cache hits.

Usage:
    uv run --with aiohttp python concurrency_bench.py same-model \
        --model qwen3.6:35b-mlx --levels 1 2 4 --repeats 3
    uv run --with aiohttp python concurrency_bench.py multi-model \
        --model qwen3.6:35b-mlx --model qwen2.5-coder:fim-1.5b --repeats 3
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

try:
    import aiohttp
except ImportError as exc:
    raise SystemExit(
        "aiohttp is required. Run: uv run --with aiohttp python concurrency_bench.py ..."
    ) from exc

DEFAULT_URL = "http://localhost:11434"

PROMPTS = [
    "Write a Python function that validates an email address with a regex. Include a docstring and three doctest cases.",
    "Explain the trade-offs between REST and gRPC APIs in three short paragraphs aimed at a backend engineer.",
    "Design a minimal Prometheus exporter for a fictional job queue. List the metrics, their types, and their labels.",
    "Refactor this idea into a class-based state machine in Python: a door can be open, closed, or locked; invalid transitions raise.",
    "Summarize the key differences between symmetric and asymmetric encryption for a junior developer, with one example each.",
    "Write a bash script that rotates log files older than 7 days in /var/log/myapp, keeping the 5 newest, with safety checks.",
    "Explain how a bloom filter works and when a hash set is the better choice. Keep it under 250 words.",
    "Write a SQL query and index recommendation for finding the top 10 customers by 30-day rolling order volume.",
]


@dataclass
class RequestResult:
    model: str
    slot: int
    wall_s: float
    ttft_s: float  # time to first streamed token, includes queue wait
    gen_tps: float  # eval_count / eval_duration from Ollama final chunk
    tokens: int
    prompt_tokens: int
    prompt_eval_s: float
    eval_s: float
    load_s: float
    started_at: float  # monotonic offset from level start
    error: str | None = None


@dataclass
class LevelResult:
    label: str
    concurrency: int
    repeats: int
    results: list[RequestResult] = field(default_factory=list)
    repeat_walls: list[float] = field(default_factory=list)
    repeat_tokens: list[int] = field(default_factory=list)
    ps_after: list[dict] = field(default_factory=list)

    def summary(self) -> dict:
        ok = [r for r in self.results if not r.error]
        errors = [r.error for r in self.results if r.error]

        def stats(vals: list[float]) -> dict:
            if not vals:
                return {}
            return {
                "median": round(statistics.median(vals), 3),
                "mean": round(statistics.mean(vals), 3),
                "min": round(min(vals), 3),
                "max": round(max(vals), 3),
            }

        agg_tps = [
            t / w for t, w in zip(self.repeat_tokens, self.repeat_walls) if w > 0
        ]
        return {
            "label": self.label,
            "concurrency": self.concurrency,
            "requests_ok": len(ok),
            "errors": errors,
            "ttft_s": stats([r.ttft_s for r in ok]),
            "gen_tps_per_req": stats([r.gen_tps for r in ok]),
            "wall_s": stats([r.wall_s for r in ok]),
            "aggregate_tps": stats(agg_tps),
            "prompt_tokens_mean": round(
                statistics.mean([r.prompt_tokens for r in ok]), 1
            )
            if ok
            else 0,
            "tokens_mean": round(statistics.mean([r.tokens for r in ok]), 1)
            if ok
            else 0,
            "ps_after": self.ps_after[-1] if self.ps_after else None,
        }


async def stream_request(
    session: aiohttp.ClientSession,
    url: str,
    model: str,
    prompt: str,
    slot: int,
    num_ctx: int,
    num_predict: int,
    level_t0: float,
) -> RequestResult:
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": True,
        "options": {
            "num_ctx": num_ctx,
            "num_predict": num_predict,
            "temperature": 0.2,
        },
    }
    t0 = time.monotonic()
    ttft = 0.0
    final: dict = {}
    try:
        async with session.post(
            f"{url}/api/generate",
            json=payload,
            timeout=aiohttp.ClientTimeout(total=600),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"HTTP {resp.status}: {body[:200]}")
            async for raw_line in resp.content:
                line = raw_line.strip()
                if not line:
                    continue
                chunk = json.loads(line)
                if ttft == 0.0 and (chunk.get("response") or chunk.get("thinking")):
                    ttft = time.monotonic() - t0
                if chunk.get("done"):
                    final = chunk
    except Exception as exc:
        return RequestResult(
            model=model,
            slot=slot,
            wall_s=time.monotonic() - t0,
            ttft_s=0.0,
            gen_tps=0.0,
            tokens=0,
            prompt_tokens=0,
            prompt_eval_s=0.0,
            eval_s=0.0,
            load_s=0.0,
            started_at=t0 - level_t0,
            error=str(exc),
        )
    wall = time.monotonic() - t0
    eval_count = final.get("eval_count", 0)
    eval_ns = final.get("eval_duration", 0)
    return RequestResult(
        model=model,
        slot=slot,
        wall_s=wall,
        ttft_s=ttft,
        gen_tps=(eval_count / (eval_ns / 1e9)) if eval_ns else 0.0,
        tokens=eval_count,
        prompt_tokens=final.get("prompt_eval_count", 0),
        prompt_eval_s=final.get("prompt_eval_duration", 0) / 1e9,
        eval_s=eval_ns / 1e9,
        load_s=final.get("load_duration", 0) / 1e9,
        started_at=t0 - level_t0,
    )


def make_prompt(base_idx: int, uniq: str) -> str:
    # Unique preamble defeats prompt-prefix caching so every concurrent
    # request pays its own prefill.
    base = PROMPTS[base_idx % len(PROMPTS)]
    return f"[request-id {uniq}] Ignore this first line entirely.\n\n{base}"


async def poll_ps(session: aiohttp.ClientSession, url: str) -> list[dict]:
    try:
        async with session.get(f"{url}/api/ps", timeout=10) as resp:
            data = await resp.json()
        return [
            {
                "name": m.get("name"),
                "size_gb": round(m.get("size", 0) / 1024**3, 2),
                "size_vram_gb": round(m.get("size_vram", 0) / 1024**3, 2),
                "context_length": m.get("context_length"),
            }
            for m in data.get("models", [])
        ]
    except Exception:
        return []


async def warmup(session, url, model, num_ctx) -> RequestResult:
    return await stream_request(
        session,
        url,
        model,
        f"Warmup for {model}: reply with the single word 'ready'.",
        slot=-1,
        num_ctx=num_ctx,
        num_predict=16,
        level_t0=time.monotonic(),
    )


async def run_level(
    session: aiohttp.ClientSession,
    url: str,
    jobs: list[tuple[str, str]],  # (model, prompt)
    label: str,
    concurrency: int,
    repeats: int,
    num_ctx: int,
    num_predict: int,
    cooldown: int,
) -> LevelResult:
    level = LevelResult(label=label, concurrency=concurrency, repeats=repeats)
    for rep in range(repeats):
        t0 = time.monotonic()
        tasks = [
            stream_request(
                session,
                url,
                model,
                f"{prompt}\n\n(repeat {rep})",
                slot=i,
                num_ctx=num_ctx,
                num_predict=num_predict,
                level_t0=t0,
            )
            for i, (model, prompt) in enumerate(jobs)
        ]
        results = await asyncio.gather(*tasks)
        wall = time.monotonic() - t0
        level.results.extend(results)
        level.repeat_walls.append(wall)
        level.repeat_tokens.append(sum(r.tokens for r in results if not r.error))
        for r in results:
            if r.error:
                print(
                    f"    ERROR slot={r.slot} model={r.model}: {r.error}",
                    file=sys.stderr,
                )
        if rep < repeats - 1 and cooldown:
            await asyncio.sleep(cooldown)
    level.ps_after = await poll_ps(session, url)
    s = level.summary()
    print(
        f"  [{label}] c={concurrency}: "
        f"TTFT med={s['ttft_s'].get('median', '-')}s max={s['ttft_s'].get('max', '-')}s | "
        f"gen med={s['gen_tps_per_req'].get('median', '-')} tok/s | "
        f"aggregate med={s['aggregate_tps'].get('median', '-')} tok/s | "
        f"wall med={s['wall_s'].get('median', '-')}s"
    )
    return level


async def cmd_same_model(args) -> list[dict]:
    out: list[dict] = []
    async with aiohttp.ClientSession() as session:
        w = await warmup(session, args.url, args.model, args.num_ctx)
        if w.error:
            raise SystemExit(f"Warmup failed for {args.model}: {w.error}")
        print(f"Warmup {args.model}: wall={w.wall_s:.2f}s load={w.load_s:.2f}s")
        for level_n in args.levels:
            jobs = [
                (args.model, make_prompt(i, f"L{level_n}-{i}")) for i in range(level_n)
            ]
            res = await run_level(
                session,
                args.url,
                jobs,
                label=args.model,
                concurrency=level_n,
                repeats=args.repeats,
                num_ctx=args.num_ctx,
                num_predict=args.num_predict,
                cooldown=args.cooldown,
            )
            out.append(res.summary())
            if level_n != args.levels[-1]:
                await asyncio.sleep(args.cooldown)
    return out


async def cmd_multi_model(args) -> list[dict]:
    models = args.model
    if len(models) < 2:
        raise SystemExit("multi-model mode needs at least two --model flags")
    out: list[dict] = []
    async with aiohttp.ClientSession() as session:
        # Load both models first (order matters: big one first).
        for m in models:
            w = await warmup(session, args.url, m, args.num_ctx)
            if w.error:
                raise SystemExit(f"Warmup failed for {m}: {w.error}")
            print(f"Warmup {m}: wall={w.wall_s:.2f}s load={w.load_s:.2f}s")
        loaded = await poll_ps(session, args.url)
        print(f"Loaded after warmup: {json.dumps(loaded)}")
        if len(loaded) < len(models):
            print(
                "WARNING: not all models resident simultaneously — "
                "OLLAMA_MAX_LOADED_MODELS may still be 1. Results will "
                "measure load-thrash, not cohabitation.",
                file=sys.stderr,
            )

        # Solo baselines (1 request at a time per model).
        for m in models:
            jobs = [(m, make_prompt(0, f"solo-{m}"))]
            res = await run_level(
                session,
                args.url,
                jobs,
                label=f"solo:{m}",
                concurrency=1,
                repeats=args.repeats,
                num_ctx=args.num_ctx,
                num_predict=args.num_predict,
                cooldown=args.cooldown,
            )
            out.append(res.summary())
            await asyncio.sleep(args.cooldown)

        # Concurrent: one request per model simultaneously.
        jobs = [(m, make_prompt(i, f"multi-{i}")) for i, m in enumerate(models)]
        res = await run_level(
            session,
            args.url,
            jobs,
            label="concurrent:" + "+".join(models),
            concurrency=len(models),
            repeats=args.repeats,
            num_ctx=args.num_ctx,
            num_predict=args.num_predict,
            cooldown=args.cooldown,
        )
        out.append(res.summary())

        # Concurrent burst: 1 big-model request + N small-model requests
        # (autocomplete-storm shape) if exactly two models given.
        if len(models) == 2 and args.burst > 0:
            big, small = models[0], models[1]
            jobs = [(big, make_prompt(0, "burst-big"))] + [
                (small, make_prompt(i + 1, f"burst-small-{i}"))
                for i in range(args.burst)
            ]
            res = await run_level(
                session,
                args.url,
                jobs,
                label=f"burst:{big}+{args.burst}x{small}",
                concurrency=1 + args.burst,
                repeats=args.repeats,
                num_ctx=args.num_ctx,
                num_predict=args.num_predict,
                cooldown=args.cooldown,
            )
            out.append(res.summary())
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)

    def common(p):
        p.add_argument("--url", default=DEFAULT_URL)
        p.add_argument("--num-ctx", type=int, default=8192)
        p.add_argument("--num-predict", type=int, default=256)
        p.add_argument("--repeats", type=int, default=3)
        p.add_argument("--cooldown", type=int, default=5)
        p.add_argument("--output-json", type=Path, default=None)

    p_same = sub.add_parser("same-model")
    common(p_same)
    p_same.add_argument("--model", required=True)
    p_same.add_argument("--levels", type=int, nargs="+", default=[1, 2, 4])

    p_multi = sub.add_parser("multi-model")
    common(p_multi)
    p_multi.add_argument(
        "--model",
        action="append",
        required=True,
        help="Repeat for each model; first should be the big/resident one",
    )
    p_multi.add_argument(
        "--burst",
        type=int,
        default=3,
        help="Small-model burst size for the autocomplete-storm level (0 disables)",
    )

    args = parser.parse_args()
    if args.mode == "same-model":
        summaries = asyncio.run(cmd_same_model(args))
    else:
        summaries = asyncio.run(cmd_multi_model(args))

    bundle = {
        "mode": args.mode,
        "num_ctx": args.num_ctx,
        "num_predict": args.num_predict,
        "repeats": args.repeats,
        "levels": summaries,
    }
    print(json.dumps(bundle, indent=2))
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(json.dumps(bundle, indent=2))
        print(f"Wrote {args.output_json}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
