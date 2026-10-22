#!/usr/bin/env python3
"""Validate the re-curated ground truth against the live compendium vault.

Phase 0 gate for TASK-1122: every non-special question must point at a
vault_path that resolves today. Run before any scoring pass.

Usage:
    python3 validate_groundtruth.py [--vault ~/code/compendium] [groundtruth.json]

Exit 0 if every vault_path resolves; exit 1 (with a report) otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

DEFAULT_GT = Path(__file__).with_name("eval_groundtruth_2026-07-04.json")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("groundtruth", nargs="?", default=str(DEFAULT_GT))
    ap.add_argument("--vault", default=os.path.expanduser("~/code/compendium"))
    args = ap.parse_args()

    vault = Path(os.path.expanduser(args.vault))
    data = json.loads(Path(args.groundtruth).read_text())
    questions = data["questions"]

    missing: list[tuple[str, str]] = []
    checked = 0
    special = 0

    for q in questions:
        path = q.get("vault_path")
        frag = q.get("match_fragment")
        # Special rows carry no single vault_path: negatives (NONE) and the
        # report-any-bug row (bug-).
        if path is None:
            special += 1
            continue
        checked += 1
        if not (vault / path).is_file():
            missing.append((q["qid"], path))

    counts = data.get("counts", {})
    print(f"ground truth: {args.groundtruth}")
    print(f"vault:        {vault}")
    print(
        f"questions:    {len(questions)} "
        f"(declared total={counts.get('total', '?')}, "
        f"positive={counts.get('positive', '?')}, "
        f"negative={counts.get('negative', '?')})"
    )
    print(f"path-checked: {checked}   special (no path): {special}")

    if missing:
        print(f"\nFAIL — {len(missing)} vault_path(s) do not resolve:")
        for qid, path in missing:
            print(f"  {qid}: {path}")
        return 1

    print("\nOK — every vault_path resolves against the live vault.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
