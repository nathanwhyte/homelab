#!/usr/bin/env python3
"""IMPR-1188 Phase 3.1 / 4a: turn ``ov-recall-shadow`` log lines into a verdict table,
joined against the ov-pilot ledger for backend and lab/pilot provenance. Stdlib only.

Finding (2026-09-27, verified live against the deployed OpenViking v0.4.20 image, and
against the IMPR-1188 Phase 1 classifier as committed — homelab
``viking/manifests/test/nav-patch/ov_memory_guard_patch.py::shadow_classify``): the
classifier's hook, ``ExtractContext.__init__``, is given only ``messages`` and
``chunk_meta``. A real ``Message`` has ``id, role, parts, peer_id, created_at, turn_id,
message_kind, source_message_ids`` (``dataclasses.fields(Message)``, checked live) and
carries no session or archive id. **Every ``ov-recall-shadow`` record therefore has no
field to join a ledger on today** — this report labels every row from a real production
log ``backend: unknown`` / ``lab_or_pilot: unknown``. The join machinery below (ledger
parsing, ``cc-``/``__subagent-`` stripping, launch_id join, lab-vs-pilot from the archive
URI) is built and tested against an optional ``archive`` field
(``viking://user/<user>/sessions/cc-<uuid>[__subagent-<id>]/...``) a shadow record does
not carry yet, so the report is ready the moment a later phase adds one — it is not
exercised by real shadow-log data today.

Ledger row shapes consumed (jsonl, one file per machine, ``$pilot_home/ledger.jsonl``,
``bin/ov-pilot-session-hook.sh`` / ``ov-pilot.sh``):

  start:   {"event":"start","launch_id":...,"ts":...,"mode":...,"backend":...,
            "cwd":...,"toplevel":...,"user":...,"plugin_commit":...,"dotfiles":...,
            "settings_sha":...}
  session: {"event":"session","launch_id":...,"ts":...,"mode":...,
            "session_id":<bare Claude UUID>,"source":"startup"|"resume"|"clear"|"compact",
            "transcript_path":...,"cwd":...}

A launch's backend/mode comes from its ``start`` row. Multiple ``session`` rows can share
one ``launch_id`` (resume/compact recreates the mapping under the same launch). An
OpenViking archive/session id is ``cc-<uuid>`` or ``cc-<uuid>__subagent-<id>``; the join
strips ``cc-`` and drops any ``__subagent-...`` suffix before matching a ledger
``session_id`` — a subagent joins through its parent session's launch.

Usage:
    ov-recall-shadow-report.py --log shadow.log --ledger pop=~/.openviking/pilot/ledger.jsonl \\
        --ledger workbook=/path/to/workbook-ledger.jsonl --out report.md

    kubectl -n viking logs deploy/openviking --all-containers \\
        | ov-recall-shadow-report.py --log -
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sys

SHADOW_PREFIX = "ov-recall-shadow "
_SESSION_SEGMENT_RE = re.compile(r"/sessions/(cc-[0-9a-fA-F-]+(?:__subagent-[^/]+)?)")
_ARCHIVE_USER_RE = re.compile(r"^viking://user/([^/]+)/")
LAB_USER = "noot-pilot-lab"
PILOT_USER = "noot-pilot"


def parse_shadow_lines(lines):
    """``ov-recall-shadow {...}`` JSON payloads from raw log lines (any prefix, e.g. a
    ``kubectl logs`` timestamp or pod name, is ignored — only the payload after the
    marker matters). Lines that are not shadow lines, or whose payload doesn't parse,
    are skipped rather than raising: a report over a whole pod log must not abort on one
    unrelated line."""
    records = []
    for line in lines:
        idx = line.find(SHADOW_PREFIX)
        if idx == -1:
            continue
        payload = line[idx + len(SHADOW_PREFIX) :].strip()
        try:
            rec = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict) and "skipped" not in rec:
            records.append(rec)
    return records


def parse_ledger_rows(path):
    rows = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def index_ledger(rows):
    """rows -> (launch_id -> {"backend":..., "mode":...}, session_uuid -> launch_id)."""
    starts = {}
    sessions = {}
    for row in rows:
        launch_id = row.get("launch_id")
        if not launch_id:
            continue
        event = row.get("event")
        if event == "start":
            starts[launch_id] = {"backend": row.get("backend"), "mode": row.get("mode")}
        elif event == "session" and row.get("session_id"):
            sessions[row["session_id"]] = launch_id
    return starts, sessions


def load_ledgers(specs):
    """``["pop=path", "workbook=path"]`` -> {machine: (starts, sessions)}."""
    ledgers = {}
    for spec in specs:
        machine, sep, path = spec.partition("=")
        if not sep or not path:
            raise ValueError(f"--ledger expects MACHINE=PATH, got {spec!r}")
        ledgers[machine] = index_ledger(parse_ledger_rows(path))
    return ledgers


def bare_session_uuid(archive: str):
    """``viking://user/u/sessions/cc-<uuid>[__subagent-<id>]/...`` -> bare ``<uuid>``,
    stripping ``cc-`` and any ``__subagent-...`` suffix (a subagent joins through its
    parent's launch). ``None`` when the URI has no recognisable sessions segment."""
    if not archive:
        return None
    m = _SESSION_SEGMENT_RE.search(archive)
    if not m:
        return None
    token = m.group(1)[len("cc-") :]
    return token.split("__subagent-", 1)[0]


def lab_or_pilot(archive: str) -> str:
    """``"lab"`` / ``"pilot"`` from the archive URI's user segment, else ``"unknown"``."""
    if not archive:
        return "unknown"
    m = _ARCHIVE_USER_RE.match(archive)
    if not m:
        return "unknown"
    user = m.group(1)
    if user == LAB_USER:
        return "lab"
    if user == PILOT_USER:
        return "pilot"
    return "unknown"


def backend_for(record, ledgers):
    """(backend, machine) for a shadow record's optional ``archive`` field, or
    ``("unknown", None)`` when the field is absent or matches no ledger — a record with
    no matching session is reported, never dropped."""
    session_uuid = bare_session_uuid(record.get("archive"))
    if not session_uuid:
        return "unknown", None
    for machine, (starts, sessions) in ledgers.items():
        launch_id = sessions.get(session_uuid)
        if launch_id and launch_id in starts:
            return starts[launch_id].get("backend") or "unknown", machine
    return "unknown", None


def build_report(records, ledgers):
    by_verdict = collections.Counter()
    by_backend = {}
    rows = []
    for rec in records:
        verdict = rec.get("verdict", "unknown")
        by_verdict[verdict] += 1
        backend, machine = backend_for(rec, ledgers)
        archive = rec.get("archive")
        session_key = archive or (
            rec.get("first_message_id"),
            rec.get("last_message_id"),
        )
        bucket = by_backend.setdefault(
            backend, {"sessions": set(), "turns": 0, "strip": 0}
        )
        bucket["sessions"].add(session_key)
        bucket["turns"] += 1
        if verdict == "strip":
            bucket["strip"] += 1
        rows.append(
            {
                "verdict": verdict,
                "backend": backend,
                "machine": machine or "unknown",
                "lab_or_pilot": lab_or_pilot(archive) if archive else "unknown",
                "turn_start": rec.get("turn_start"),
                "turn_end": rec.get("turn_end"),
                "first_message_id": rec.get("first_message_id"),
                "last_message_id": rec.get("last_message_id"),
                "ov_tools": rec.get("ov_tools", []),
                "other_tools": rec.get("other_tools", []),
            }
        )
    return {"by_verdict": by_verdict, "by_backend": by_backend, "rows": rows}


def render_markdown(report) -> str:
    lines = ["## Turns by verdict", "", "| Verdict | Count |", "| --- | --- |"]
    for verdict, count in sorted(report["by_verdict"].items()):
        lines.append(f"| {verdict} | {count} |")
    lines += [
        "",
        "## Per-backend summary",
        "",
        "| Backend | Sessions | Recall turns | Strip verdicts |",
        "| --- | --- | --- | --- |",
    ]
    for backend, bucket in sorted(report["by_backend"].items()):
        lines.append(
            f"| {backend} | {len(bucket['sessions'])} | {bucket['turns']} | {bucket['strip']} |"
        )
    return "\n".join(lines) + "\n"


def _read_lines(paths):
    lines = []
    for path in paths:
        if path == "-":
            lines.extend(sys.stdin.readlines())
        else:
            with open(path) as fh:
                lines.extend(fh.readlines())
    return lines


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--log",
        action="append",
        required=True,
        help="Path to a log file containing ov-recall-shadow lines, or - for stdin. Repeatable.",
    )
    parser.add_argument(
        "--ledger",
        action="append",
        default=[],
        help="MACHINE=PATH to a pilot ledger.jsonl. Repeatable.",
    )
    parser.add_argument(
        "--out", help="Write the markdown table here instead of stdout."
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    lines = _read_lines(args.log)
    records = parse_shadow_lines(lines)
    ledgers = load_ledgers(args.ledger)
    report = build_report(records, ledgers)
    text = render_markdown(report)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text)
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
