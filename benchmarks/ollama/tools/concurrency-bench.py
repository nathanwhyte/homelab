#!/usr/bin/env python3
"""Ollama concurrency benchmark harness.

Replaces the ad-hoc llama/tools/ollama-concurrency-benchmark.py with a
config-driven tool that reports P50/P90/P95/P99 latency percentiles,
inter-token latency (ITL), and aggregate throughput. Supports mixed mem0 +
sub-agent workloads and samples GPU metrics during the run.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import tomllib


try:
    import aiohttp
except ImportError as exc:  # pragma: no cover - runtime dependency hint
    raise SystemExit(
        "aiohttp is required. Run: uv run --with aiohttp python "
        "benchmarks/ollama/tools/concurrency-bench.py ..."
    ) from exc


# Make lib imports work when running from repo root, PYTHONPATH, or a flat
# directory (e.g. /bench with lib/ alongside the script).
script_dir = Path(__file__).resolve().parent
lib_root: Path | None = None
# Flat layout: lib/ next to the script.
if (script_dir / "lib" / "metrics.py").exists():
    lib_root = script_dir / "lib"
# Source layout mounted under /scripts with /scripts/lib alongside.
elif (script_dir / "scripts" / "lib" / "metrics.py").exists():
    lib_root = script_dir / "scripts" / "lib"
# Repo root layout: walk up until we find a lib/ directory with metrics.py.
else:
    for parent in script_dir.parents:
        if (parent / "lib" / "metrics.py").exists():
            lib_root = parent / "lib"
            break
if lib_root is None:
    raise SystemExit(
        f"Could not find benchmark lib/ directory containing metrics.py "
        f"(script_dir={script_dir})"
    )
sys.path.insert(0, str(lib_root))

from metrics import sample_during  # type: ignore[import-not-found]
from output import (  # type: ignore[import-not-found]
    build_meta,
    latency_summary,
    markdown_table,
    throughput_summary,
    write_csv,
    write_json,
)
from prompts import select_workload  # type: ignore[import-not-found]
from tool_calls import validate_tool_call  # type: ignore[import-not-found]


@dataclass
class RequestResult:
    wall_s: float
    ttft_s: float
    itl_s: float
    gen_tps: float
    tokens: int
    prompt_tokens: int
    error: Optional[str] = None
    response_text: Optional[str] = None
    tool_validation: Optional[dict[str, Any]] = None


@dataclass
class LevelResult:
    concurrency: int
    requests: int
    repeats: int
    wall_s: list[float] = field(default_factory=list)
    ttft_s: list[float] = field(default_factory=list)
    itl_s: list[float] = field(default_factory=list)
    gen_tps: list[float] = field(default_factory=list)
    tokens: list[int] = field(default_factory=list)
    prompt_tokens: list[int] = field(default_factory=list)
    repeat_throughput: list[float] = field(default_factory=list)
    aggregate_tok_s: Optional[float] = None
    tool_validations: list[dict[str, Any]] = field(default_factory=list)

    def _tool_validation_summary(self) -> Optional[dict[str, Any]]:
        if not self.tool_validations:
            return None
        n = len(self.tool_validations)

        def _rate(key: str) -> float:
            return sum(1 for v in self.tool_validations if v.get(key)) / n

        return {
            "count": n,
            "valid_json_rate": round(_rate("valid_json"), 4),
            "has_tool_field_rate": round(_rate("has_tool_field"), 4),
            "has_arguments_field_rate": round(_rate("has_arguments_field"), 4),
            "tool_recognized_rate": round(_rate("tool_recognized"), 4),
            "required_args_present_rate": round(_rate("required_args_present"), 4),
            "arg_types_valid_rate": round(_rate("arg_types_valid"), 4),
            "fully_valid_rate": round(
                sum(
                    1
                    for v in self.tool_validations
                    if v.get("valid_json")
                    and v.get("tool_recognized")
                    and v.get("required_args_present")
                    and v.get("arg_types_valid")
                )
                / n,
                4,
            ),
        }

    def to_summary(self) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "concurrency": self.concurrency,
            "requests": self.requests,
            "repeats": self.repeats,
            "aggregate_tok_s": round(self.aggregate_tok_s, 2)
            if self.aggregate_tok_s is not None
            else None,
            "wall_s": latency_summary(self.wall_s, "wall_s"),
            "ttft_s": latency_summary(self.ttft_s, "ttft_s"),
            "itl_s": latency_summary(self.itl_s, "itl_s"),
            "gen_tps": latency_summary(self.gen_tps, "gen_tps"),
            "tokens": {
                "mean": round(statistics.mean(self.tokens), 1) if self.tokens else 0.0,
                "total": sum(self.tokens),
            },
            "prompt_tokens": {
                "mean": round(statistics.mean(self.prompt_tokens), 1)
                if self.prompt_tokens
                else 0.0,
                "total": sum(self.prompt_tokens),
            },
        }
        tv = self._tool_validation_summary()
        if tv is not None:
            summary["tool_validation"] = tv
        return summary

    def to_csv_row(self) -> dict[str, Any]:
        """Flatten percentiles into scalar columns for CSV/Markdown output."""
        s = self.to_summary()

        def _lat(prefix: str) -> dict[str, float]:
            d = s[prefix]
            return {
                f"{prefix}_p50": d["p50"],
                f"{prefix}_p95": d["p95"],
                f"{prefix}_mean": d["mean"],
            }

        row = {
            "concurrency": s["concurrency"],
            "requests": s["requests"],
            "repeats": s["repeats"],
            "aggregate_tok_s": s["aggregate_tok_s"],
            **_lat("wall_s"),
            **_lat("ttft_s"),
            **_lat("itl_s"),
            **_lat("gen_tps"),
            "tokens_mean": s["tokens"]["mean"],
            "tokens_total": s["tokens"]["total"],
            "prompt_tokens_mean": s["prompt_tokens"]["mean"],
            "prompt_tokens_total": s["prompt_tokens"]["total"],
        }
        if "tool_validation" in s:
            row.update({f"tv_{k}": v for k, v in s["tool_validation"].items()})
        return row


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
            f"{url}/api/generate",
            json=payload,
            timeout=aiohttp.ClientTimeout(total=300),
        ) as resp:
            data = await resp.json()
    except Exception as exc:
        return RequestResult(
            wall_s=time.monotonic() - t0,
            ttft_s=0.0,
            itl_s=0.0,
            gen_tps=0.0,
            tokens=0,
            prompt_tokens=0,
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
    itl = 1.0 / gen_tps if gen_tps > 0 else 0.0

    response_text = data.get("response", "")
    return RequestResult(
        wall_s=wall,
        ttft_s=ttft,
        itl_s=itl,
        gen_tps=gen_tps,
        tokens=eval_count,
        prompt_tokens=prompt_count,
        response_text=response_text,
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
    expected_tools: Optional[list[Optional[str]]] = None,
    validate_tools: bool = False,
) -> list[RequestResult]:
    sem = asyncio.Semaphore(concurrency)

    async def bounded(prompt_idx: int, prompt: str) -> RequestResult:
        async with sem:
            result = await run_request(
                session, url, model, prompt, num_ctx, num_predict, temperature
            )
            if validate_tools and result.response_text is not None:
                expected = expected_tools[prompt_idx] if expected_tools else None
                validation = validate_tool_call(result.response_text, expected)
                result.tool_validation = validation.to_dict()
            return result

    return await asyncio.gather(*[bounded(i, p) for i, p in enumerate(prompts)])


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


def load_config(path: Path) -> dict[str, Any]:
    with path.open("rb") as f:
        return tomllib.load(f)


def make_output_dir(config: dict[str, Any], override_dir: Optional[str]) -> Path:
    base = (
        Path(override_dir)
        if override_dir
        else Path(config.get("output", {}).get("dir", "benchmarks/results"))
    )
    prefix = config.get("output", {}).get("prefix", "ollama")
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    outdir = base / f"{prefix}-{timestamp}"
    outdir.mkdir(parents=True, exist_ok=True)
    return outdir


def print_summary_table(results: list[LevelResult]) -> None:
    print("\n=== Ollama Concurrency Benchmark Summary ===")
    print(
        f"{'Conc':>4} | {'Req':>3} | {'P50 TTFT':>8} | {'P95 TTFT':>8} | "
        f"{'P50 ITL':>8} | {'P95 ITL':>8} | {'Agg tok/s':>10} | {'P50 gen':>8}"
    )
    print("-" * 78)
    for r in results:
        s = r.to_summary()
        print(
            f"{r.concurrency:>4} | {r.requests:>3} | "
            f"{s['ttft_s']['p50']:>8.2f} | {s['ttft_s']['p95']:>8.2f} | "
            f"{s['itl_s']['p50']:>8.3f} | {s['itl_s']['p95']:>8.3f} | "
            f"{s['aggregate_tok_s'] if s['aggregate_tok_s'] is not None else 'n/a':>10} | "
            f"{s['gen_tps']['p50']:>8.1f}"
        )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Benchmark Ollama concurrent inference.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("benchmarks/ollama/configs/default.toml"),
        help="TOML config path",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="Override the output directory from config",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the first prompt and exit without calling Ollama",
    )
    parser.add_argument(
        "--no-gpu-sampling",
        action="store_true",
        help="Disable Prometheus GPU metric sampling",
    )
    parser.add_argument(
        "--amdsmi",
        action="store_true",
        help="Sample AMD GPU metrics via the local amdsmi Python library (ROCm)",
    )
    parser.add_argument(
        "--tool-validate",
        action="store_true",
        help="Validate tool-calling responses (requires tool_calling workload)",
    )
    return parser


async def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    if not args.config.exists():
        print(f"ERROR: config not found: {args.config}", file=sys.stderr)
        return 1

    config = load_config(args.config)
    ollama_cfg = config.get("ollama", {})
    workload_cfg = config.get("workload", {})
    prom_cfg = config.get("prometheus", {})

    url = ollama_cfg.get("url", "http://ollama.llama.svc:11434")
    model = ollama_cfg.get("model", "gemma4:12b-it-qat")
    num_ctx = ollama_cfg.get("num_ctx", 16384)
    num_predict = ollama_cfg.get("num_predict", 512)
    temperature = ollama_cfg.get("temperature", 0.3)

    workload_name = workload_cfg.get("name", "mixed_mem0")
    max_concurrency = workload_cfg.get("max_concurrency", 6)
    requests_per_level = workload_cfg.get("requests_per_level", max_concurrency)
    repeats = workload_cfg.get("repeats", 3)
    cooldown = workload_cfg.get("cooldown_seconds", 15)
    tool_validate = args.tool_validate or workload_cfg.get("tool_validate", False)

    prom_url = prom_cfg.get("url", "http://prom-prometheus.grafana:9090")
    prom_instance = prom_cfg.get("instance_label", ".*")
    sample_interval = prom_cfg.get("sample_interval_seconds", 2.0)
    enable_gpu = not args.no_gpu_sampling and prom_url
    use_amdsmi = args.amdsmi

    workload = select_workload(workload_name, requests_per_level)
    prompts = workload.prompts
    expected_tools = workload.expected_tools if workload.expected_tools else None

    if args.dry_run:
        print("Dry run; first prompt:")
        print(prompts[0][:200] + "..." if len(prompts[0]) > 200 else prompts[0])
        return 0

    connector = aiohttp.TCPConnector(limit=max_concurrency + 5)
    async with aiohttp.ClientSession(connector=connector) as session:
        # Connectivity / model check.
        try:
            async with session.get(f"{url}/api/tags", timeout=10) as resp:
                models = await resp.json()
            model_names = [m.get("name", "") for m in models.get("models", [])]
            if not any(model in n for n in model_names):
                print(
                    f"WARNING: model '{model}' not found in Ollama /api/tags. "
                    f"Available: {model_names}",
                    file=sys.stderr,
                )
        except Exception as exc:
            print(f"ERROR: cannot reach Ollama at {url}: {exc}", file=sys.stderr)
            return 1

        # Warmup.
        print(f"Warming up with 1 request to {model} ...")
        warmup_res = await warmup(
            session, url, model, prompts[0], num_ctx, num_predict, temperature
        )
        if warmup_res.error:
            print(f"ERROR during warmup: {warmup_res.error}", file=sys.stderr)
            return 1
        print(
            f"Warmup complete: wall={warmup_res.wall_s:.2f}s, "
            f"TTFT={warmup_res.ttft_s:.2f}s, ITL={warmup_res.itl_s:.3f}s, "
            f"gen={warmup_res.gen_tps:.1f} tok/s"
        )

        level_results: list[LevelResult] = []
        for level in range(1, max_concurrency + 1):
            print(
                f"\nBenchmarking concurrency={level} with {requests_per_level} requests "
                f"(x{repeats} repeats) ..."
            )

            level_res = LevelResult(
                concurrency=level, requests=len(prompts), repeats=repeats
            )
            gpu_samples: list[Any] = []
            repeat_start_global = time.monotonic()

            for _ in range(repeats):
                repeat_start = time.monotonic()
                repeat_results = await benchmark_level(
                    session,
                    url,
                    model,
                    prompts,
                    level,
                    num_ctx,
                    num_predict,
                    temperature,
                    expected_tools=expected_tools,
                    validate_tools=tool_validate,
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
                    level_res.itl_s.append(rr.itl_s)
                    level_res.gen_tps.append(rr.gen_tps)
                    level_res.tokens.append(rr.tokens)
                    level_res.prompt_tokens.append(rr.prompt_tokens)
                    repeat_tokens += rr.tokens
                    if rr.tool_validation:
                        level_res.tool_validations.append(rr.tool_validation)
                if repeat_wall > 0:
                    level_res.repeat_throughput.append(repeat_tokens / repeat_wall)

                if enable_gpu or use_amdsmi:
                    series_list = await sample_during(
                        session,
                        duration_s=repeat_wall,
                        interval_s=sample_interval,
                        prom_url=prom_url,
                        ollama_url=url,
                        model_name=model,
                        instance_label=prom_instance,
                        use_amdsmi=use_amdsmi,
                    )
                    gpu_samples.extend([s.__dict__ for s in series_list])

            total_wall = time.monotonic() - repeat_start_global
            total_tokens = sum(level_res.tokens)
            level_res.aggregate_tok_s = (
                total_tokens / total_wall if total_wall > 0 else 0.0
            )

            level_results.append(level_res)
            s = level_res.to_summary()
            print(
                f"  P50 TTFT={s['ttft_s']['p50']:.2f}s, "
                f"P95 TTFT={s['ttft_s']['p95']:.2f}s, "
                f"P50 ITL={s['itl_s']['p50']:.3f}s, "
                f"aggregate throughput={s['aggregate_tok_s']:.1f} tok/s"
            )
            if "tool_validation" in s:
                tv = s["tool_validation"]
                print(
                    f"  tool validation: fully_valid={tv['fully_valid_rate']:.1%} "
                    f"(json={tv['valid_json_rate']:.1%}, "
                    f"tool={tv['tool_recognized_rate']:.1%}, "
                    f"args={tv['required_args_present_rate']:.1%})"
                )

            if level < max_concurrency:
                print(f"Cooling down for {cooldown}s ...")
                await asyncio.sleep(cooldown)

        print_summary_table(level_results)

        outdir = make_output_dir(config, args.output_dir)
        json_path = outdir / "results.json"
        csv_path = outdir / "summary.csv"
        md_path = outdir / "summary.md"

        samples_meta = [
            {
                "workload": workload_name,
                "max_concurrency": max_concurrency,
                "requests_per_level": requests_per_level,
                "repeats": repeats,
            }
        ]
        all_validations = [v for r in level_results for v in r.tool_validations]
        summary: dict[str, Any] = {
            "levels": [r.to_summary() for r in level_results],
            "overall_throughput": throughput_summary(
                [tok for r in level_results for tok in r.tokens],
                [w for r in level_results for w in r.wall_s],
            ),
        }
        if all_validations:
            n = len(all_validations)

            def _overall_rate(key: str) -> float:
                return sum(1 for v in all_validations if v.get(key)) / n

            summary["overall_tool_validation"] = {
                "count": n,
                "valid_json_rate": round(_overall_rate("valid_json"), 4),
                "has_tool_field_rate": round(_overall_rate("has_tool_field"), 4),
                "has_arguments_field_rate": round(
                    _overall_rate("has_arguments_field"), 4
                ),
                "tool_recognized_rate": round(_overall_rate("tool_recognized"), 4),
                "required_args_present_rate": round(
                    _overall_rate("required_args_present"), 4
                ),
                "arg_types_valid_rate": round(_overall_rate("arg_types_valid"), 4),
                "fully_valid_rate": round(
                    sum(
                        1
                        for v in all_validations
                        if v.get("valid_json")
                        and v.get("tool_recognized")
                        and v.get("required_args_present")
                        and v.get("arg_types_valid")
                    )
                    / n,
                    4,
                ),
            }

        write_json(
            json_path,
            meta=build_meta("ollama-concurrency-bench"),
            config=config,
            samples=samples_meta,
            summary=summary,
            gpu_series=gpu_samples or None,
        )
        print(f"\nWrote JSON results to {json_path}")

        csv_rows = [r.to_csv_row() for r in level_results]
        write_csv(csv_path, csv_rows)
        print(f"Wrote CSV summary to {csv_path}")

        md_path.write_text(
            f"# Ollama concurrency benchmark summary\n\n{markdown_table(csv_rows)}\n"
        )
        print(f"Wrote Markdown summary to {md_path}")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
