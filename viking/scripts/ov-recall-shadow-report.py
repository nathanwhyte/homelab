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

**Namespace selection (2026-09-28 finding).** A ``start`` row's ``user`` field is
hardcoded ``noot-pilot`` regardless of ``--backend`` (``ov-pilot.sh:224``, ``--arg user
noot-pilot``, unconditional) — every existing ``ollama:*`` launch's archives actually
live under ``noot-pilot``, not ``noot-pilot-lab``. ``_candidate_users`` therefore tries
the recorded ``start.user`` (or ``noot-pilot`` when the field is absent, for
pre-existing ledger rows) first, and ``noot-pilot-lab`` only as a fallback — never a
guess from the backend. A later capture-side change (IMPR-1204 layer 1) may route new
``--ollama`` archives under ``noot-pilot-lab`` going forward; the fallback exists for
that case, not because today's ledger data points there.

**Resumed sessions (2026-09-28 finding).** A session can be resumed under a different
launch — an ``--ollama`` capture session picked up later under a plain ``claude``
launch, for example — and each launch gets its own row. ``index_ledger`` keeps every
launch a session appeared under, not just the first; ``backend_for`` picks the launch
window (``_launch_windows``: from one launch's ``ts`` to the next launch's ``ts``, or
its own ``end`` row if the ledger ever emits one) the turn's ``created_at_min`` falls
in, and reports ``backend: "ambiguous"`` with every fitting ``{"machine", "backend"}``
candidate when more than one window fits (or none do, with no ``created_at_min`` to
disambiguate). ``testdata/ledger-resume.jsonl`` is an ollama→anthropic resume fixture.

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
    """rows -> (launch_id -> {"backend":..., "mode":..., "user":...},
    session_uuid -> [{"launch_id":..., "ts":..., "end_ts":...}, ...] sorted by ts).

    A session can be resumed under a different launch (``source: "resume"``), so the
    *full* launch history is kept, one entry per distinct ``launch_id`` (the earliest
    ``session``-row ``ts`` for that launch — resume/compact can repeat rows under the
    same launch). ``end_ts`` comes from an ``event: "end"`` row for that
    ``(session_id, launch_id)`` pair if the ledger ever emits one — not confirmed to
    exist as of 2026-09-28, handled defensively so a launch-window close is honoured
    the moment it does; ``_launch_windows`` falls back to "until the next launch"
    otherwise.
    """
    starts = {}
    launch_ts = {}  # (uuid, launch_id) -> earliest session-row ts
    end_ts = {}  # (uuid, launch_id) -> latest end-row ts
    for row in rows:
        launch_id = row.get("launch_id")
        if not launch_id:
            continue
        event = row.get("event")
        if event == "start":
            starts[launch_id] = {
                "backend": row.get("backend"),
                "mode": row.get("mode"),
                "user": row.get("user"),
            }
        elif event == "session" and row.get("session_id"):
            key = (row["session_id"], launch_id)
            ts = row.get("ts")
            if ts and (key not in launch_ts or ts < launch_ts[key]):
                launch_ts[key] = ts
        elif event == "end" and row.get("session_id"):
            key = (row["session_id"], launch_id)
            ts = row.get("ts")
            if ts and (key not in end_ts or ts > end_ts[key]):
                end_ts[key] = ts

    sessions = {}
    for (uuid, launch_id), ts in launch_ts.items():
        sessions.setdefault(uuid, []).append(
            {"launch_id": launch_id, "ts": ts, "end_ts": end_ts.get((uuid, launch_id))}
        )
    for launches in sessions.values():
        launches.sort(key=lambda entry: entry["ts"])
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


def _launch_windows(launches):
    """``launches`` (sorted by ts, ``index_ledger``'s per-session list) ->
    ``[(start_ts, end_ts_or_None, launch_id), ...]``.

    A window runs from its launch's ``ts`` until the next launch's ``ts`` (a resume
    hands the session to a new launch) — or its own ``end_ts``, when the ledger has
    one and it closes before the next launch starts. The last launch's window is
    open-ended (``None``) when there is no ``end_ts``.
    """
    windows = []
    for i, entry in enumerate(launches):
        next_start = launches[i + 1]["ts"] if i + 1 < len(launches) else None
        end_ts = entry.get("end_ts")
        if end_ts and (next_start is None or end_ts < next_start):
            close = end_ts
        else:
            close = next_start
        windows.append((entry["ts"], close, entry["launch_id"]))
    return windows


def _backend_windows_for_session(session_uuid, ledgers):
    """``[(machine, launch_id, start_ts, end_ts_or_None), ...]`` across every ledger."""
    out = []
    for machine, (_starts, sessions) in ledgers.items():
        launches = sessions.get(session_uuid)
        if not launches:
            continue
        for start_ts, end_ts, launch_id in _launch_windows(launches):
            out.append((machine, launch_id, start_ts, end_ts))
    return out


def backend_for(archive, ledgers, created_at_min=None):
    """(backend, machine, candidates).

    ``backend`` is a real backend string, ``"unknown"`` (no archive, no ledger match),
    or ``"ambiguous"`` (the turn's ``created_at_min`` fits more than one launch window,
    or — with no ``created_at_min`` given — the session was resumed under more than one
    launch and there is nothing to disambiguate with). ``candidates`` is the list of
    ``{"machine", "backend"}`` dicts that fit when ``backend == "ambiguous"``, else
    ``None``. A record with no matching session is reported ``"unknown"``, never
    dropped.
    """
    session_uuid = bare_session_uuid(archive)
    if not session_uuid:
        return "unknown", None, None
    windows = _backend_windows_for_session(session_uuid, ledgers)
    if not windows:
        return "unknown", None, None

    def _backend(machine, launch_id):
        starts, _sessions = ledgers[machine]
        return starts.get(launch_id, {}).get("backend") or "unknown"

    if created_at_min is None:
        if len(windows) == 1:
            machine, launch_id, _s, _e = windows[0]
            return _backend(machine, launch_id), machine, None
        candidates = [
            {"machine": m, "backend": _backend(m, lid)} for m, lid, _s, _e in windows
        ]
        return "ambiguous", None, candidates

    turn_ts = _parse_ts(created_at_min)
    fitting = []
    for machine, launch_id, start_ts, end_ts in windows:
        start = _parse_ts(start_ts)
        if turn_ts is None or start is None or turn_ts < start:
            continue
        end = _parse_ts(end_ts) if end_ts else None
        if end is not None and turn_ts >= end:
            continue
        fitting.append((machine, launch_id))
    if len(fitting) == 1:
        machine, launch_id = fitting[0]
        return _backend(machine, launch_id), machine, None
    if not fitting:
        return "unknown", None, None
    candidates = [{"machine": m, "backend": _backend(m, lid)} for m, lid in fitting]
    return "ambiguous", None, candidates


def _candidate_users(launch_user):
    """[primary, fallback?] OpenViking user namespaces to search for a launch's
    archives, primary first.

    2026-09-28 finding: every existing ``ollama:*`` launch's archives actually live
    under ``noot-pilot`` — a backend-based guess (assuming any ``ollama:*`` launch is
    ``noot-pilot-lab``) missed them all. The recorded ``user`` from the ledger's
    ``start`` row is authoritative; ``noot-pilot`` is the fallback default for a start
    row with no ``user`` field (older ledger rows). ``noot-pilot-lab`` is tried only
    when the primary guess misses — never assumed from the backend.
    """
    primary = launch_user or PILOT_USER
    if primary == LAB_USER:
        return [primary]
    return [primary, LAB_USER]


def _parse_ts(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts))
    except ValueError:
        return None


def _candidate_sessions(ledgers, created_at_min, ts_slack_seconds):
    """[(ts, session_uuid, launch_user), ...], closest-preceding first.

    A candidate is one ledger launch (of possibly several — a resumed session has one
    per launch) whose ``ts`` is at or before the turn's ``created_at_min`` (a launch
    that started after the turn cannot have written it) and within
    ``ts_slack_seconds`` of it. ``launch_user`` is that launch's recorded
    ``start.user`` (``None`` when the start row predates that field or is missing). A
    resumed session naturally contributes one candidate per launch, so the
    session-directory search in ``resolve_archive`` tries the user each launch
    actually recorded, not just the session's original one.
    """
    turn_start = _parse_ts(created_at_min)
    if turn_start is None:
        return []
    candidates = []
    for starts, sessions in ledgers.values():
        for session_uuid, launches in sessions.items():
            for entry in launches:
                session_ts = _parse_ts(entry.get("ts"))
                if session_ts is None or session_ts > turn_start:
                    continue
                if (turn_start - session_ts).total_seconds() > ts_slack_seconds:
                    continue
                launch_user = starts.get(entry.get("launch_id"), {}).get("user")
                candidates.append((session_ts, session_uuid, launch_user))
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
    *before* any archive is read (see ``_candidate_sessions``); for each candidate
    session, every user namespace ``_candidate_users`` names (the launch's recorded
    ``start.user``, else ``noot-pilot``, then ``noot-pilot-lab`` only as a fallback)
    and each of that user's session-directory variants (the parent plus every subagent
    ``reader.list_sessions`` returns) are tried in turn, most-recent session first,
    until one archive's ``messages.jsonl`` contains the id. ``reader`` caches its own
    reads, so re-resolving many records in one report run only reads each archive once.
    """
    first_id = record.get("first_message_id")
    if not first_id:
        return None
    for _ts, session_uuid, launch_user in _candidate_sessions(
        ledgers, record.get("created_at_min"), ts_slack_seconds
    )[:max_candidates]:
        for user in _candidate_users(launch_user):
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
        backend, machine, candidates = backend_for(
            archive, ledgers, created_at_min=rec.get("created_at_min")
        )
        # Session identity, not archive identity: bare_session_uuid already strips a
        # subagent's "__subagent-..." suffix, so a subagent's turns are counted under
        # its parent session, never as a session of their own. A turn with no
        # resolved archive has no session to attribute and is counted separately
        # (unresolved_turns), never as a pseudo-session keyed on its message ids.
        session_uuid = bare_session_uuid(archive) if archive else None
        bucket = by_backend.setdefault(
            backend, {"sessions": set(), "turns": 0, "strip": 0, "unresolved_turns": 0}
        )
        if session_uuid:
            bucket["sessions"].add(session_uuid)
        else:
            bucket["unresolved_turns"] += 1
        bucket["turns"] += 1
        if verdict == "strip":
            bucket["strip"] += 1
        rows.append(
            {
                "verdict": verdict,
                "backend": backend,
                "machine": machine or "unknown",
                "ambiguous_candidates": candidates,
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
        "| Backend | Sessions | Recall turns | Strip verdicts | Unresolved turns |",
        "| --- | --- | --- | --- | --- |",
    ]
    for backend, bucket in sorted(report["by_backend"].items()):
        lines.append(
            f"| {backend} | {len(bucket['sessions'])} | {bucket['turns']} | "
            f"{bucket['strip']} | {bucket['unresolved_turns']} |"
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
