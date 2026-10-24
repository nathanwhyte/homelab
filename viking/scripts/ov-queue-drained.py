"""Exit 0 only when every OpenViking queue reads Pending = In Progress = 0 (GUIDE-1085 drain gate).

Reads `ov status --output json` and parses its queue table with the exporter's own
`_parse_tables` (viking/exporters/ov-exporter.py), the parser production's
`openviking_queue_*` metrics already depend on, rather than a second, shell-side
reading of the same text (IMPR-1412 review: an awk gate passed on
`[queue] (unhealthy)` with no rows).

It refuses to call the queues drained unless the status's `ok` and its queue
component's `is_healthy` are both JSON `true`, the queue status is text holding
exactly one table with Pending and
In Progress columns, every row's two counts are integers, and every queue in
QUEUES is present (Semantic-Nodes included: `TOTAL` leaves it out, BUG-1181).

The server's own `is_healthy` is not part of the judgment. v0.5.1.1 clears it
whenever the vikingdb component's aggregate call 404s, a route the vectordb has
never served, so it reads false on every v0.5.1.1 server (IMPR-1412 rehearsal).
Every report carries an `unhealthy` list naming each component whose `is_healthy`
is not `true` (false, missing, or not an object), queue included.

Exit codes: 0 drained, 3 work remaining, 1 the status could not be judged
(bad arguments and unreadable input included). Prints one JSON line in every case.

Usage: python3 ov-queue-drained.py            (runs `ov status --output json`)
       python3 ov-queue-drained.py --file F   (judges a saved status JSON)
"""

import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

EXPORTERS = Path(__file__).resolve().parents[1] / "exporters"
QUEUES = (
    "Embedding",
    "Semantic",
    "ExternalParse",
    "AddResource",
    "SessionCommit",
    "ExternalTask",
    "UserDeletion",
    "Semantic-Nodes",
)


def _exporter():
    sys.path.insert(0, str(EXPORTERS))  # ov-exporter imports its sibling s3lite
    spec = importlib.util.spec_from_file_location(
        "ov_exporter", EXPORTERS / "ov-exporter.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def judge(status: dict) -> tuple[int, dict]:
    ex = _exporter()
    status = _dict(status)
    result = _dict(status.get("result"))
    components = _dict(result.get("components"))
    queue = _dict(components.get("queue"))
    unhealthy = sorted(
        n for n, c in components.items() if _dict(c).get("is_healthy") is not True
    )

    def refuse(error: str) -> tuple[int, dict]:
        return 1, {"error": error, "unhealthy": unhealthy}

    if not (status.get("ok") is True and queue.get("is_healthy") is True):
        return refuse("status not ok or queue component not healthy")
    text = queue.get("status")
    tables = ex._parse_tables(text if isinstance(text, str) else "")
    if (
        len(tables) != 1
        or not tables[0]
        or not {"Queue", "Pending", "In Progress"} <= set(tables[0][0])
    ):
        return refuse(f"expected one queue table, found {len(tables)}")
    counts = {}
    for row in tables[0]:
        name = row.get("Queue", "")
        if name == "TOTAL":
            continue
        p, i = ex._to_int(row.get("Pending")), ex._to_int(row.get("In Progress"))
        if p is None or i is None:
            return refuse(f"unreadable counts on row {name!r}")
        counts[name] = (p, i)
    missing = [q for q in QUEUES if q not in counts]
    if missing:
        return refuse(f"queue rows missing: {missing}")
    busy = {
        n: {"pending": p, "in_progress": i} for n, (p, i) in counts.items() if p or i
    }
    return (3 if busy else 0), {
        "drained": not busy,
        "queues": len(counts),
        "busy": busy,
        "unhealthy": unhealthy,
    }


class _Parser(argparse.ArgumentParser):
    """No --help: argparse would exit 0 after it, and 0 means drained."""

    def __init__(self):
        super().__init__(add_help=False)

    def error(self, message):
        print(json.dumps({"error": f"bad arguments: {message}"}))
        sys.exit(1)


def main() -> int:
    ap = _Parser()
    ap.add_argument("--file", type=Path)
    try:
        a = ap.parse_args()
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    try:
        if a.file:
            raw = a.file.read_text()
        else:
            run = subprocess.run(
                ["ov", "status", "--output", "json"], capture_output=True, text=True
            )
    except (OSError, UnicodeDecodeError) as exc:
        print(json.dumps({"error": f"could not read the status: {exc}"}))
        return 1
    if not a.file:
        if run.returncode != 0:
            print(
                json.dumps(
                    {
                        "error": f"ov status exited {run.returncode}",
                        "stderr": run.stderr[-300:],
                    }
                )
            )
            return 1
        raw = run.stdout
    try:
        status = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(json.dumps({"error": f"ov status output is not JSON: {exc}"}))
        return 1
    code, report = judge(status)
    print(json.dumps(report))
    return code


if __name__ == "__main__":
    sys.exit(main())
