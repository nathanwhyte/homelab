#!/usr/bin/env python3
"""IMPR-1188 Phase 3.1 / 4a: turn ``ov-recall-shadow`` log lines into a verdict table,
joined against the ov-pilot ledger for backend and lab/pilot provenance. Stdlib only.

Finding (2026-09-27, verified live against the deployed OpenViking v0.4.20 image, and
against the IMPR-1188 Phase 1 classifier as committed — homelab
``viking/manifests/test/nav-patch/ov_memory_guard_patch.py::shadow_classify``): the
classifier's hook, ``ExtractContext.__init__``, is given only ``messages`` and
``chunk_meta``. A real ``Message`` has ``id, role, parts, peer_id, created_at, turn_id,
message_kind, source_message_ids`` (``dataclasses.fields(Message)``, checked live) and
carries no session or archive id — confirmed by tracing the real commit path
(``session.py``: the archive write, Phase 2's read-back, and the id-preserving hydration
and image-replacement passes before extraction). **A shadow record therefore has no
field to join a ledger on directly.**

**2026-09-28 decision: the join key is the turn's message ids.** ``resolve_archive``
finds the archive whose ``messages.jsonl`` contains the record's ``first_message_id``,
narrowing candidates first — before reading a single archive — by the record's
``created_at_min``/``created_at_max`` span against the ledger's ``session`` row
timestamps (only a session that had already started can have written this turn), then
walks that session's archives (closest-preceding ledger session first) until one
``messages.jsonl`` contains the id. Reads are cached per ``(user, session_dir,
archive_id)`` for the life of one report run. Once resolved, the existing machinery
(``cc-``/``__subagent-`` stripping, launch_id join, lab-vs-pilot from the resolved
archive's user segment) runs unchanged. A record that resolves to nothing — no ledger
data, no ``created_at`` span, or no archive anywhere contains the id — is reported
``backend: unknown`` / ``lab_or_pilot: unknown``, never dropped. Two readers implement
the archive-content side: ``LocalTreeReader`` (offline, a local directory mirroring the
``viking://`` sessions tree — see ``testdata/archive-tree/``) and ``OvCliReader``
(online, read-only ``ov ls`` / ``ov read ... --user <user> -o json`` subprocess calls;
the command shapes were confirmed live against real prod session data 2026-09-28, but
``resolve_archive``'s candidate walk itself is exercised only against the offline
fixture in this repo's tests).

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
import os
import re
import subprocess
import sys
from datetime import datetime

SHADOW_PREFIX = "ov-recall-shadow "
_SESSION_SEGMENT_RE = re.compile(r"/sessions/(cc-[0-9a-fA-F-]+(?:__subagent-[^/]+)?)")
_ARCHIVE_USER_RE = re.compile(r"^viking://user/([^/]+)/")
LAB_USER = "noot-pilot-lab"
PILOT_USER = "noot-pilot"
# How much earlier than the turn's first message a ledger session may have started and
# still count as a candidate. Generous on purpose: a long-running session's launch can
# precede any one turn by hours.
DEFAULT_TS_SLACK_SECONDS = 24 * 3600
DEFAULT_MAX_CANDIDATES = 5


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
    """rows -> (launch_id -> {"backend":..., "mode":...},
    session_uuid -> {"launch_id":..., "ts": <earliest session-row ts>}).

    Multiple ``session`` rows can share one ``launch_id`` (resume/compact); the earliest
    ``ts`` for a given session UUID is kept, since that is the closest thing to "when
    this session started" available for candidate narrowing.
    """
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
            uuid = row["session_id"]
            ts = row.get("ts")
            existing = sessions.get(uuid)
            if existing is None:
                sessions[uuid] = {"launch_id": launch_id, "ts": ts}
            elif ts and (existing["ts"] is None or ts < existing["ts"]):
                existing["ts"] = ts
                existing["launch_id"] = launch_id
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


def backend_for(archive, ledgers):
    """(backend, machine) for a resolved ``archive`` URI, or ``("unknown", None)`` when
    there is no archive or it matches no ledger session — a record with no matching
    session is reported, never dropped."""
    session_uuid = bare_session_uuid(archive)
    if not session_uuid:
        return "unknown", None
    for machine, (starts, sessions) in ledgers.items():
        entry = sessions.get(session_uuid)
        launch_id = entry.get("launch_id") if entry else None
        if launch_id and launch_id in starts:
            return starts[launch_id].get("backend") or "unknown", machine
    return "unknown", None


def user_for_backend(backend) -> str:
    """Which OpenViking user namespace a launch's archives live under.

    IMPR-1204 layer 1: ``--ollama`` archives live under ``noot-pilot-lab``; everything
    else (including an unknown backend) is searched under ``noot-pilot`` first.
    """
    if backend and str(backend).startswith("ollama"):
        return LAB_USER
    return PILOT_USER


def _parse_ts(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts))
    except ValueError:
        return None


def _candidate_sessions(ledgers, created_at_min, ts_slack_seconds):
    """[(ts, session_uuid, backend), ...], closest-preceding first.

    A candidate is a ledger ``session`` row whose ``ts`` is at or before the turn's
    ``created_at_min`` (a session that started after the turn cannot have written it)
    and within ``ts_slack_seconds`` of it.
    """
    turn_start = _parse_ts(created_at_min)
    if turn_start is None:
        return []
    candidates = []
    for starts, sessions in ledgers.values():
        for session_uuid, entry in sessions.items():
            session_ts = _parse_ts(entry.get("ts"))
            if session_ts is None or session_ts > turn_start:
                continue
            if (turn_start - session_ts).total_seconds() > ts_slack_seconds:
                continue
            backend = starts.get(entry.get("launch_id"), {}).get("backend")
            candidates.append((session_ts, session_uuid, backend))
    candidates.sort(key=lambda c: c[0], reverse=True)
    return candidates


def resolve_archive(
    record,
    ledgers,
    reader,
    ts_slack_seconds=DEFAULT_TS_SLACK_SECONDS,
    max_candidates=DEFAULT_MAX_CANDIDATES,
):
    """The archive URI whose ``messages.jsonl`` contains the record's
    ``first_message_id``, or ``None`` when it cannot be resolved.

    Candidates are narrowed by ``created_at_min``/the ledger's session timestamps
    *before* any archive is read (see ``_candidate_sessions``); each candidate session's
    directory variants (the parent plus every subagent ``reader.list_sessions`` returns
    for the inferred user) are tried in turn, most-recent first, until one archive's
    ``messages.jsonl`` contains the id. ``reader`` caches its own reads, so re-resolving
    many records in one report run only reads each archive once.
    """
    first_id = record.get("first_message_id")
    if not first_id:
        return None
    for _ts, session_uuid, backend in _candidate_sessions(
        ledgers, record.get("created_at_min"), ts_slack_seconds
    )[:max_candidates]:
        user = user_for_backend(backend)
        for session_dir in reader.list_sessions(user):
            if not session_dir.startswith("cc-"):
                continue
            token = session_dir[len("cc-") :].split("__subagent-", 1)[0]
            if token != session_uuid:
                continue
            for archive_id in reader.list_archives(user, session_dir):
                messages = reader.read_messages(user, session_dir, archive_id)
                if not messages:
                    continue
                if any(m.get("id") == first_id for m in messages):
                    return f"viking://user/{user}/sessions/{session_dir}/history/{archive_id}"
    return None


class LocalTreeReader:
    """Offline: reads a local directory mirroring the ``viking://`` sessions tree.

    Layout: ``<root>/<user>/sessions/<session-dir>/history/<archive-id>/messages.jsonl``.
    """

    def __init__(self, root):
        self.root = root
        self._read_cache = {}

    def list_sessions(self, user):
        sessions_dir = os.path.join(self.root, user, "sessions")
        if not os.path.isdir(sessions_dir):
            return []
        return sorted(os.listdir(sessions_dir))

    def list_archives(self, user, session_dir):
        history_dir = os.path.join(self.root, user, "sessions", session_dir, "history")
        if not os.path.isdir(history_dir):
            return []
        return sorted(os.listdir(history_dir))

    def read_messages(self, user, session_dir, archive_id):
        key = (user, session_dir, archive_id)
        if key in self._read_cache:
            return self._read_cache[key]
        path = os.path.join(
            self.root,
            user,
            "sessions",
            session_dir,
            "history",
            archive_id,
            "messages.jsonl",
        )
        messages = None
        if os.path.isfile(path):
            with open(path) as fh:
                messages = [json.loads(ln) for ln in fh if ln.strip()]
        self._read_cache[key] = messages
        return messages


class OvCliReader:
    """Online, read-only: ``ov ls`` / ``ov read ... --user <user> -o json`` via
    subprocess. Command shapes confirmed live against real prod session data,
    2026-09-28 (``ov ls <uri> -s --user <user>`` for a simple path list; ``ov read <uri>
    --user <user> -o json`` returns ``{"ok": true, "result": "<raw jsonl>"}``). Never
    writes; every call is ``ls`` or ``read``.
    """

    def __init__(self, ov_bin="ov", timeout=30, run=subprocess.run):
        self.ov_bin = ov_bin
        self.timeout = timeout
        self._run = run
        self._ls_cache = {}
        self._read_cache = {}

    def _ls(self, uri, user):
        key = (uri, user)
        if key in self._ls_cache:
            return self._ls_cache[key]
        proc = self._run(
            [self.ov_bin, "ls", uri, "-s", "--user", user],
            capture_output=True,
            text=True,
            timeout=self.timeout,
        )
        paths = [
            line.strip()
            for line in proc.stdout.splitlines()
            if line.strip().startswith("viking://")
        ]
        self._ls_cache[key] = paths
        return paths

    def list_sessions(self, user):
        paths = self._ls(f"viking://user/{user}/sessions", user)
        return sorted({p.rsplit("/", 1)[-1] for p in paths})

    def list_archives(self, user, session_dir):
        paths = self._ls(f"viking://user/{user}/sessions/{session_dir}/history", user)
        return sorted({p.rsplit("/", 1)[-1] for p in paths})

    def read_messages(self, user, session_dir, archive_id):
        key = (user, session_dir, archive_id)
        if key in self._read_cache:
            return self._read_cache[key]
        uri = f"viking://user/{user}/sessions/{session_dir}/history/{archive_id}/messages.jsonl"
        proc = self._run(
            [self.ov_bin, "read", uri, "--user", user, "-o", "json"],
            capture_output=True,
            text=True,
            timeout=self.timeout,
        )
        messages = None
        try:
            payload = json.loads(proc.stdout)
            raw = payload.get("result") if isinstance(payload, dict) else None
            if isinstance(raw, str):
                messages = [json.loads(ln) for ln in raw.splitlines() if ln.strip()]
        except json.JSONDecodeError:
            messages = None
        self._read_cache[key] = messages
        return messages


def resolve_record_archive(
    record, ledgers, reader, ts_slack_seconds=DEFAULT_TS_SLACK_SECONDS
):
    """The record's archive URI: explicit ``archive`` field if present (forward
    compatibility — no shadow record carries one today), else resolved via ``reader``
    from the turn's message ids (``resolve_archive``); ``None`` when neither yields one
    (no reader given, no ledger data, or no archive anywhere contains the id)."""
    archive = record.get("archive")
    if archive:
        return archive
    if reader is None:
        return None
    return resolve_archive(record, ledgers, reader, ts_slack_seconds=ts_slack_seconds)


def build_report(
    records, ledgers, reader=None, ts_slack_seconds=DEFAULT_TS_SLACK_SECONDS
):
    by_verdict = collections.Counter()
    by_backend = {}
    rows = []
    for rec in records:
        verdict = rec.get("verdict", "unknown")
        by_verdict[verdict] += 1
        archive = resolve_record_archive(
            rec, ledgers, reader, ts_slack_seconds=ts_slack_seconds
        )
        backend, machine = backend_for(archive, ledgers)
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
    parser.add_argument(
        "--archive-root",
        help=(
            "Resolve each record's archive from a local directory mirroring the "
            "viking:// sessions tree (see testdata/archive-tree/ for the layout). "
            "Mutually exclusive with --online."
        ),
    )
    parser.add_argument(
        "--online",
        action="store_true",
        help=(
            "Resolve each record's archive via read-only `ov ls`/`ov read --user <user> "
            "-o json` calls (requires the ov CLI and its config to already be set up). "
            "Mutually exclusive with --archive-root."
        ),
    )
    parser.add_argument("--ov-bin", default="ov", help="ov CLI binary for --online.")
    parser.add_argument(
        "--ts-slack-seconds",
        type=int,
        default=DEFAULT_TS_SLACK_SECONDS,
        help="How far before a turn's created_at_min a ledger session may have started.",
    )
    args = parser.parse_args(argv)
    if args.archive_root and args.online:
        parser.error("--archive-root and --online are mutually exclusive")
    return args


def _build_reader(args):
    if args.archive_root:
        return LocalTreeReader(args.archive_root)
    if args.online:
        return OvCliReader(ov_bin=args.ov_bin)
    return None


def main(argv=None):
    args = parse_args(argv)
    lines = _read_lines(args.log)
    records = parse_shadow_lines(lines)
    ledgers = load_ledgers(args.ledger)
    reader = _build_reader(args)
    report = build_report(
        records, ledgers, reader=reader, ts_slack_seconds=args.ts_slack_seconds
    )
    text = render_markdown(report)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text)
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
