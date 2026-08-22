#!/usr/bin/env python3
"""Post-run quality gate for concurrency-bench.py output (PROJ-1003, TASK-1190).

WHY THIS EXISTS. concurrency-bench.py records request errors, empty answers,
and truncations, then returns 0 unconditionally (`return 0` at the end of
main). A wrapper script therefore cannot tell a healthy arm from one whose
300-second requests all failed or whose 16384-token responses all came back
empty — both exit 0 and both write a results file.

This gate reads the JSON that run produced and fails the arm when the recorded
quality counters breach explicit thresholds. It is deliberately a SEPARATE
script rather than a change to concurrency-bench.py's return value, because
several existing runners (run-pop-qwen38-matrix.sh, run-pop-moe-matrix.sh,
run-cluster-np-sweep.sh, ...) depend on the current exit semantics and would
start failing on historically-tolerated noise.

Usage:
    bench-quality-gate.py <results.json> [--max-error-rate 0.0]
                                         [--max-empty-rate 0.05]
                                         [--max-truncation-rate 0.25]

Exit 0 = all thresholds satisfied.
Exit 1 = at least one threshold breached (details on stdout).
Exit 2 = not gateable — missing file, invalid JSON, or an artifact with no
         per-level quality block (a prefill or FIM result, say). Distinct from
         exit 1 on purpose: pointing the gate at the wrong file is an operator
         error, not a bad benchmark.

THRESHOLD CALIBRATION. Defaults come from the 89 result files in
benchmarks/results, of which 33 are concurrency-bench shaped:

    errors      0 nonzero runs out of 33  -> default 0.0, any error is anomalous
    empty       6 nonzero runs, up to 82% -> default 0.05
    truncation 10 nonzero runs, up to 100% -> default 0.25, most config-sensitive

Against that corpus these defaults pass 23, fail 10, and report 56 as N/A. All
ten failures are the vulkan cluster runs carrying 53-100% empty answers, i.e.
exactly the runs that should never have been read as results.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _levels(doc):
    """Extract the per-level list from a concurrency-bench results.json.

    The real shape is doc["summary"]["levels"], each entry carrying a
    "quality" block from LevelResult._quality_summary(). Older/looser shapes
    are tolerated so this gate can also be pointed at hand-assembled files.
    """
    if isinstance(doc, list):
        return doc
    summary = doc.get("summary")
    if isinstance(summary, dict) and isinstance(summary.get("levels"), list):
        return summary["levels"]
    for key in ("levels", "results", "level_results", "raw"):
        v = doc.get(key)
        if isinstance(v, list):
            return v
    return None


def _num(level, *names):
    """Read a counter from the level's quality block, falling back to top level."""
    sources = []
    q = level.get("quality")
    if isinstance(q, dict):
        sources.append(q)
    sources.append(level)
    for src in sources:
        for n in names:
            v = src.get(n)
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)):
                return v
            if isinstance(v, list):
                return len(v)
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("results", type=Path)
    ap.add_argument(
        "--max-error-rate",
        type=float,
        default=0.0,
        help="all 33 historical concurrency-bench runs recorded zero request errors, "
        "so any error at all is anomalous",
    )
    ap.add_argument(
        "--max-empty-rate",
        type=float,
        default=0.05,
        help="calibrated against 33 historical concurrency-bench runs: empties appear in 6, "
        "all of them known-bad cluster runs (up to 82%%); healthy pop runs sit at 0%%",
    )
    ap.add_argument(
        "--max-truncation-rate",
        type=float,
        default=0.25,
        help="truncation is expected at a low rate on long agentic prompts; a high rate means "
        "the num_predict budget, not the model, decided the answer. Historical runs range 0%% to "
        "100%%, so this threshold is the one most likely to need per-config tuning",
    )
    ap.add_argument("--min-attempted", type=int, default=1)
    args = ap.parse_args()

    if not args.results.is_file():
        print(f"QUALITY GATE: results file not found: {args.results}")
        return 2
    try:
        doc = json.loads(args.results.read_text())
    except json.JSONDecodeError as e:
        print(f"QUALITY GATE: {args.results} is not valid JSON: {e}")
        return 2

    levels = _levels(doc)
    if levels is None:
        print(
            f"QUALITY GATE: N/A — {args.results} has no recognizable per-level results "
            "block, so it is not a concurrency-bench result. Nothing to gate."
        )
        return 2
    totals = {"attempted": 0, "errors": 0, "empty": 0, "truncated": 0}
    saw_quality = False
    for lv in levels:
        if not isinstance(lv, dict):
            continue
        if isinstance(lv.get("quality"), dict):
            saw_quality = True
        totals["attempted"] += _num(lv, "attempted")
        totals["errors"] += _num(lv, "failed", "errors", "error_count")
        totals["empty"] += _num(lv, "empty_answers", "empty", "empty_count")
        totals["truncated"] += _num(lv, "truncated", "truncated_answers", "truncations")

    # Distinguish "not a concurrency-bench result" from "a bad one". Pointing
    # this gate at a prefill or FIM artifact is an operator mistake, not a
    # quality failure, and reporting it as FAIL would train people to ignore
    # the gate. Exit 2 keeps it loud but categorically different.
    if not saw_quality:
        print(
            f"QUALITY GATE: N/A — {args.results} carries no per-level 'quality' block, so it is "
            "not a concurrency-bench result. Nothing to gate."
        )
        return 2

    attempted = totals["attempted"]
    if attempted < args.min_attempted:
        print(
            f"QUALITY GATE: FAIL — only {attempted} attempted request(s) recorded "
            f"(minimum {args.min_attempted}). The arm did not really run."
        )
        return 1

    rates = {
        "error": totals["errors"] / attempted,
        "empty": totals["empty"] / attempted,
        "truncation": totals["truncated"] / attempted,
    }
    limits = {
        "error": args.max_error_rate,
        "empty": args.max_empty_rate,
        "truncation": args.max_truncation_rate,
    }

    counter_key = {"error": "errors", "empty": "empty", "truncation": "truncated"}

    print(f"QUALITY GATE: {args.results.name} — {attempted} attempted")
    breached = []
    for k in ("error", "empty", "truncation"):
        ok = rates[k] <= limits[k]
        print(
            f"  {k:>10}: {totals[counter_key[k]]:>4} "
            f"({rates[k]:.1%})  limit {limits[k]:.1%}  {'ok' if ok else 'BREACH'}"
        )
        if not ok:
            breached.append(k)

    if breached:
        print(
            f"QUALITY GATE: FAIL — {', '.join(breached)} above threshold. "
            "These numbers are not usable as a benchmark result."
        )
        return 1
    print("QUALITY GATE: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
