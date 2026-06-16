#!/usr/bin/env python3
"""Ollama concurrency benchmark for GPU cohabitation analysis.

Extends INFO-047 to measure 1..N concurrent client requests against a single
Ollama model. Captures wall time, TTFT, generation tok/s, total throughput,
and (optionally) GPU VRAM. Designed to validate the mem0 + Hermes cohabitation
configuration described in INFO-055.

Run from anywhere with network access to the Ollama endpoint:

    uv run --with aiohttp python llama/tools/ollama-concurrency-benchmark.py

Or inside the cluster:

    kubectl run -it --rm bench --image=python:3.12-slim --restart=Never -- \
        sh -c "pip install aiohttp && python -c '...'"

The script is read-only with respect to cluster state; it only calls Ollama's
/api/generate, /api/ps, and (optionally) Prometheus query endpoints.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import aiohttp
except ImportError as exc:  # pragma: no cover - runtime dependency hint
    raise SystemExit(
        "aiohttp is required. Run: uv run --with aiohttp python "
        "llama/tools/ollama-concurrency-benchmark.py ..."
    ) from exc


DEFAULT_URL = "http://ollama.llama.svc.cluster.local:11434"
DEFAULT_MODEL = "gemma4:12b-it-qat"
DEFAULT_PROM_URL = "http://prom-prometheus.grafana:9090"

SUB_AGENT_PROMPTS = [
    "Write a Python function that validates an email address. Include a docstring and three test cases.",
    "Explain the trade-offs between REST and gRPC APIs in three short paragraphs.",
    "Design a minimal Prometheus exporter for a fictional job queue. List the metrics and their labels.",
    "Refactor this snippet into a class-based state machine: a door can be open, closed, or locked; transitions must be valid.",
    "Summarize the key differences between symmetric and asymmetric encryption for a junior developer.",
]

# Approximates the mem0 ADDITIVE_EXTRACTION_PROMPT shape: a long, fixed system
# prefix followed by a short user payload. The byte-identical prefix exercises
# Ollama/llama.cpp per-slot prompt-prefix caching under contention.
MEM0_STYLE_PROMPT = """You are a memory extraction assistant. Your task is to read the conversation below and emit a single JSON object with a top-level key "memory" containing an array of memory operations. Each operation must have keys: "data" (string), "event" (string), "category" (one of: habits, preferences, goals, relationships, plans, other), and "score" (number 0.0-1.0). Do not output any text outside the JSON object.

Conversation:
User: I've been trying to wake up at 6am to work on my side project before my day job. I prefer dark mode for all my coding tools. My partner and I are planning a trip to Japan in the spring. I want to improve my Python async skills this year, and I usually review my weekly goals on Sunday evenings. I dislike noisy open-plan offices.

Extract memories as JSON:"""


@dataclass
class RequestResult:
    wall_s: float
    ttft_s: float
    gen_tps: float
    tokens: int
    prompt_tokens: int
    load_s: float
    prefill_s: float
    eval_s: float
    error: Optional[str] = None


@dataclass
class LevelResult:
    concurrency: int
    requests: int
    repeats: int
    wall_s: list[float] = field(default_factory=list)
    ttft_s: list[float] = field(default_factory=list)
    gen_tps: list[float] = field(default_factory=list)
    tokens: list[int] = field(default_factory=list)
    prompt_tokens: list[int] = field(default_factory=list)
    repeat_throughput: list[float] = field(default_factory=list)
    vram_gb: Optional[float] = None
    throughput_tok_s: Optional[float] = None

    def to_summary(self) -> dict:
        def stats(name: str, values: list[float]) -> dict:
            if not values:
                return {"mean": 0.0, "median": 0.0, "min": 0.0, "max": 0.0}
            return {
                "mean": round(statistics.mean(values), 3),
                "median": round(statistics.median(values), 3),
                "min": round(min(values), 3),
                "max": round(max(values), 3),
            }

        return {
            "concurrency": self.concurrency,
            "requests": self.requests,
            "repeats": self.repeats,
            "vram_gb": round(self.vram_gb, 3) if self.vram_gb is not None else None,
            "throughput_tok_s": round(self.throughput_tok_s, 2)
            if self.throughput_tok_s
            else None,
            "wall_s": stats("wall_s", self.wall_s),
            "ttft_s": stats("ttft_s", self.ttft_s),
            "gen_tps": stats("gen_tps", self.gen_tps),
            "tokens": {
                "mean": round(statistics.mean(self.tokens), 1) if self.tokens else 0.0,
                "total": sum(self.tokens),
            },
            "prompt_tokens": {
                "mean": round(statistics.mean(self.prompt_tokens), 1)
                if self.prompt_tokens
                else 0.0,
            },
            "repeat_throughput": stats("repeat_throughput", self.repeat_throughput),
        }


async def run_request(
    session: aiohttp.ClientSession,
    url: str,
    model: str,
    prompt: str,
    num_ctx: int,
    num_predict: int,
    temperature: float,
) -> RequestResult:
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "num_ctx": num_ctx,
            "num_predict": num_predict,
            "temperature": temperature,
        },
    }
    t0 = time.monotonic()
    try:
        async with session.post(
            f"{url}/api/generate", json=payload, timeout=300
        ) as resp:
            data = await resp.json()
    except Exception as exc:
        return RequestResult(
            wall_s=time.monotonic() - t0,
            ttft_s=0.0,
            gen_tps=0.0,
            tokens=0,
            prompt_tokens=0,
            load_s=0.0,
            prefill_s=0.0,
            eval_s=0.0,
            error=str(exc),
        )
    wall = time.monotonic() - t0

    load_ns = data.get("load_duration", 0) or 0
    prefill_ns = data.get("prompt_eval_duration", 0) or 0
    eval_ns = data.get("eval_duration", 0) or 0
    eval_count = data.get("eval_count", 0) or 0
    prompt_count = data.get("prompt_eval_count", 0) or 0

    ttft = (load_ns + prefill_ns) / 1e9
    gen_tps = eval_count / (eval_ns / 1e9) if eval_ns else 0.0

    return RequestResult(
        wall_s=wall,
        ttft_s=ttft,
        gen_tps=gen_tps,
        tokens=eval_count,
        prompt_tokens=prompt_count,
        load_s=load_ns / 1e9,
        prefill_s=prefill_ns / 1e9,
        eval_s=eval_ns / 1e9,
    )


async def benchmark_level(
    session: aiohttp.ClientSession,
    url: str,
    model: str,
    prompts: list[str],
    concurrency: int,
    num_ctx: int,
    num_predict: int,
    temperature: float,
) -> list[RequestResult]:
    sem = asyncio.Semaphore(concurrency)

    async def bounded(prompt: str) -> RequestResult:
        async with sem:
            return await run_request(
                session, url, model, prompt, num_ctx, num_predict, temperature
            )

    return await asyncio.gather(*[bounded(p) for p in prompts])


async def warmup(
    session: aiohttp.ClientSession,
    url: str,
    model: str,
    prompt: str,
    num_ctx: int,
    num_predict: int,
    temperature: float,
) -> RequestResult:
    return await run_request(
        session, url, model, prompt, num_ctx, num_predict, temperature
    )


async def poll_ps_vram(
    session: aiohttp.ClientSession,
    url: str,
    model: str,
) -> Optional[float]:
    try:
        async with session.get(f"{url}/api/ps", timeout=10) as resp:
            data = await resp.json()
        for m in data.get("models", []):
            name = m.get("name", "")
            if model in name or name.startswith(model.split(":")[0]):
                size_vram = m.get("size_vram", 0)
                if size_vram:
                    return size_vram / (1024**3)
    except Exception:
        pass
    return None


async def poll_prometheus_vram(
    session: aiohttp.ClientSession,
    prom_url: str,
    instance_label: Optional[str],
) -> Optional[float]:
    query = "amdgpu_vram_used_bytes"
    if instance_label:
        query += f'{{instance=~"{instance_label}"}}'
    try:
        async with session.get(
            f"{prom_url}/api/v1/query",
            params={"query": query},
            timeout=10,
        ) as resp:
            data = await resp.json()
        results = data.get("data", {}).get("result", [])
        if results:
            return float(results[0]["value"][1]) / (1024**3)
    except Exception:
        pass
    return None


def make_prompts(
    mixed: bool, rotate_prompts: bool, requests_per_level: int
) -> list[str]:
    if mixed:
        # Always include one mem0-style long extraction prompt and fill the rest
        # with sub-agent prompts. With enough slots, each prompt prefix stays
        # resident; with fewer slots than distinct prompts, cache thrashing is
        # exercised.
        prompts = [MEM0_STYLE_PROMPT]
        i = 0
        while len(prompts) < requests_per_level:
            prompts.append(SUB_AGENT_PROMPTS[i % len(SUB_AGENT_PROMPTS)])
            i += 1
        return prompts
    if rotate_prompts:
        return [
            SUB_AGENT_PROMPTS[i % len(SUB_AGENT_PROMPTS)]
            for i in range(requests_per_level)
        ]
    # Default: identical prompt for every request so per-slot prefix caching is
    # stable at every concurrency level. This isolates continuous-batching
    # throughput from cache-thrash effects.
    return [SUB_AGENT_PROMPTS[0]] * requests_per_level


def print_summary_table(results: list[LevelResult]) -> None:
    print("\n=== Concurrency Benchmark Summary ===")
    print(
        f"{'Conc':>4} | {'Req':>3} | {'Wall(s)':>7} | {'TTFT(s)':>7} | "
        f"{'Gen(t/s)':>8} | {'Total(t/s)':>10} | {'VRAM(GB)':>8}"
    )
    print("-" * 70)
    for r in results:
        s = r.to_summary()
        print(
            f"{r.concurrency:>4} | {r.requests:>3} | "
            f"{s['wall_s']['median']:>7.2f} | "
            f"{s['ttft_s']['median']:>7.2f} | "
            f"{s['gen_tps']['median']:>8.1f} | "
            f"{s['throughput_tok_s']:>10.1f} | "
            f"{s['vram_gb'] if s['vram_gb'] is not None else 'n/a':>8}"
        )


def write_csv(results: list[LevelResult], path: Path) -> None:
    rows = []
    for r in results:
        s = r.to_summary()
        rows.append(
            {
                "concurrency": r.concurrency,
                "requests": r.requests,
                "repeats": r.repeats,
                "wall_mean": s["wall_s"]["mean"],
                "wall_median": s["wall_s"]["median"],
                "ttft_mean": s["ttft_s"]["mean"],
                "ttft_median": s["ttft_s"]["median"],
                "gen_tps_mean": s["gen_tps"]["mean"],
                "gen_tps_median": s["gen_tps"]["median"],
                "throughput_tok_s": s["throughput_tok_s"],
                "throughput_tok_s_min": s["repeat_throughput"]["min"],
                "throughput_tok_s_max": s["repeat_throughput"]["max"],
                "vram_gb": s["vram_gb"],
            }
        )
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Benchmark Ollama concurrent inference for GPU cohabitation analysis.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--url", default=DEFAULT_URL, help="Ollama base URL")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Model to benchmark")
    parser.add_argument(
        "--max-concurrency",
        type=int,
        default=6,
        help="Maximum client concurrency level to test (tests 1..N)",
    )
    parser.add_argument(
        "--requests-per-level",
        type=int,
        default=None,
        help="Number of requests fired per level (default: max-concurrency)",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=3,
        help="Repeat each concurrency level this many times",
    )
    parser.add_argument(
        "--num-ctx", type=int, default=4096, help="Per-request context length"
    )
    parser.add_argument(
        "--num-predict", type=int, default=512, help="Max tokens to generate"
    )
    parser.add_argument(
        "--temperature", type=float, default=0.3, help="Sampling temperature"
    )
    parser.add_argument(
        "--mixed",
        action="store_true",
        help="Include a mem0-style long-prompt request in every level",
    )
    parser.add_argument(
        "--rotate-prompts",
        action="store_true",
        help="Rotate through distinct sub-agent prompts (exercises cache thrash)",
    )
    parser.add_argument(
        "--cooldown",
        type=int,
        default=15,
        help="Seconds to wait between concurrency levels",
    )
    parser.add_argument(
        "--prom-url",
        default=DEFAULT_PROM_URL,
        help="Prometheus URL for GPU VRAM query (disable with empty string)",
    )
    parser.add_argument(
        "--prom-instance",
        default=".*",
        help="Prometheus instance label regex for amdgpu_vram_used_bytes",
    )
    parser.add_argument(
        "--output-json", type=Path, default=None, help="Path to write full JSON results"
    )
    parser.add_argument(
        "--output-csv", type=Path, default=None, help="Path to write CSV summary"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the first prompt and exit without calling Ollama",
    )
    return parser


async def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    requests_per_level = args.requests_per_level or args.max_concurrency
    if requests_per_level < 1:
        print("requests-per-level must be >= 1", file=sys.stderr)
        return 1

    if args.dry_run:
        prompts = make_prompts(args.mixed, args.rotate_prompts, requests_per_level)
        print("Dry run; first prompt:")
        print(prompts[0][:200] + "..." if len(prompts[0]) > 200 else prompts[0])
        return 0

    connector = aiohttp.TCPConnector(limit=args.max_concurrency + 5)
    async with aiohttp.ClientSession(connector=connector) as session:
        # Connectivity / model check.
        try:
            async with session.get(f"{args.url}/api/tags", timeout=10) as resp:
                models = await resp.json()
            model_names = [m.get("name", "") for m in models.get("models", [])]
            if not any(args.model in n for n in model_names):
                print(
                    f"WARNING: model '{args.model}' not found in Ollama /api/tags. "
                    f"Available: {model_names}",
                    file=sys.stderr,
                )
        except Exception as exc:
            print(f"ERROR: cannot reach Ollama at {args.url}: {exc}", file=sys.stderr)
            return 1

        # Warmup to ensure the model is loaded and caches are warm.
        print(f"Warming up with 1 request to {args.model} ...")
        warmup_prompt = SUB_AGENT_PROMPTS[0]
        warmup_res = await warmup(
            session,
            args.url,
            args.model,
            warmup_prompt,
            args.num_ctx,
            args.num_predict,
            args.temperature,
        )
        if warmup_res.error:
            print(f"ERROR during warmup: {warmup_res.error}", file=sys.stderr)
            return 1
        print(
            f"Warmup complete: wall={warmup_res.wall_s:.2f}s, "
            f"TTFT={warmup_res.ttft_s:.2f}s, gen={warmup_res.gen_tps:.1f} tok/s"
        )

        level_results: list[LevelResult] = []
        for level in range(1, args.max_concurrency + 1):
            prompts = make_prompts(args.mixed, args.rotate_prompts, requests_per_level)
            print(
                f"\nBenchmarking concurrency={level} with {requests_per_level} requests "
                f"(x{args.repeat} repeats) ..."
            )

            level_res = LevelResult(
                concurrency=level, requests=len(prompts), repeats=args.repeat
            )
            for _ in range(args.repeat):
                repeat_start = time.monotonic()
                repeat_results = await benchmark_level(
                    session,
                    args.url,
                    args.model,
                    prompts,
                    level,
                    args.num_ctx,
                    args.num_predict,
                    args.temperature,
                )
                repeat_wall = time.monotonic() - repeat_start
                repeat_tokens = 0
                for rr in repeat_results:
                    if rr.error:
                        print(
                            f"  Request error at concurrency={level}: {rr.error}",
                            file=sys.stderr,
                        )
                        continue
                    level_res.wall_s.append(rr.wall_s)
                    level_res.ttft_s.append(rr.ttft_s)
                    level_res.gen_tps.append(rr.gen_tps)
                    level_res.tokens.append(rr.tokens)
                    level_res.prompt_tokens.append(rr.prompt_tokens)
                    repeat_tokens += rr.tokens
                if repeat_wall > 0:
                    level_res.repeat_throughput.append(repeat_tokens / repeat_wall)
            level_res.throughput_tok_s = (
                statistics.median(level_res.repeat_throughput)
                if level_res.repeat_throughput
                else 0.0
            )

            # Poll VRAM once after the level completes.
            ps_vram = await poll_ps_vram(session, args.url, args.model)
            prom_vram = await poll_prometheus_vram(
                session, args.prom_url, args.prom_instance
            )
            level_res.vram_gb = prom_vram if prom_vram is not None else ps_vram

            level_results.append(level_res)
            s = level_res.to_summary()
            print(
                f"  median wall={s['wall_s']['median']:.2f}s, "
                f"median TTFT={s['ttft_s']['median']:.2f}s, "
                f"median gen={s['gen_tps']['median']:.1f} tok/s, "
                f"aggregate throughput={s['throughput_tok_s']:.1f} tok/s, "
                f"VRAM={s['vram_gb']} GB"
            )

            if level < args.max_concurrency:
                print(f"Cooling down for {args.cooldown}s ...")
                await asyncio.sleep(args.cooldown)

        print_summary_table(level_results)

        if args.output_json:
            payload = {
                "config": {
                    "url": args.url,
                    "model": args.model,
                    "max_concurrency": args.max_concurrency,
                    "requests_per_level": requests_per_level,
                    "repeats": args.repeat,
                    "num_ctx": args.num_ctx,
                    "num_predict": args.num_predict,
                    "temperature": args.temperature,
                    "mixed": args.mixed,
                    "rotate_prompts": args.rotate_prompts,
                    "prom_url": args.prom_url,
                    "prom_instance": args.prom_instance,
                },
                "results": [r.to_summary() for r in level_results],
            }
            args.output_json.write_text(json.dumps(payload, indent=2))
            print(f"\nWrote full JSON results to {args.output_json}")

        if args.output_csv:
            write_csv(level_results, args.output_csv)
            print(f"Wrote CSV summary to {args.output_csv}")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
