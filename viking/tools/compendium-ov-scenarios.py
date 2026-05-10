#!/usr/bin/env python3
"""Run smoke scenarios for Compendium pointer entries in OpenViking."""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass


TARGET = "viking://resources/compendium"


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    command: list[str]
    expect_any: tuple[str, ...]
    should_succeed: bool = True


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        name="direct-kinde-roles",
        description="Direct bug lookup for Kinde role pagination.",
        command=["ov", "find", "-u", TARGET, "-n", "5", "kinde roles pagination"],
        expect_any=("bugs/bug-053.md", "Kinde roles"),
    ),
    Scenario(
        name="resolved-peoplease-duplicate",
        description="Resolved bug lookup for duplicate peoplease locations.",
        command=["ov", "find", "-u", TARGET, "-n", "5", "duplicate company info peoplease locations"],
        expect_any=("bugs/resolved/bug-005.md", "peoplease_locations"),
    ),
    Scenario(
        name="semantic-column-mapping",
        description="Semantic lookup spanning pipeline references and bug reports.",
        command=["ov", "find", "-u", TARGET, "-n", "5", "column mapping macro calculated columns"],
        expect_any=("info/pipeline-column-mappings.md", "bugs/bug-046.md", "pipeline-calculated-columns.md"),
    ),
    Scenario(
        name="cross-type-policy-extension",
        description="Cross-type lookup across tasks and reference docs.",
        command=["ov", "find", "-u", TARGET, "-n", "5", "policy extension stage 1 column mappings"],
        expect_any=("tasks/task-035.md", "tasks/task-032.md", "info/pipeline-column-mappings.md"),
    ),
    Scenario(
        name="snowflake-lookup-crash",
        description="Fuzzy lookup for a resolved Snowflake lookup crash.",
        command=["ov", "find", "-u", TARGET, "-n", "5", "snowflake crash lookup column spaces"],
        expect_any=("bugs/resolved/bug-044.md",),
    ),
    Scenario(
        name="active-exclusion",
        description="Active/todo BUG-000 should not exist after default resync.",
        command=["ov", "stat", f"{TARGET}/bugs/bug-000.md"],
        expect_any=("not found", "NotFound", "404", "No such"),
        should_succeed=False,
    ),
    Scenario(
        name="pointer-path",
        description="Pointer payload should expose a stable local markdown Path.",
        command=[
            "ov",
            "grep",
            "-u",
            f"{TARGET}/bugs/bug-053.md",
            "-n",
            "5",
            "Path: ~/code/compendium/bugs/BUG-053",
        ],
        expect_any=("Path: ~/code/compendium/bugs/BUG-053",),
    ),
    Scenario(
        name="tree-shape",
        description="Top-level Compendium namespaces should be present.",
        command=["ov", "tree", "-L", "3", "-n", "120", TARGET],
        expect_any=("bugs", "features", "ideas", "info", "plans", "tasks"),
    ),
)


def scenario_by_name(name: str) -> Scenario | None:
    for scenario in SCENARIOS:
        if scenario.name == name:
            return scenario
    return None


def run_scenario(scenario: Scenario) -> bool:
    print(f"\n=== {scenario.name} ===")
    print(scenario.description)
    print("$ " + " ".join(scenario.command))

    result = subprocess.run(scenario.command, capture_output=True, text=True, timeout=120)
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    if output.strip():
        print(output.rstrip())

    exit_ok = result.returncode == 0 if scenario.should_succeed else result.returncode != 0
    content_ok = any(expected in output for expected in scenario.expect_any)
    passed = exit_ok and content_ok

    print(f"RESULT: {'PASS' if passed else 'FAIL'}")
    if not exit_ok:
        expected = "success" if scenario.should_succeed else "failure"
        print(f"  exit check failed: expected {expected}, got code {result.returncode}")
    if not content_ok:
        print("  content check failed: none of these markers appeared:")
        for expected in scenario.expect_any:
            print(f"    - {expected}")
    return passed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="List scenario names")
    parser.add_argument("--run", nargs="+", metavar="NAME", help="Run scenario names, or 'all'")
    args = parser.parse_args()

    if args.list:
        for scenario in SCENARIOS:
            print(f"{scenario.name}\t{scenario.description}")
        return 0

    if not args.run:
        parser.error("choose --list or --run")

    selected: list[Scenario] = []
    if args.run == ["all"]:
        selected = list(SCENARIOS)
    else:
        for name in args.run:
            scenario = scenario_by_name(name)
            if scenario is None:
                print(f"unknown scenario: {name}", file=sys.stderr)
                return 2
            selected.append(scenario)

    passed = 0
    for scenario in selected:
        if run_scenario(scenario):
            passed += 1

    print(f"\nSummary: {passed}/{len(selected)} passed")
    return 0 if passed == len(selected) else 1


if __name__ == "__main__":
    raise SystemExit(main())
