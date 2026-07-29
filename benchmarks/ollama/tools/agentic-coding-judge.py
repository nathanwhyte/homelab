#!/usr/bin/env python3
"""Style scoring for agentic-coding results (PROJ-1003) — SECONDARY signal only.

`agentic-coding-bench.py` answers "did it work" with a hidden verifier. This
answers "was the change well made", which no test can capture: minimality of the
diff, whether the fix addresses the cause or papers over the symptom, and whether
the docstring's stated contract survived.

⚠ READ BEFORE QUOTING ANY NUMBER FROM THIS SCRIPT

  * It only judges solutions that ALREADY PASSED their verifier. A high style
    score on unsolved work is meaningless, so it is never computed.
  * The judge is a local model, and local judges have a recorded failure history
    in this homelab: TASK-1169's precision collapsed from 0.90 in-sample to 0.067
    out-of-sample at low prevalence. Treat these scores as advisory ordering, not
    measurement, and never let them override a verifier result.
  * The default judge (qwen3.6:subagent) is a sibling of two tags under test. It
    is judging its own family, and that bias is not corrected for anywhere.
  * The judge must run on a GGUF tag: Ollama's MLX engine silently drops the
    `format` schema (INFO-1127), returning prose with HTTP 200. This script
    validates the response shape and reports drops rather than parsing prose.

Usage:
    python3 agentic-coding-judge.py results/agentic-coding-*.json
    python3 agentic-coding-judge.py --judge qwen3.6:subagent results/*.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def _sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return "unavailable"


DEFAULT_JUDGE = "qwen3.6:subagent"
DEFAULT_BASE = "http://localhost:11434"

RUBRIC_SCHEMA = {
    "type": "object",
    "properties": {
        "minimality": {"type": "integer"},
        "root_cause": {"type": "integer"},
        "contract_kept": {"type": "integer"},
        "note": {"type": "string"},
    },
    "required": ["minimality", "root_cause", "contract_kept", "note"],
}

PROMPT = """You are reviewing one change a coding agent made. The change is already
known to be functionally correct — its hidden tests pass. Judge only HOW it was made.

Task given to the agent:
{prompt}

Original file ({path}):
```python
{original}
```

Final file after the agent's edit:
```python
{final}
```

Score three axes, each an integer 1-5:
- minimality: 5 = only what the task required; 1 = sprawling unrelated rewrites,
  renamed public names, or gratuitous restructuring.
- root_cause: 5 = fixes the underlying cause; 1 = special-cases the symptom or
  hardcodes the tested values.
- contract_kept: 5 = the documented behaviour and signature still hold; 1 = the
  docstring now lies, or callers would break.

Add a note of at most 20 words. Respond with JSON only."""


def post(
    base: str, payload: dict[str, Any], timeout: int = 300
) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(
        f"{base}/api/chat",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, {"error": (e.read() or b"").decode(errors="replace")[:300]}
    except Exception as e:  # noqa: BLE001
        return 0, {"error": f"{type(e).__name__}: {e}"}


def _valid_rubric(parsed: Any) -> bool:
    """Accept only well-formed rubric objects.

    Checking that three keys exist is not validation: a response carrying
    minimality=99, root_cause="five", contract_kept=-1 satisfied that and was
    averaged into the means. Scores outside 1-5 silently distort every aggregate
    they touch, so they are dropped as unparseable instead.
    """
    if not isinstance(parsed, dict):
        return False
    axes = ("minimality", "root_cause", "contract_kept")
    if set(parsed) - {*axes, "note"}:
        return False
    for axis in axes:
        value = parsed.get(axis)
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 5:
            return False
    return isinstance(parsed.get("note", ""), str)


def judge_one(
    base: str,
    judge: str,
    task_prompt: str,
    path: str,
    original: str,
    final: str,
) -> dict[str, Any] | None:
    content = PROMPT.format(
        prompt=task_prompt, path=path, original=original.strip(), final=final.strip()
    )
    status, body = post(
        base,
        {
            "model": judge,
            "messages": [{"role": "user", "content": content}],
            "stream": False,
            "think": False,
            "format": RUBRIC_SCHEMA,
            "keep_alive": "10m",
            "options": {
                "temperature": 0,
                "num_ctx": 16384,
                "num_predict": 256,
                "seed": 42,
            },
        },
    )
    if status != 200:
        return None
    raw = ((body.get("message") or {}).get("content") or "").strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # The MLX-drops-`format` failure mode looks exactly like this.
        return None
    return parsed if _valid_rubric(parsed) else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "results", nargs="+", help="JSON files written by agentic-coding-bench.py"
    )
    ap.add_argument("--judge", default=DEFAULT_JUDGE)
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument(
        "--force",
        action="store_true",
        help="judge even when the result's fixtures_sha256 no longer matches",
    )
    ap.add_argument(
        "--out", default=None, help="write judged JSON here (default: alongside input)"
    )
    args = ap.parse_args()

    if args.out and len(args.results) > 1 and not Path(args.out).is_dir():
        ap.error(
            "--out with multiple result files must name an existing directory; "
            "a single path would have every input overwrite the last"
        )

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from coding_tasks import (
        TASKS,
    )

    by_id = {t.task_id: t for t in TASKS}

    current_fixtures = _sha256(
        Path(__file__).resolve().parent / "coding_tasks" / "__init__.py"
    )

    for result_path in args.results:
        path = Path(result_path)
        data = json.loads(path.read_text())
        model = data.get("model")

        # Judging compares each final file against the fixture's ORIGINAL. If the
        # fixtures moved since the run, that original is the wrong baseline and
        # the scores are quietly meaningless.
        recorded = (data.get("provenance") or {}).get("fixtures_sha256")
        if recorded and recorded != current_fixtures and not args.force:
            print(
                f"\n=== {model} ({path.name}) ===\n"
                f"  SKIPPED: fixtures changed since this run "
                f"(recorded {recorded}, current {current_fixtures}). "
                f"Re-run the benchmark, or pass --force to judge anyway."
            )
            continue
        judged: list[dict[str, Any]] = []
        drops = 0

        for row in data.get("results", []):
            if not row.get("solved"):
                continue
            task = by_id.get(row["task_id"])
            if task is None:
                continue
            for filename, final_src in (row.get("final_sources") or {}).items():
                original = task.files.get(filename, "")
                verdict = judge_one(
                    args.base, args.judge, task.prompt, filename, original, final_src
                )
                if verdict is None:
                    drops += 1
                    continue
                judged.append(
                    {
                        "task_id": row["task_id"],
                        "tier": row["tier"],
                        "repeat": row.get("repeat", 0),
                        "file": filename,
                        **verdict,
                    }
                )

        if judged:
            means = {
                axis: round(statistics.mean(j[axis] for j in judged), 2)
                for axis in ("minimality", "root_cause", "contract_kept")
            }
        else:
            means = {}

        print(f"\n=== {model} ({path.name}) ===")
        print(f"judged {len(judged)} solved change(s), {drops} unparseable response(s)")
        if means:
            print(f"  means: {means}")
            for j in judged:
                print(
                    f"  T{j['tier']} {j['task_id']:<26} min={j['minimality']} "
                    f"cause={j['root_cause']} contract={j['contract_kept']}  {j['note'][:60]}"
                )
        if drops:
            print(
                f"  ⚠ {drops} response(s) were not valid JSON — if the judge tag is "
                f"MLX-backed, that is INFO-1127 (schema silently dropped), not a model failure"
            )

        if args.out:
            out_dir = Path(args.out)
            out_path = (
                out_dir / f"{path.stem}.judged.json" if out_dir.is_dir() else out_dir
            )
        else:
            out_path = path.with_suffix(".judged.json")
        out_path.write_text(
            json.dumps(
                {
                    "model": model,
                    "judge": args.judge,
                    "judge_caveat": (
                        "advisory only; local-judge precision collapsed out-of-sample "
                        "in TASK-1169, and the default judge is a sibling of tags under test"
                    ),
                    "means": means,
                    "unparseable": drops,
                    "judged": judged,
                },
                indent=2,
            )
        )
        print(f"  wrote {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
