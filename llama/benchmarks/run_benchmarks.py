#!/usr/bin/env python3
"""Benchmark multiple llama.cpp-served models for quality and speed."""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import http.client
import json
import math
import re
import socket
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


SAFE_BUILTINS: dict[str, Any] = {
    "abs": abs,
    "all": all,
    "any": any,
    "bool": bool,
    "dict": dict,
    "enumerate": enumerate,
    "filter": filter,
    "float": float,
    "int": int,
    "len": len,
    "list": list,
    "map": map,
    "max": max,
    "min": min,
    "range": range,
    "reversed": reversed,
    "set": set,
    "sorted": sorted,
    "str": str,
    "sum": sum,
    "tuple": tuple,
    "zip": zip,
}

DISALLOWED_NODES = (
    ast.Import,
    ast.ImportFrom,
    ast.With,
    ast.Try,
    ast.ClassDef,
    ast.Raise,
    ast.Delete,
    ast.Global,
    ast.Nonlocal,
    ast.AsyncFunctionDef,
    ast.Await,
    ast.Yield,
    ast.YieldFrom,
)

BLOCKED_CALLS = {"__import__", "eval", "exec", "open", "compile", "input", "breakpoint"}


def parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parents[2]
    benchmark_dir = repo_root / "llama" / "benchmarks"

    parser = argparse.ArgumentParser(
        description="Run benchmark suite across 7B/8B model configs on llama.cpp deployment."
    )
    parser.add_argument(
        "--models-file",
        default=str(benchmark_dir / "models.json"),
        help="Path to model matrix JSON (default: %(default)s)",
    )
    parser.add_argument(
        "--cases-file",
        default=str(benchmark_dir / "cases.json"),
        help="Path to benchmark cases JSON (default: %(default)s)",
    )
    parser.add_argument(
        "--output-dir",
        default=str(benchmark_dir / "results"),
        help="Directory for result artifacts (default: %(default)s)",
    )
    parser.add_argument(
        "--model",
        default="current.gguf",
        help="Model name sent to OpenAI-compatible endpoint (default: %(default)s)",
    )
    parser.add_argument(
        "--namespace",
        default="llama",
        help="Kubernetes namespace containing llama deployment (default: %(default)s)",
    )
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:18000/v1",
        help="OpenAI-compatible endpoint base URL (default: %(default)s)",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.2,
        help="Sampling temperature for all prompts (default: %(default)s)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=180,
        help="Per-request HTTP timeout seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--rollout-timeout",
        type=int,
        default=1800,
        help="Deployment rollout timeout seconds when switching models (default: %(default)s)",
    )
    parser.add_argument(
        "--local-port",
        type=int,
        default=18000,
        help="Local port used for kubectl port-forward (default: %(default)s)",
    )
    parser.add_argument(
        "--models",
        default="",
        help="Comma-separated model ids to run (default: all)",
    )
    parser.add_argument(
        "--no-switch-models",
        action="store_true",
        help="Do not mutate deployment model env vars/rollout between models.",
    )
    parser.add_argument(
        "--no-port-forward",
        action="store_true",
        help="Use --base-url directly and skip kubectl port-forward management.",
    )
    parser.add_argument(
        "--max-cases-per-category",
        type=int,
        default=12,
        help="Maximum cases per category after loading suite (default: %(default)s)",
    )
    return parser.parse_args()


class PortForward:
    def __init__(self, namespace: str, local_port: int) -> None:
        self.namespace = namespace
        self.local_port = local_port
        self.proc: subprocess.Popen[str] | None = None

    def __enter__(self) -> "PortForward":
        cmd = [
            "kubectl",
            "-n",
            self.namespace,
            "port-forward",
            "svc/llama-api",
            f"{self.local_port}:80",
        ]
        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        deadline = time.time() + 30
        lines: list[str] = []
        while time.time() < deadline:
            if self.proc.poll() is not None:
                break
            if self._is_port_open("127.0.0.1", self.local_port):
                return self
            if self.proc.stdout is not None:
                line = self.proc.stdout.readline().strip()
                if line:
                    lines.append(line)
            time.sleep(0.2)
        detail = "\n".join(lines[-6:])
        raise SystemExit(f"Failed to establish port-forward:\n{detail}")

    @staticmethod
    def _is_port_open(host: str, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.4)
            return sock.connect_ex((host, port)) == 0

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        if self.proc is None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def run_cmd(cmd: list[str], timeout: int | None = None) -> str:
    proc = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "unknown error").strip()
        raise SystemExit(f"Command failed: {' '.join(cmd)}\n{detail}")
    return (proc.stdout or "").strip()


def load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def normalize_text(value: str) -> str:
    cleaned = value.strip().lower()
    cleaned = cleaned.strip("`\"'")
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"[^a-z0-9+./ -]", "", cleaned)
    return cleaned.strip()


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    rank = (len(values) - 1) * pct
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return values[lower]
    weight = rank - lower
    return values[lower] * (1 - weight) + values[upper] * weight


def extract_content(response: dict[str, Any]) -> str:
    choices = response.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    return str(message.get("content") or "").strip()


def call_llm(
    *,
    base_url: str,
    model: str,
    prompt: str,
    max_tokens: int,
    temperature: float,
    timeout: int,
) -> tuple[dict[str, Any], float]:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as err:
        details = err.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP {err.code}: {details}") from err
    except urllib.error.URLError as err:
        raise SystemExit(f"Connection error: {err.reason}") from err
    except http.client.RemoteDisconnected as err:
        raise SystemExit(f"Connection error: remote disconnected: {err}") from err
    except TimeoutError as err:
        raise SystemExit(f"Connection timeout: {err}") from err
    except ConnectionResetError as err:
        raise SystemExit(f"Connection reset: {err}") from err
    elapsed = time.perf_counter() - started

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as err:
        raise SystemExit(f"Invalid JSON response: {err}") from err
    return parsed, elapsed


def call_llm_with_retry(
    *,
    base_url: str,
    model: str,
    prompt: str,
    max_tokens: int,
    temperature: float,
    timeout: int,
    retries: int,
    retry_delay_s: float,
) -> tuple[dict[str, Any], float]:
    last_err: SystemExit | None = None
    for attempt in range(1, retries + 1):
        try:
            return call_llm(
                base_url=base_url,
                model=model,
                prompt=prompt,
                max_tokens=max_tokens,
                temperature=temperature,
                timeout=timeout,
            )
        except SystemExit as err:
            last_err = err
            if attempt == retries:
                break
            time.sleep(retry_delay_s)
    if last_err is not None:
        raise last_err
    raise SystemExit("Unknown error calling model endpoint")


def extract_python(text: str) -> str:
    match = re.search(
        r"```(?:python)?\s*(.*?)```", text, flags=re.IGNORECASE | re.DOTALL
    )
    if match:
        code = match.group(1)
    else:
        code = text
    if "def " in code and not code.lstrip().startswith("def "):
        code = code[code.index("def ") :]
    return code.strip()


def validate_code_ast(code: str) -> ast.Module:
    tree = ast.parse(code)
    for node in ast.walk(tree):
        if isinstance(node, DISALLOWED_NODES):
            raise ValueError(f"Disallowed syntax: {type(node).__name__}")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in BLOCKED_CALLS:
                raise ValueError(f"Disallowed call: {node.func.id}")
    return tree


def compare_value(actual: Any, expected: Any, *, order_insensitive_pairs: bool) -> bool:
    if order_insensitive_pairs:
        if isinstance(actual, tuple):
            actual = list(actual)
        if (
            isinstance(actual, list)
            and isinstance(expected, list)
            and len(actual) == len(expected) == 2
        ):
            return sorted(actual) == sorted(expected)
    return actual == expected


def score_python_unit(case: dict[str, Any], content: str) -> tuple[float, str]:
    evaluator = case["evaluator"]
    fn_name = evaluator["function"]
    tests = evaluator["tests"]
    order_insensitive_pairs = bool(evaluator.get("order_insensitive_pairs", False))

    code = extract_python(content)
    if not code:
        return 0.0, "empty output"

    try:
        validate_code_ast(code)
    except (SyntaxError, ValueError) as err:
        return 0.0, f"invalid code: {err}"

    env: dict[str, Any] = {"__builtins__": SAFE_BUILTINS}
    try:
        exec(compile(code, "<generated>", "exec"), env, env)
    except Exception as err:  # noqa: BLE001
        return 0.0, f"exec error: {err}"

    fn = env.get(fn_name)
    if not callable(fn):
        return 0.0, f"function not found: {fn_name}"

    passed = 0
    failures: list[str] = []
    for idx, test in enumerate(tests, start=1):
        args = test.get("args", [])
        expected = test.get("expected")
        try:
            actual = fn(*args)
        except Exception as err:  # noqa: BLE001
            failures.append(f"t{idx}: runtime error {err}")
            continue

        if compare_value(
            actual, expected, order_insensitive_pairs=order_insensitive_pairs
        ):
            passed += 1
        else:
            failures.append(f"t{idx}: expected={expected} actual={actual}")

    score = passed / max(len(tests), 1)
    note = "all tests passed" if not failures else "; ".join(failures[:2])
    return score, note


def score_exact(case: dict[str, Any], content: str) -> tuple[float, str]:
    accepted = [normalize_text(v) for v in case["evaluator"]["accepted"]]
    text = normalize_text(content.splitlines()[0] if content else "")
    if text in accepted:
        return 1.0, "exact match"
    return 0.0, f"expected one of {accepted}, got {text or '<empty>'}"


def score_keyword(case: dict[str, Any], content: str) -> tuple[float, str]:
    evaluator = case["evaluator"]
    required = [normalize_text(v) for v in evaluator["required"]]
    min_required = int(evaluator.get("min_required", len(required)))
    body = normalize_text(content)
    matched = sum(1 for token in required if token in body)

    raw = matched / max(len(required), 1)
    if matched < min_required:
        raw *= 0.6
    return raw, f"matched {matched}/{len(required)} keywords"


def score_case(
    case: dict[str, Any], response: dict[str, Any]
) -> tuple[float, str, str]:
    content = extract_content(response)
    evaluator_type = case["evaluator"]["type"]
    if evaluator_type == "python_unit":
        score, note = score_python_unit(case, content)
    elif evaluator_type == "exact":
        score, note = score_exact(case, content)
    elif evaluator_type == "keyword":
        score, note = score_keyword(case, content)
    else:
        score, note = 0.0, f"unknown evaluator type {evaluator_type}"
    return score, note, content


def switch_model(
    namespace: str, model_cfg: dict[str, Any], rollout_timeout: int
) -> None:
    deadline = time.time() + 120
    while time.time() < deadline:
        probe = subprocess.run(
            ["kubectl", "-n", namespace, "get", "deployment", "llamacpp-openai"],
            text=True,
            capture_output=True,
        )
        if probe.returncode == 0:
            break
        time.sleep(2)
    else:
        raise SystemExit(
            f"Deployment llamacpp-openai not found in namespace {namespace}."
        )

    env_vars = [
        f"LLAMA_PRIMARY_MODEL_FILE={model_cfg['primary_model_file']}",
        f"LLAMA_PRIMARY_MODEL_URL={model_cfg['primary_model_url']}",
        f"LLAMA_FALLBACK_MODEL_FILE={model_cfg['fallback_model_file']}",
        f"LLAMA_FALLBACK_MODEL_URL={model_cfg['fallback_model_url']}",
    ]
    run_cmd(
        [
            "kubectl",
            "-n",
            namespace,
            "set",
            "env",
            "deployment/llamacpp-openai",
            *env_vars,
        ]
    )
    run_cmd(
        ["kubectl", "-n", namespace, "rollout", "restart", "deployment/llamacpp-openai"]
    )
    run_cmd(
        [
            "kubectl",
            "-n",
            namespace,
            "rollout",
            "status",
            "deployment/llamacpp-openai",
            f"--timeout={rollout_timeout}s",
        ]
    )


def warm_endpoint(base_url: str, model: str, timeout: int, max_wait_s: int) -> None:
    deadline = time.time() + max_wait_s
    last_error = ""
    while time.time() < deadline:
        try:
            call_llm_with_retry(
                base_url=base_url,
                model=model,
                prompt="Reply with OK.",
                max_tokens=8,
                temperature=0.0,
                timeout=min(timeout, 30),
                retries=1,
                retry_delay_s=1.0,
            )
            return
        except SystemExit as err:
            last_error = str(err)
            time.sleep(5)
    detail = f" Last error: {last_error}" if last_error else ""
    raise SystemExit(f"Model endpoint did not warm up after rollout.{detail}")


def limit_cases(
    cases: list[dict[str, Any]], max_per_category: int
) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for case in cases:
        buckets.setdefault(case["category"], []).append(case)
    limited: list[dict[str, Any]] = []
    for category in ("codegen", "tech_qa", "basic_qa"):
        limited.extend(buckets.get(category, [])[:max_per_category])
    return limited


def category_means(case_results: list[dict[str, Any]]) -> dict[str, float]:
    scores: dict[str, list[float]] = {"codegen": [], "tech_qa": [], "basic_qa": []}
    for item in case_results:
        scores[item["category"]].append(item["score"])
    return {
        name: (sum(vals) / len(vals) if vals else 0.0) for name, vals in scores.items()
    }


def score_accuracy(categories: dict[str, float]) -> float:
    return 100.0 * (
        0.50 * categories.get("codegen", 0.0)
        + 0.30 * categories.get("tech_qa", 0.0)
        + 0.20 * categories.get("basic_qa", 0.0)
    )


def normalize_inverse(value: float, best: float, worst: float) -> float:
    if math.isclose(best, worst):
        return 1.0
    return (worst - value) / (worst - best)


def normalize_direct(value: float, low: float, high: float) -> float:
    if math.isclose(low, high):
        return 1.0
    return (value - low) / (high - low)


def write_markdown(path: Path, summary_rows: list[dict[str, Any]]) -> None:
    lines = [
        "# Llama Benchmark Results",
        "",
        "Weights: overall = 60% accuracy + 40% speed; accuracy = 50% codegen + 30% tech_qa + 20% basic_qa.",
        "",
        "| Rank | Model | Overall | Accuracy | Speed | Codegen | Tech QA | Basic QA | Avg Latency (s) | P95 (s) | Avg tok/s |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for idx, row in enumerate(summary_rows, start=1):
        lines.append(
            "| {rank} | {label} | {overall:.2f} | {accuracy:.2f} | {speed:.2f} | {codegen:.3f} | {tech:.3f} | {basic:.3f} | {lat:.2f} | {p95:.2f} | {tps:.2f} |".format(
                rank=idx,
                label=row["label"],
                overall=row["overall_score"],
                accuracy=row["accuracy_score"],
                speed=row["speed_score"],
                codegen=row["category_scores"]["codegen"],
                tech=row["category_scores"]["tech_qa"],
                basic=row["category_scores"]["basic_qa"],
                lat=row["avg_latency_s"],
                p95=row["p95_latency_s"],
                tps=row["avg_tokens_per_s"],
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    args = parse_args()
    models = load_json(args.models_file)
    cases = load_json(args.cases_file)
    cases = limit_cases(cases, args.max_cases_per_category)

    wanted = {m.strip() for m in args.models.split(",") if m.strip()}
    if wanted:
        models = [m for m in models if m["id"] in wanted]
    if not models:
        raise SystemExit("No models selected.")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d-%H%M%S")

    all_model_results: list[dict[str, Any]] = []

    def run_all() -> None:
        for model_cfg in models:
            print(
                f"\n=== Benchmarking {model_cfg['label']} ({model_cfg['id']}) ===",
                flush=True,
            )
            if not args.no_switch_models:
                print(
                    "Switching deployment model and waiting for rollout...", flush=True
                )
                switch_model(args.namespace, model_cfg, args.rollout_timeout)

            print("Warming endpoint...", flush=True)
            warm_endpoint(args.base_url, args.model, args.timeout, args.rollout_timeout)

            case_results: list[dict[str, Any]] = []
            latencies: list[float] = []
            token_rates: list[float] = []

            for idx, case in enumerate(cases, start=1):
                print(f"[{idx:02d}/{len(cases)}] {case['id']}", flush=True)
                response, latency = call_llm_with_retry(
                    base_url=args.base_url,
                    model=args.model,
                    prompt=case["prompt"],
                    max_tokens=int(case.get("max_tokens", 256)),
                    temperature=args.temperature,
                    timeout=args.timeout,
                    retries=3,
                    retry_delay_s=2.0,
                )
                score, note, content = score_case(case, response)

                usage = response.get("usage") or {}
                completion_tokens = usage.get("completion_tokens")
                tokens_per_s = None
                if isinstance(completion_tokens, (int, float)) and latency > 0:
                    tokens_per_s = float(completion_tokens) / latency
                    token_rates.append(tokens_per_s)
                latencies.append(latency)

                case_results.append(
                    {
                        "id": case["id"],
                        "category": case["category"],
                        "score": score,
                        "note": note,
                        "latency_s": latency,
                        "completion_tokens": completion_tokens,
                        "tokens_per_s": tokens_per_s,
                        "response_excerpt": content[:220],
                    }
                )

            categories = category_means(case_results)
            accuracy_score = score_accuracy(categories)
            avg_latency = statistics.mean(latencies) if latencies else 0.0
            p95_latency = percentile(sorted(latencies), 0.95) if latencies else 0.0
            avg_tps = statistics.mean(token_rates) if token_rates else 0.0

            all_model_results.append(
                {
                    "model_id": model_cfg["id"],
                    "label": model_cfg["label"],
                    "category_scores": categories,
                    "accuracy_score": accuracy_score,
                    "avg_latency_s": avg_latency,
                    "p95_latency_s": p95_latency,
                    "avg_tokens_per_s": avg_tps,
                    "cases": case_results,
                }
            )

    if args.no_port_forward:
        run_all()
    else:
        with PortForward(args.namespace, args.local_port):
            run_all()

    latencies = [r["avg_latency_s"] for r in all_model_results]
    tps_vals = [r["avg_tokens_per_s"] for r in all_model_results]
    best_lat, worst_lat = min(latencies), max(latencies)
    low_tps, high_tps = min(tps_vals), max(tps_vals)

    for row in all_model_results:
        latency_component = normalize_inverse(row["avg_latency_s"], best_lat, worst_lat)
        tps_component = normalize_direct(row["avg_tokens_per_s"], low_tps, high_tps)
        row["speed_score"] = 100.0 * (0.70 * latency_component + 0.30 * tps_component)
        row["overall_score"] = 0.60 * row["accuracy_score"] + 0.40 * row["speed_score"]

    ranked = sorted(all_model_results, key=lambda x: x["overall_score"], reverse=True)

    result_doc = {
        "generated_at_utc": dt.datetime.now(dt.UTC).isoformat(),
        "weights": {
            "overall": {"accuracy": 0.60, "speed": 0.40},
            "accuracy": {"codegen": 0.50, "tech_qa": 0.30, "basic_qa": 0.20},
            "speed": {"avg_latency_s": 0.70, "avg_tokens_per_s": 0.30},
        },
        "models": ranked,
    }

    json_path = output_dir / f"benchmark-{stamp}.json"
    md_path = output_dir / f"benchmark-{stamp}.md"
    json_path.write_text(json.dumps(result_doc, indent=2) + "\n", encoding="utf-8")
    write_markdown(md_path, ranked)

    print("\n=== Final Ranking ===", flush=True)
    for idx, row in enumerate(ranked, start=1):
        print(
            f"{idx}. {row['label']} | overall={row['overall_score']:.2f} "
            f"accuracy={row['accuracy_score']:.2f} speed={row['speed_score']:.2f}",
            flush=True,
        )
    print(f"\nWrote: {json_path}", flush=True)
    print(f"Wrote: {md_path}", flush=True)


if __name__ == "__main__":
    main()
