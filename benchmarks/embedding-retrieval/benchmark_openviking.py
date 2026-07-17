#!/usr/bin/env python3
"""Evaluate the frozen retrieval ground truth against a live OpenViking index.

Run once before an embedder backend change to capture a baseline, then again
after the change with --baseline. The corpus vectors remain untouched, so the
comparison measures whether queries from the new backend are compatible with
the vectors already stored by the old backend.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


HERE = Path(__file__).parent
DEFAULT_GROUND_TRUTH = HERE / "eval_groundtruth_2026-07-04.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True, help="Run label, e.g. rocm or vulkan")
    parser.add_argument("--output", required=True, type=Path, help="JSON result path")
    parser.add_argument(
        "--baseline",
        type=Path,
        help="Baseline JSON to compare with; omit when capturing the baseline",
    )
    parser.add_argument(
        "--ground-truth",
        type=Path,
        default=DEFAULT_GROUND_TRUTH,
        help="Frozen ground-truth JSON",
    )
    parser.add_argument(
        "--max-regression-queries",
        type=int,
        default=1,
        help="Maximum allowed additional top-1 or top-5 misses (default: 1)",
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("OPENVIKING_URL", "http://192.168.1.19:31933"),
        help="OpenViking base URL (default: OPENVIKING_URL or LAN NodePort)",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("OPENVIKING_KEY"),
        help="OpenViking API key (default: OPENVIKING_KEY)",
    )
    return parser.parse_args()


def search(url: str, api_key: str, query: str) -> list[dict]:
    request = Request(
        f"{url.rstrip('/')}/api/v1/search/search",
        data=json.dumps({"query": query, "limit": 5}).encode(),
        headers={
            "Content-Type": "application/json",
            "X-API-Key": api_key,
            "X-OpenViking-Account": "default",
            "X-OpenViking-User": "noot",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=60) as response:
            payload = json.load(response)
    except HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise RuntimeError(f"search returned HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"search request failed: {exc.reason}") from exc

    if payload.get("status") != "ok":
        raise RuntimeError(f"search returned an error: {payload}")

    result = payload.get("result", {})
    return [
        item
        for section in ("resources", "memories", "skills")
        for item in result.get(section, [])
    ][:5]


def summarize(rows: list[dict]) -> dict[str, int | float]:
    positives = [row for row in rows if row["expected"] != "NONE"]
    top1_hits = sum(row["top1_hit"] for row in positives)
    top5_hits = sum(row["top5_hit"] for row in positives)
    total = len(positives)
    return {
        "positive_queries": total,
        "top1_hits": top1_hits,
        "top5_hits": top5_hits,
        "top1_pct": round(100 * top1_hits / total, 1),
        "top5_pct": round(100 * top5_hits / total, 1),
    }


def compare(current: dict, baseline: dict, max_regression: int) -> list[str]:
    failures = []
    current_metrics = current["metrics"]
    baseline_metrics = baseline["metrics"]

    if current["ground_truth_sha256"] != baseline.get("ground_truth_sha256"):
        failures.append("baseline and current runs use different ground truth")

    if current_metrics["positive_queries"] != baseline_metrics["positive_queries"]:
        failures.append("baseline and current runs contain different query counts")

    for metric in ("top1_hits", "top5_hits"):
        drop = baseline_metrics[metric] - current_metrics[metric]
        if drop > max_regression:
            failures.append(
                f"{metric} regressed by {drop} queries "
                f"(allowed: {max_regression}; "
                f"baseline={baseline_metrics[metric]}, current={current_metrics[metric]})"
            )
    return failures


def main() -> int:
    args = parse_args()
    if not args.api_key:
        print("error: set OPENVIKING_KEY or pass --api-key", file=sys.stderr)
        return 2

    ground_truth = json.loads(args.ground_truth.read_text())
    rows = []
    for question in ground_truth["questions"]:
        try:
            results = search(args.url, args.api_key, question["question"])
        except RuntimeError as exc:
            print(f"error: {question['qid']}: {exc}", file=sys.stderr)
            return 2

        uris = [item.get("uri", "") for item in results]
        expected = question["match_fragment"]
        top1_hit = expected != "NONE" and bool(uris) and expected in uris[0].lower()
        top5_hit = expected != "NONE" and any(expected in uri.lower() for uri in uris)
        rows.append(
            {
                "qid": question["qid"],
                "category": question["category"],
                "question": question["question"],
                "expected": expected,
                "top1_hit": top1_hit,
                "top5_hit": top5_hit,
                "results": [
                    {"uri": item.get("uri"), "score": item.get("score")}
                    for item in results
                ],
            }
        )
        print(
            f"{question['qid']:>3} "
            f"top1={'yes' if top1_hit else 'no ':>3} "
            f"top5={'yes' if top5_hit else 'no ':>3}"
        )

    output = {
        "label": args.label,
        "created_at": datetime.now(UTC).isoformat(),
        "url": args.url,
        "ground_truth": str(args.ground_truth),
        "ground_truth_sha256": hashlib.sha256(
            args.ground_truth.read_bytes()
        ).hexdigest(),
        "metrics": summarize(rows),
        "queries": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2) + "\n")
    print(json.dumps(output["metrics"], indent=2))
    print(f"wrote {args.output}")

    if not args.baseline:
        return 0

    baseline = json.loads(args.baseline.read_text())
    failures = compare(output, baseline, args.max_regression_queries)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1

    print(
        "PASS: top-1 and top-5 regression are within "
        f"{args.max_regression_queries} query"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
