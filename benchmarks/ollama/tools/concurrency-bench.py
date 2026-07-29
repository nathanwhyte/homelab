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
import json
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional, Union

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

# Upper bound for a concurrently-running GPU sampler. The sampler is stopped by
# event the moment its repeat finishes; this only caps a runaway.
GPU_SAMPLE_MAX_S = 3600.0

# Ollama accepts `think` as a bool or as a level string ("low"/"medium"/"high").
ThinkParam = Optional[Union[bool, str]]


@dataclass
class RequestResult:
    wall_s: float
    ttft_s: float
    itl_s: float
    gen_tps: float
    tokens: int
    prompt_tokens: int
    # Time to first *answer* token. For a thinking model Ollama streams
    # reasoning first, so ttft_s (first output of any kind) can be near zero
    # while the answer is still seconds away; think on/off latency is only
    # comparable on ttfa_s.
    ttfa_s: float = 0.0
    error: Optional[str] = None
    response_text: Optional[str] = None
    tool_validation: Optional[dict[str, Any]] = None
    # Ollama separates a reasoning model's chain-of-thought (`thinking`) from
    # its answer (`response`). Without both, a reply that burns its whole token
    # budget reasoning and returns an empty answer is indistinguishable from a
    # genuine answer of the same token count.
    thinking_text: Optional[str] = None
    done_reason: Optional[str] = None
    # Server-reported load+prefill, retained alongside the client-observed TTFT
    # above so the two can be compared (queue time is their difference).
    server_prefill_s: float = 0.0

    @property
    def empty_answer(self) -> bool:
        """Completed, but produced no usable answer text.

        Keyed on stripped content, not token count: a request that returns
        whitespace with eval_count=0 is just as unusable as one that spent a
        full budget reasoning, and gating on tokens > 0 would score it usable.
        """
        return self.error is None and not (self.response_text or "").strip()

    @property
    def truncated(self) -> bool:
        """Hit the num_predict ceiling rather than stopping naturally."""
        return self.done_reason == "length"


@dataclass
class LevelResult:
    concurrency: int
    requests: int
    repeats: int
    wall_s: list[float] = field(default_factory=list)
    ttft_s: list[float] = field(default_factory=list)
    ttfa_s: list[float] = field(default_factory=list)
    server_prefill_s: list[float] = field(default_factory=list)
    itl_s: list[float] = field(default_factory=list)
    gen_tps: list[float] = field(default_factory=list)
    tokens: list[int] = field(default_factory=list)
    prompt_tokens: list[int] = field(default_factory=list)
    repeat_throughput: list[float] = field(default_factory=list)
    aggregate_tok_s: Optional[float] = None
    tool_validations: list[dict[str, Any]] = field(default_factory=list)
    # Response-quality accounting. Without these a row can report a healthy
    # token count while every answer was empty, truncated, or errored.
    attempted: int = 0
    errors: list[str] = field(default_factory=list)
    empty_answers: int = 0
    truncated_answers: int = 0
    done_reasons: dict[str, int] = field(default_factory=dict)

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

    def _quality_summary(self) -> dict[str, Any]:
        """Per-level response health.

        `succeeded` counts requests that returned without error; `usable`
        further excludes replies that produced no answer text. A reasoning
        model that spends its whole budget thinking lands in the gap.
        """
        ok = len(self.tokens)
        return {
            "attempted": self.attempted,
            "succeeded": ok,
            "failed": len(self.errors),
            "empty_answers": self.empty_answers,
            "truncated": self.truncated_answers,
            "usable": ok - self.empty_answers,
            "usable_rate": round((ok - self.empty_answers) / self.attempted, 4)
            if self.attempted
            else 0.0,
            "done_reasons": dict(self.done_reasons),
            "errors": self.errors[:10],
        }

    def to_summary(self) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "concurrency": self.concurrency,
            "requests": self.requests,
            "repeats": self.repeats,
            "quality": self._quality_summary(),
            "aggregate_tok_s": round(self.aggregate_tok_s, 2)
            if self.aggregate_tok_s is not None
            else None,
            "wall_s": latency_summary(self.wall_s, "wall_s"),
            "ttft_s": latency_summary(self.ttft_s, "ttft_s"),
            "ttfa_s": latency_summary(self.ttfa_s, "ttfa_s"),
            "server_prefill_s": latency_summary(
                self.server_prefill_s, "server_prefill_s"
            ),
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

        q = s["quality"]
        row = {
            "concurrency": s["concurrency"],
            "requests": s["requests"],
            "repeats": s["repeats"],
            "aggregate_tok_s": s["aggregate_tok_s"],
            # Quality travels with throughput into CSV/Markdown so downstream
            # consumers (plot-bars.py et al.) can gate on it. Plotting
            # aggregate_tok_s alone lets a row of empty answers render as a
            # tall bar indistinguishable from real work.
            "attempted": q["attempted"],
            "failed": q["failed"],
            "empty_answers": q["empty_answers"],
            "truncated": q["truncated"],
            "usable": q["usable"],
            "usable_rate": q["usable_rate"],
            **_lat("wall_s"),
            **_lat("ttft_s"),
            **_lat("ttfa_s"),
            **_lat("server_prefill_s"),
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


async def run_request_openai(
    session: aiohttp.ClientSession,
    url: str,
    model: str,
    prompt: str,
    num_predict: int,
    temperature: float,
    stop: Optional[list] = None,
) -> RequestResult:
    """Streaming /v1/completions request with client-side TTFT/ITL timing.

    OpenAI-compatible servers (mlx_lm.server, vLLM, llama.cpp server) do not
    report server-side load/prefill durations the way Ollama does, so TTFT is
    measured as first-chunk arrival. This includes HTTP/framing overhead —
    the same latency an editor client actually experiences.
    """
    payload = {
        "model": model,
        "prompt": prompt,
        "max_tokens": num_predict,
        "temperature": temperature,
        "stream": True,
    }
    if stop:
        payload["stop"] = stop
    t0 = time.monotonic()
    t_first: Optional[float] = None
    n_chunks = 0
    prompt_tokens = 0
    completion_tokens = 0
    text_parts: list[str] = []
    try:
        async with session.post(
            f"{url}/v1/completions",
            json=payload,
            timeout=aiohttp.ClientTimeout(total=300),
        ) as resp:
            async for line_bytes in resp.content:
                line = line_bytes.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data_str = line[len("data:") :].strip()
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                except ValueError:
                    continue
                choices = chunk.get("choices") or []
                if choices and choices[0].get("text"):
                    if t_first is None:
                        t_first = time.monotonic()
                    n_chunks += 1
                    text_parts.append(choices[0]["text"])
                usage = chunk.get("usage") or {}
                prompt_tokens = usage.get("prompt_tokens") or prompt_tokens
                completion_tokens = usage.get("completion_tokens") or completion_tokens
    except Exception as exc:
        return RequestResult(
            wall_s=time.monotonic() - t0,
            ttft_s=0.0,
            itl_s=0.0,
            gen_tps=0.0,
            tokens=0,
            prompt_tokens=0,
            # Type-prefixed: asyncio.TimeoutError stringifies to "", which is
            # falsy and would be read downstream as "no error".
            error=f"{type(exc).__name__}: {exc}",
        )
    t_end = time.monotonic()
    wall = t_end - t0
    if t_first is None:
        return RequestResult(
            wall_s=wall,
            ttft_s=0.0,
            itl_s=0.0,
            gen_tps=0.0,
            tokens=0,
            prompt_tokens=prompt_tokens,
            error="no tokens streamed",
        )
    tokens = completion_tokens or n_chunks
    decode_s = t_end - t_first
    gen_tps = (tokens - 1) / decode_s if decode_s > 0 and tokens > 1 else 0.0
    itl = 1.0 / gen_tps if gen_tps > 0 else 0.0
    return RequestResult(
        wall_s=wall,
        ttft_s=t_first - t0,
        itl_s=itl,
        gen_tps=gen_tps,
        tokens=tokens,
        prompt_tokens=prompt_tokens,
        response_text="".join(text_parts),
    )


async def run_request(
    session: aiohttp.ClientSession,
    url: str,
    model: str,
    prompt: str,
    num_ctx: int,
    num_predict: int,
    temperature: float,
    raw: bool = False,
    api: str = "ollama",
    stop: Optional[list] = None,
    extra_options: Optional[dict] = None,
    think: ThinkParam = None,
) -> RequestResult:
    if api == "openai_completions":
        return await run_request_openai(
            session, url, model, prompt, num_predict, temperature, stop
        )
    # Merge optional per-model sampling settings from the config's [ollama.options]
    # table; explicit keys (num_ctx/num_predict/temperature) always win over them.
    options: dict[str, Any] = dict(extra_options or {})
    options.update(
        {
            "num_ctx": num_ctx,
            "num_predict": num_predict,
            "temperature": temperature,
        }
    )
    payload = {
        "model": model,
        "prompt": prompt,
        # Streamed so TTFT is the client-observed first-token time (queueing
        # included) rather than the server's load+prefill total. The final
        # chunk still carries the same duration/count fields as a unary reply.
        "stream": True,
        "options": options,
    }
    if stop:
        payload["options"]["stop"] = stop
    if raw:
        payload["raw"] = True
    # `think` is a top-level Ollama API param, not an option. Omitted entirely
    # when unset so configs that don't set it send an unchanged payload.
    # Ollama accepts a bool or a level string ("low"/"medium"/"high").
    if think is not None:
        payload["think"] = think

    def _failure(exc: str, elapsed: float) -> RequestResult:
        # Never store a falsy error. asyncio.TimeoutError stringifies to "",
        # which made `if rr.error:` skip it and score a 300s timeout as a
        # successful zero-token request.
        return RequestResult(
            wall_s=elapsed,
            ttft_s=0.0,
            itl_s=0.0,
            gen_tps=0.0,
            tokens=0,
            prompt_tokens=0,
            error=exc or "unknown error (exception with empty message)",
        )

    t0 = time.monotonic()
    t_first: Optional[float] = None
    t_first_answer: Optional[float] = None
    saw_done = False
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    data: dict[str, Any] = {}
    try:
        async with session.post(
            f"{url}/api/generate",
            json=payload,
            timeout=aiohttp.ClientTimeout(total=300),
        ) as resp:
            # Ollama signals failure both by non-2xx status and by an `error`
            # key in an otherwise-200 body. Neither was previously checked, so
            # a rejected request scored as a success with zero-valued metrics.
            if resp.status != 200:
                body = (await resp.text())[:300]
                return _failure(f"HTTP {resp.status}: {body}", time.monotonic() - t0)
            async for line in resp.content:
                if not line.strip():
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError as exc:
                    # A frame we cannot parse means the stream is not what the
                    # protocol promises. Skipping it silently would let a
                    # truncated or corrupted response score as a success.
                    return _failure(
                        f"malformed stream frame: {exc}", time.monotonic() - t0
                    )
                if chunk.get("error"):
                    return _failure(
                        f"ollama error: {chunk['error']}", time.monotonic() - t0
                    )
                piece = chunk.get("response", "")
                thought = chunk.get("thinking", "")
                # Two distinct latencies. `t_first` is the first output of any
                # kind; `t_first_answer` is the first *answer* token. Ollama
                # streams reasoning before content, so for a thinking model the
                # two differ by the whole reasoning phase — collapsing them
                # would make think-on TTFT look near-zero and render think
                # on/off latency comparisons meaningless.
                if (piece or thought) and t_first is None:
                    t_first = time.monotonic()
                if piece and t_first_answer is None:
                    t_first_answer = time.monotonic()
                if piece:
                    text_parts.append(piece)
                if thought:
                    thinking_parts.append(thought)
                if chunk.get("done"):
                    data = chunk
                    saw_done = True
    except Exception as exc:
        # Include the type: several exceptions of interest here (notably
        # asyncio.TimeoutError) carry an empty message.
        return _failure(f"{type(exc).__name__}: {exc}", time.monotonic() - t0)

    wall = time.monotonic() - t0
    # A stream that ends without a terminating `done` chunk was cut short. All
    # the duration/count fields live on that final chunk, so without it the
    # result would be all zeros yet still read as a successful request.
    if not saw_done:
        return _failure("stream ended before a done chunk (premature EOF)", wall)
    if data.get("error"):
        return _failure(f"ollama error: {data['error']}", wall)

    load_ns = data.get("load_duration", 0) or 0
    prefill_ns = data.get("prompt_eval_duration", 0) or 0
    eval_ns = data.get("eval_duration", 0) or 0
    eval_count = data.get("eval_count", 0) or 0
    prompt_count = data.get("prompt_eval_count", 0) or 0

    ttft = (t_first - t0) if t_first is not None else wall
    ttfa = (t_first_answer - t0) if t_first_answer is not None else wall
    gen_tps = eval_count / (eval_ns / 1e9) if eval_ns else 0.0
    itl = 1.0 / gen_tps if gen_tps > 0 else 0.0

    return RequestResult(
        wall_s=wall,
        ttft_s=ttft,
        ttfa_s=ttfa,
        itl_s=itl,
        gen_tps=gen_tps,
        tokens=eval_count,
        prompt_tokens=prompt_count,
        response_text="".join(text_parts),
        thinking_text="".join(thinking_parts) or None,
        done_reason=data.get("done_reason"),
        server_prefill_s=(load_ns + prefill_ns) / 1e9,
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
    raw: bool = False,
    api: str = "ollama",
    stop: Optional[list] = None,
    extra_options: Optional[dict] = None,
    think: ThinkParam = None,
) -> list[RequestResult]:
    sem = asyncio.Semaphore(concurrency)

    async def bounded(prompt_idx: int, prompt: str) -> RequestResult:
        async with sem:
            result = await run_request(
                session,
                url,
                model,
                prompt,
                num_ctx,
                num_predict,
                temperature,
                raw,
                api,
                stop,
                extra_options,
                think,
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
    raw: bool = False,
    api: str = "ollama",
    stop: Optional[list] = None,
    extra_options: Optional[dict] = None,
    think: ThinkParam = None,
) -> RequestResult:
    return await run_request(
        session,
        url,
        model,
        prompt,
        num_ctx,
        num_predict,
        temperature,
        raw,
        api,
        stop,
        extra_options,
        think,
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
        required=True,
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
    extra_options = ollama_cfg.get("options", {})
    raw = ollama_cfg.get("raw", False)
    api = ollama_cfg.get("api", "ollama")
    stop = ollama_cfg.get("stop", None)
    think = ollama_cfg.get("think", None)

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

    # Tool validation parses the answer as a {"tool", "arguments"} JSON object.
    # Applied to a prose workload such as `agentic` it fails every correct
    # answer, reporting a uniform 0% that reads as a model defect. Only run it
    # where the workload actually asks for tool calls.
    if tool_validate and expected_tools is None:
        print(
            f"WARNING: workload '{workload_name}' does not emit tool calls; "
            f"ignoring tool validation (it would score every answer invalid). "
            f"Use the per-level `quality` block for usability instead.",
            file=sys.stderr,
        )
        tool_validate = False

    if args.dry_run:
        print("Dry run; first prompt:")
        print(prompts[0][:200] + "..." if len(prompts[0]) > 200 else prompts[0])
        return 0

    connector = aiohttp.TCPConnector(limit=max_concurrency + 5)
    async with aiohttp.ClientSession(connector=connector) as session:
        # Connectivity / model check.
        try:
            if api == "openai_completions":
                async with session.get(f"{url}/v1/models", timeout=10) as resp:
                    models = await resp.json()
                model_names = [m.get("id", "") for m in models.get("data", [])]
            else:
                async with session.get(f"{url}/api/tags", timeout=10) as resp:
                    models = await resp.json()
                model_names = [m.get("name", "") for m in models.get("models", [])]
            if not any(model in n for n in model_names):
                print(
                    f"WARNING: model '{model}' not found at {url}. "
                    f"Available: {model_names}",
                    file=sys.stderr,
                )
        except Exception as exc:
            print(f"ERROR: cannot reach server at {url}: {exc}", file=sys.stderr)
            return 1

        # Warmup.
        print(f"Warming up with 1 request to {model} ...")
        warmup_res = await warmup(
            session,
            url,
            model,
            prompts[0],
            num_ctx,
            num_predict,
            temperature,
            raw,
            api,
            stop,
            extra_options,
            think,
        )
        # `is not None`, not truthiness: an exception with an empty message
        # would otherwise read as success.
        if warmup_res.error is not None:
            print(f"ERROR during warmup: {warmup_res.error}", file=sys.stderr)
            return 1
        print(
            f"Warmup complete: wall={warmup_res.wall_s:.2f}s, "
            f"TTFT={warmup_res.ttft_s:.2f}s, ITL={warmup_res.itl_s:.3f}s, "
            f"gen={warmup_res.gen_tps:.1f} tok/s"
        )

        level_results: list[LevelResult] = []
        gpu_series_by_level: list[dict[str, Any]] = []
        run_elapsed_s = 0.0
        run_tokens = 0
        for level in range(1, max_concurrency + 1):
            print(
                f"\nBenchmarking concurrency={level} with {requests_per_level} requests "
                f"(x{repeats} repeats) ..."
            )

            level_res = LevelResult(
                concurrency=level, requests=len(prompts), repeats=repeats
            )
            # Per-level bucket, appended to the run-wide list below. Previously
            # this was rebound each level, so only the final level's samples
            # ever reached the output file.
            gpu_samples: list[Any] = []
            repeat_start_global = time.monotonic()

            for _ in range(repeats):
                # Sample the GPU *while* the workload runs. Sampling used to be
                # started after the workload finished, so it measured a post-run
                # idle GPU and its duration was then counted in the throughput
                # window — roughly halving reported aggregate throughput.
                gpu_stop = asyncio.Event()
                sampler: Optional[asyncio.Task] = None
                if enable_gpu or use_amdsmi:
                    sampler = asyncio.create_task(
                        sample_during(
                            session,
                            duration_s=GPU_SAMPLE_MAX_S,
                            interval_s=sample_interval,
                            prom_url=prom_url,
                            ollama_url=url,
                            model_name=model,
                            instance_label=prom_instance,
                            use_amdsmi=use_amdsmi,
                            stop_event=gpu_stop,
                        )
                    )
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
                    raw=raw,
                    api=api,
                    stop=stop,
                    extra_options=extra_options,
                    think=think,
                )
                repeat_wall = time.monotonic() - repeat_start
                repeat_tokens = 0
                for rr in repeat_results:
                    level_res.attempted += 1
                    if rr.error is not None:
                        level_res.errors.append(rr.error)
                        print(
                            f"  Request error at concurrency={level}: {rr.error}",
                            file=sys.stderr,
                        )
                        continue
                    if rr.done_reason:
                        level_res.done_reasons[rr.done_reason] = (
                            level_res.done_reasons.get(rr.done_reason, 0) + 1
                        )
                    if rr.empty_answer:
                        level_res.empty_answers += 1
                    if rr.truncated:
                        level_res.truncated_answers += 1
                    level_res.wall_s.append(rr.wall_s)
                    level_res.ttft_s.append(rr.ttft_s)
                    level_res.ttfa_s.append(rr.ttfa_s)
                    level_res.server_prefill_s.append(rr.server_prefill_s)
                    level_res.itl_s.append(rr.itl_s)
                    level_res.gen_tps.append(rr.gen_tps)
                    level_res.tokens.append(rr.tokens)
                    level_res.prompt_tokens.append(rr.prompt_tokens)
                    repeat_tokens += rr.tokens
                    if rr.tool_validation:
                        level_res.tool_validations.append(rr.tool_validation)
                if repeat_wall > 0:
                    level_res.repeat_throughput.append(repeat_tokens / repeat_wall)

                if sampler is not None:
                    gpu_stop.set()
                    series_list = await sampler
                    # asdict() recurses into the nested GpuSample dataclasses.
                    # __dict__ was shallow, so json.dumps(default=str) wrote the
                    # samples out as repr strings instead of objects.
                    gpu_samples.extend([asdict(s) for s in series_list])

            total_wall = time.monotonic() - repeat_start_global
            total_tokens = sum(level_res.tokens)
            level_res.aggregate_tok_s = (
                total_tokens / total_wall if total_wall > 0 else 0.0
            )
            run_elapsed_s += total_wall
            run_tokens += total_tokens
            if gpu_samples:
                gpu_series_by_level.append(
                    {"concurrency": level, "series": gpu_samples}
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
            # Elapsed wall clock across all levels, NOT the sum of per-request
            # wall times. Summing double-counts concurrent requests: two
            # simultaneous 10-token requests taking 1s scored 10 tok/s instead
            # of 20. Per-request rates are still reported by throughput_summary
            # for distribution stats.
            "overall_throughput": {
                **throughput_summary(
                    [tok for r in level_results for tok in r.tokens],
                    [w for r in level_results for w in r.wall_s],
                ),
                "elapsed_s": round(run_elapsed_s, 3),
                "aggregate_tok_s": round(run_tokens / run_elapsed_s, 2)
                if run_elapsed_s > 0
                else 0.0,
            },
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
            gpu_series=gpu_series_by_level or None,
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
