#!/usr/bin/env python3
"""Summarize saved benchmark evidence without issuing inference requests."""

import argparse
import hashlib
import json
import math
import statistics
from pathlib import Path


def rows(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def percentiles(values):
    values = sorted(values)
    return (
        {
            f"p{p}": values[max(0, math.ceil(len(values) * p / 100) - 1)]
            for p in (50, 95, 99)
        }
        if values
        else {}
    )


def cosine(a, b):
    if len(a) != len(b):
        raise ValueError("Cannot compare vectors with different dimensions")
    return sum(x * y for x, y in zip(a, b)) / math.sqrt(
        sum(x * x for x in a) * sum(y * y for y in b)
    )


def compare_inputs_and_vectors(directory, name, reference, overrides):
    result = {}
    for filename in ("vectors.jsonl", "queries.jsonl", "sequential.jsonl"):
        actual = {
            r["key"]: r for r in rows(overrides.get(name, directory / name) / filename)
        }
        baseline = {
            r["key"]: r
            for r in rows(overrides.get(reference, directory / reference) / filename)
        }
        shared = actual.keys() & baseline.keys()
        mismatches = sorted(
            key
            for key in shared
            if actual[key]["input_sha256"] != baseline[key]["input_sha256"]
        )
        compared = {
            "actual_count": len(actual),
            "reference_count": len(baseline),
            "shared_count": len(shared),
            "input_mismatches": mismatches,
            "missing_keys": sorted(baseline.keys() - actual.keys()),
            "extra_keys": sorted(actual.keys() - baseline.keys()),
        }
        if name.startswith("06b") and reference.startswith("06b") and shared:
            similarities = [
                cosine(actual[key]["vector"], baseline[key]["vector"])
                for key in shared
                if key not in mismatches
            ]
            if similarities:
                compared["cosine"] = {
                    "minimum": min(similarities),
                    "median": statistics.median(similarities),
                    "maximum": max(similarities),
                }
        result[filename] = compared
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("directory", type=Path)
    ap.add_argument("--sample", type=Path, required=True)
    ap.add_argument("--output", type=Path)
    ap.add_argument("--arm-directory", action="append", default=[], metavar="ARM=PATH")
    args = ap.parse_args()
    overrides = {}
    for value in args.arm_directory:
        name, path = value.split("=", 1)
        if name not in ("4b-s1", "06b-s1", "06b-s2", "06b-s4"):
            ap.error(f"Unknown arm: {name}")
        overrides[name] = Path(path)
    sample = json.loads(args.sample.read_text())
    result = {"arms": {}, "percentile_method": "nearest rank"}
    for name in ("4b-s1", "06b-s1", "06b-s2", "06b-s4"):
        path = overrides.get(name, args.directory / name)
        if not path.exists():
            continue
        summary = (
            json.loads((path / "summary.json").read_text())
            if (path / "summary.json").exists()
            else {}
        )
        sequential = rows(path / "sequential.jsonl")
        telemetry = [r for r in rows(path / "telemetry.jsonl") if "cpu_c" in r]
        arm = {
            "source_directory": str(path),
            "completed": "completed_at" in summary,
            "quality_skipped": summary.get("quality_skipped", False),
            "sequential_count": len(sequential),
            "buckets": {},
            "throughput": {},
        }
        for target in sorted({r["target"] for r in sample}):
            selected = [r for r in sequential if sample[r["key"]]["target"] == target]
            times = [r["latency_ms"] for r in selected]
            arm["buckets"][target] = {
                "n": len(times),
                "median_ms": statistics.median(times) if times else None,
                **percentiles(times),
            }
        for concurrency in (1, 2, 4):
            phases = [
                r
                for r in summary.get("phases", [])
                if r["phase"] == "throughput" and r["concurrency"] == concurrency
            ]
            timings = [
                r["latency_ms"]
                for repeat in range(3)
                for r in rows(path / f"c{concurrency}-r{repeat}.jsonl")
            ]
            arm["throughput"][concurrency] = {
                "completed_passes": len(phases),
                "tokens_per_second": [r["response_tokens_per_second"] for r in phases],
                "requests_per_second": [r["requests_per_second"] for r in phases],
                "latency_ms": percentiles(timings),
            }
            rates = [r["response_tokens_per_second"] for r in phases]
            if rates:
                arm["throughput"][concurrency]["median_tokens_per_second"] = (
                    statistics.median(rates)
                )
            arm["throughput"][concurrency]["by_target_latency_ms"] = {
                target: percentiles(
                    [
                        r["latency_ms"]
                        for repeat in range(3)
                        for r in rows(path / f"c{concurrency}-r{repeat}.jsonl")
                        if sample[r["key"]]["target"] == target
                    ]
                )
                for target in sorted({r["target"] for r in sample})
            }
        if telemetry:
            arm["resources"] = {
                key: max(r[key] for r in telemetry)
                for key in ("cpu_c", "gpu_c", "vram_mib", "rss_kib", "hwm_kib")
            }
        if (path / "mixed.json").exists():
            arm["mixed"] = json.loads((path / "mixed.json").read_text())
        if (path / "quality.json").exists():
            arm["quality"] = json.loads((path / "quality.json").read_text())
        arm["artifact_hashes"] = {
            f.name: hashlib.sha256(f.read_bytes()).hexdigest()
            for f in path.iterdir()
            if f.is_file() and f.suffix in (".json", ".jsonl", ".log")
        }
        result["arms"][name] = arm
    for name, arm in result["arms"].items():
        arm["inputs_vs_4b"] = compare_inputs_and_vectors(
            args.directory, name, "4b-s1", overrides
        )
        if name.startswith("06b"):
            arm["vectors_vs_06b_s1"] = compare_inputs_and_vectors(
                args.directory, name, "06b-s1", overrides
            )
    control = result["arms"].get("4b-s1", {}).get("quality", {}).get("per_query", [])
    if control:
        control = {r["qid"]: r for r in control if r["expected"] != "NONE"}
        for arm in result["arms"].values():
            quality = arm.get("quality", {})
            if "per_query" not in quality:
                continue
            paired = {
                "top1_gain": [],
                "top1_loss": [],
                "top5_gain": [],
                "top5_loss": [],
            }
            for row in quality["per_query"]:
                if row["qid"] not in control:
                    continue
                baseline = control[row["qid"]]
                for n in (1, 5):
                    hit = any(row["expected"] in item[0] for item in row["top5"][:n])
                    old = any(
                        baseline["expected"] in item[0] for item in baseline["top5"][:n]
                    )
                    if hit != old:
                        paired[f"top{n}_{'gain' if hit else 'loss'}"].append(row["qid"])
            quality["paired_vs_4b"] = paired
    if args.output:
        args.output.write_text(json.dumps(result, indent=2) + "\n")
    else:
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
