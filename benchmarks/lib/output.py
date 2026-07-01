"""Structured result writers for the benchmarking harness.

Every harness writes a JSON bundle that conforms to
`benchmarks/results-schema.json`, plus a CSV summary and a Markdown table for
quick paste into compendium items.
"""

from __future__ import annotations

import csv
import json
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


SCHEMA_VERSION = "1.0.0"


@dataclass
class Percentiles:
    p50: float
    p90: float
    p95: float
    p99: float
    mean: float
    min: float
    max: float


def percentiles(values: list[float]) -> Percentiles:
    """Compute P50/P90/P95/P99 plus mean/min/max for a list of floats."""
    if not values:
        return Percentiles(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    sorted_values = sorted(values)
    n = len(sorted_values)

    def _pct(p: float) -> float:
        idx = (n - 1) * p
        low = int(idx)
        high = min(low + 1, n - 1)
        weight = idx - low
        return sorted_values[low] * (1 - weight) + sorted_values[high] * weight

    return Percentiles(
        p50=_pct(0.50),
        p90=_pct(0.90),
        p95=_pct(0.95),
        p99=_pct(0.99),
        mean=statistics.mean(sorted_values),
        min=sorted_values[0],
        max=sorted_values[-1],
    )


def _percentiles_as_dict(p: Percentiles) -> dict[str, float]:
    return {
        "p50": round(p.p50, 4),
        "p90": round(p.p90, 4),
        "p95": round(p.p95, 4),
        "p99": round(p.p99, 4),
        "mean": round(p.mean, 4),
        "min": round(p.min, 4),
        "max": round(p.max, 4),
    }


def build_meta(tool: str, version: Optional[str] = None) -> dict[str, Any]:
    """Build the common `meta` block for a result bundle."""
    import subprocess

    commit = "unknown"
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        pass

    return {
        "tool": tool,
        "version": version or "unknown",
        "schema_version": SCHEMA_VERSION,
        "timestamp": _iso_now(),
        "commit": commit,
    }


def _iso_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def write_json(
    path: Path,
    meta: dict[str, Any],
    config: dict[str, Any],
    samples: list[dict[str, Any]],
    summary: dict[str, Any],
    gpu_series: Optional[list[dict[str, Any]]] = None,
) -> None:
    """Write a JSON result bundle conforming to results-schema.json."""
    payload = {
        "meta": meta,
        "config": config,
        "samples": samples,
        "summary": summary,
    }
    if gpu_series is not None:
        payload["gpu_series"] = gpu_series
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    """Write a CSV summary. Keys come from the first row."""
    if not rows:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def markdown_table(rows: list[dict[str, Any]]) -> str:
    """Render a Markdown table from a list of dicts."""
    if not rows:
        return ""
    headers = list(rows[0].keys())
    lines = ["| " + " | ".join(headers) + " |"]
    lines.append("| " + " | ".join(["---"] * len(headers)) + " |")
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(h, "")) for h in headers) + " |")
    return "\n".join(lines)


def latency_summary(
    values: list[float],
    name: str,
) -> dict[str, Any]:
    """Return a standard latency summary dict with percentiles."""
    p = percentiles(values)
    return {
        "metric": name,
        **_percentiles_as_dict(p),
        "unit": "s",
        "count": len(values),
    }


def throughput_summary(
    generated_tokens: list[int],
    wall_seconds: list[float],
) -> dict[str, Any]:
    """Return aggregate throughput summary across multiple requests."""
    total_tokens = sum(generated_tokens)
    total_wall = sum(wall_seconds)
    per_request = [
        tok / wall if wall > 0 else 0.0
        for tok, wall in zip(generated_tokens, wall_seconds)
    ]
    p = percentiles(per_request) if per_request else Percentiles(0, 0, 0, 0, 0, 0, 0)
    return {
        "metric": "throughput",
        "total_tokens": total_tokens,
        "total_wall_s": round(total_wall, 4),
        "aggregate_tok_s": round(total_tokens / total_wall, 2)
        if total_wall > 0
        else 0.0,
        **_percentiles_as_dict(p),
        "unit": "tok/s",
        "count": len(per_request),
    }
