#!/usr/bin/env python3
"""Export the compendium corpus to corpus.jsonl for the embedder A/B (Phase 1).

One row per item entry: {entry_id, vault_path, text}. entry_id is the lowercased
ID (e.g. 'bug-1006') — the match_fragment the ground truth scores against. text is
the raw markdown (truncated at embed time to the 8192-token cap).

Usage: python3 export_corpus.py [--vault ~/code/compendium] [--out corpus.jsonl]
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
from pathlib import Path

ITEM_DIRS = ["bugs", "features", "ideas", "improvements", "projects", "tasks", "info", "errors", "guides"]
ID_RE = re.compile(r"^([A-Z]+-\d+)-", re.ASCII)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", default=os.path.expanduser("~/code/compendium"))
    ap.add_argument("--out", default=str(Path(__file__).with_name("corpus.jsonl")))
    args = ap.parse_args()

    vault = Path(os.path.expanduser(args.vault))
    seen: set[str] = set()
    rows = []
    for d in ITEM_DIRS:
        for f in glob.glob(str(vault / d / "**" / "*.md"), recursive=True):
            base = os.path.basename(f)
            m = ID_RE.match(base)
            if not m:
                continue
            entry_id = m.group(1).lower()
            if entry_id in seen:  # a moved/dup ID — keep first
                continue
            seen.add(entry_id)
            text = Path(f).read_text(encoding="utf-8", errors="ignore")
            rows.append(
                {
                    "entry_id": entry_id,
                    "vault_path": os.path.relpath(f, vault),
                    "text": text,
                }
            )

    with open(args.out, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    print(f"exported {len(rows)} entries -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
