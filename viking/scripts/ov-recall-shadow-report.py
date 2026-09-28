#!/usr/bin/env python3
"""IMPR-1188 Phase 3.1 / 4a: turn ``ov-recall-shadow`` log lines into a per-turn table
(machine, backend, session, archive, verdict, tools, produced events, an empty
manual-label column) plus an aggregate summary, joined against the ov-pilot ledger for
backend and lab/pilot provenance. Stdlib only.

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

**Per-turn table and produced events (2026-09-28 finding).** The rendered report leads
with a ``## Turns`` table — one row per shadow record: machine, backend, session
(the resolved parent session UUID), archive URI, turn index range, verdict, the tool
classifications, the events that turn's archive actually produced, and an empty
manual-label column (``restatement`` / ``new-info`` / ``mixed``, filled in by hand
during labelling) — with the aggregate counts kept as a separate ``## Aggregate
summary`` section below it. ``_events_for_archive`` reads that archive's
``memory_diff.json`` read-only through the same ``reader`` used to resolve the
archive (``LocalTreeReader.read_memory_diff`` / ``OvCliReader.read_memory_diff``,
mirroring the real nested ``operations.adds``/``updates`` shape found in
``ov-replay-archives.py``'s ``_diff_operations``) and lists each produced event's name
and a best-effort one-line abstract (the ``# Summary`` line of its rendered content).
A record with no resolved archive, or an archive with no ``memory_diff.json`` yet,
still gets a row — the Events cell just reads "—".

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
from datetime import datetime, timedelta

SHADOW_PREFIX = "ov-recall-shadow "
_SESSION_SEGMENT_RE = re.compile(r"/sessions/(cc-[0-9a-fA-F-]+(?:__subagent-[^/]+)?)")
_ARCHIVE_USER_RE = re.compile(r"^viking://user/([^/]+)/")
_ARCHIVE_URI_RE = re.compile(
    r"^viking://user/([^/]+)/sessions/([^/]+)/history/([^/]+)$"
)
LAB_USER = "noot-pilot-lab"
PILOT_USER = "noot-pilot"
# How much earlier than the turn's first message a ledger session may have started and
# still count as a candidate. Generous on purpose: a long-running session's launch can
# precede any one turn by hours.
DEFAULT_TS_SLACK_SECONDS = 24 * 3600
# D-2 (Codex #173 review, 2026-09-27): a hardcoded cap of 5 silently dropped the true
# session whenever six-plus other launches (any /clear, /resume, or new launch on
# either machine) started closer to the turn -- routine on a two-machine workday, and
# unraisable because nothing threaded a cap override through to here. Unlimited by
# default; --max-candidates opts into a narrower, faster search when needed. `None`
# means "no cap" everywhere this is threaded (a `[:None]` slice is the full list).
DEFAULT_MAX_CANDIDATES = None


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
    same launch). ``end_ts`` comes from the launch's ``event: "end"`` row. The launcher
    writes one per launch, keyed by ``launch_id`` only (``{event, launch_id, ts, mode,
    exit}``, no ``session_id``), so it closes every session that launch held. A
    crashed or still-running launch has no end row; ``_launch_windows`` then falls
    back to "until the next launch", or open-ended for the last one.
    """
    starts = {}
    launch_ts = {}  # (uuid, launch_id) -> earliest session-row ts
    launch_end = {}  # launch_id -> latest end-row ts
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
        elif event == "end":
            ts = row.get("ts")
            if ts and (launch_id not in launch_end or ts > launch_end[launch_id]):
                launch_end[launch_id] = ts

    sessions = {}
    for (uuid, launch_id), ts in launch_ts.items():
        sessions.setdefault(uuid, []).append(
            {"launch_id": launch_id, "ts": ts, "end_ts": launch_end.get(launch_id)}
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


def parse_archive_uri(archive: str):
    """``viking://user/<user>/sessions/<session-dir>/history/<archive-id>`` ->
    ``{"user", "session_dir", "archive_id"}``, or ``None`` when it doesn't match."""
    if not archive:
        return None
    m = _ARCHIVE_URI_RE.match(archive)
    if not m:
        return None
    return {"user": m.group(1), "session_dir": m.group(2), "archive_id": m.group(3)}


# --- Canonical tool classifier (C-2/C-4 downstream, Codex #173 review, 2026-09-27) --
#
# The single classifier used both to reclassify a record from the emitter's forward-
# compatible other_tool_names field and to reclassify a reconstructed archive turn
# (see reclassify_record / D-7 below). The production shadow classifier
# (viking/manifests/test/nav-patch/ov_memory_guard_patch.py::classify_tool) mirrors
# this logic; keep the two in sync.

_OV_READ_TOOL_RE = re.compile(
    r"^mcp__plugin_openviking-memory_openviking__(read|search|find|list|tree|grep|glob)$"
)
_MUTATING_TOOL_NAMES = frozenset(
    {"Edit", "Write", "NotebookEdit", "MultiEdit", "Artifact", "SendMessage"}
)
_READ_ONLY_TOOL_NAMES = frozenset(
    {"ToolSearch", "Read", "Glob", "Grep", "LS", "TodoWrite"}
)
# create/update/delete/send/write/publish. C-2 (Codex #173 review): the production
# classifier's own comment claimed this was scoped to MCP tools, but the code applied
# it to every name, so TodoWrite and SendMessage misclassified as mutating/unknown.
# Scoped to mcp__-prefixed names only here, and TodoWrite/SendMessage are handled by
# explicit name sets above so this regex never has to guess at them.
_MUTATING_VERB_RE = re.compile(
    r"create|update|delete|send|write|publish", re.IGNORECASE
)


def classify_tool_name(name, tool_status=None):
    """``"ov_read"`` / ``"ov_read_error"`` / ``"read_only"`` / ``"mutating"`` /
    ``"unknown"`` for one tool call.

    TodoWrite is explicitly neutral (routine agentic bookkeeping, changes nothing
    outside the session); SendMessage/Write/Edit are explicitly mutating; the verb
    heuristic applies only to ``mcp__``-prefixed names, never to a bare tool name that
    happens to contain a verb-like substring; a failed OpenViking read (C-4) is its own
    classification so it can never be folded into a clean, strip-eligible ov_read.
    """
    if not name:
        return "unknown"
    if _OV_READ_TOOL_RE.match(name):
        return "ov_read_error" if tool_status == "error" else "ov_read"
    if name in _MUTATING_TOOL_NAMES:
        return "mutating"
    if name in _READ_ONLY_TOOL_NAMES:
        return "read_only"
    if name.startswith("mcp__") and _MUTATING_VERB_RE.search(name):
        return "mutating"
    return "unknown"


def _derive_verdict_from_classifications(classifications):
    """The verdict a set of tool classifications implies, mirroring the production
    classifier's own precedence: any mutating call wins ``"keep-mixed"``; otherwise any
    unknown tool or an errored OpenViking read forces ``"keep-ambiguous"``; a clean
    turn is ``"strip"``."""
    classifications = list(classifications)
    if "mutating" in classifications:
        return "keep-mixed"
    if "unknown" in classifications or "ov_read_error" in classifications:
        return "keep-ambiguous"
    return "strip"


def _is_turn_boundary_message(message):
    """The same turn-boundary test the production classifier uses
    (``_is_turn_boundary``), applied to a raw archive message dict rather than a
    dataclass: ``role == "user"``, not a checkpoint, with a non-empty text part."""
    if not isinstance(message, dict) or message.get("role") != "user":
        return False
    if message.get("message_kind") == "checkpoint":
        return False
    for part in message.get("parts") or []:
        text = part.get("text") if isinstance(part, dict) else None
        if text and str(text).strip():
            return True
    return False


def reclassify_record(record, archive, reader):
    """The corrected verdict for a shadow record, or ``None`` when nothing changes it
    (or nothing can be checked at all).

    D-7 / Codex additional finding 1 (2026-09-27, the finding D-7 itself missed): a
    turn that upstream splits across extraction batches can log a fragment with
    ``partial: false`` and a clean ``strip`` verdict — ``partial`` only checks whether
    the fragment *starts* at a real turn boundary, not whether more of the same
    logical turn (a mutating tool call, say) follows in a later, separately-logged
    fragment. Filtering on ``partial`` alone, or joining only the fragments the
    emitter happened to log, cannot recover an untracked tail. This reconstructs the
    *complete* source turn from the resolved archive's raw ``messages.jsonl`` --
    walking forward from the record's own ``first_message_id`` to the next real turn
    boundary, regardless of where the record's own ``last_message_id`` fell -- and
    re-derives the verdict from every tool call actually in that window.

    Preference order (C-2/C-4 downstream): (1) the emitter's own forward-compatible
    ``other_tool_names``/``ov_read_error`` fields when present, reclassifying without
    touching the archive at all; (2) the archive reconstruction above, when the
    record's message ids can be located in it. ``None`` when neither source is
    available, or when reconstruction finds nothing beyond what was already logged.
    """
    other_names = record.get("other_tool_names")
    if other_names is not None:
        classifications = [classify_tool_name(n) for n in other_names]
        if record.get("ov_tools"):
            classifications.append(
                "ov_read_error" if record.get("ov_read_error") else "ov_read"
            )
        return _derive_verdict_from_classifications(classifications)
    if reader is None or not archive:
        return None
    parsed = parse_archive_uri(archive)
    if not parsed:
        return None
    messages = reader.read_messages(
        parsed["user"], parsed["session_dir"], parsed["archive_id"]
    )
    if not messages:
        return None
    ids = [m.get("id") for m in messages]
    first_id = record.get("first_message_id")
    if first_id not in ids:
        return None
    start = ids.index(first_id)
    last_id = record.get("last_message_id")
    logged_end_idx = ids.index(last_id) if last_id in ids else start
    end = len(messages)
    for i in range(start + 1, len(messages)):
        if _is_turn_boundary_message(messages[i]):
            end = i
            break
    if end - 1 <= logged_end_idx:
        # Nothing beyond the logged range -- the record already covered the whole
        # true turn.
        return None
    classifications = []
    for message in messages[start:end]:
        if message.get("role") != "assistant":
            continue
        for part in message.get("parts") or []:
            if not isinstance(part, dict):
                continue
            name = part.get("tool_name")
            if not name:
                continue
            classifications.append(classify_tool_name(name, part.get("tool_status")))
    if not classifications:
        return None
    return _derive_verdict_from_classifications(classifications)


def _extract_summary(content: str, max_chars: int = 160):
    """A one-line abstract: the first non-blank line after a ``# Summary`` heading,
    truncated. Falls back to the first non-blank line of the whole body when there is
    no ``# Summary`` heading; ``None`` for empty content."""
    if not content:
        return None
    lines = content.splitlines()
    start = 0
    for i, line in enumerate(lines):
        if line.strip() == "# Summary":
            start = i + 1
            break
    for line in lines[start:]:
        line = line.strip()
        if line:
            return line[:max_chars]
    return None


def _diff_events(diff):
    """[{"uri", "memory_type", "operation", "name", "abstract"}, ...] from a
    ``memory_diff.json`` payload's ``adds``, ``updates`` *and* ``deletes`` (D-6, Codex
    #173 review: a delete-only diff used to render as no events at all) -- the real
    shape nests them under ``operations``, per ``_diff_operations`` in
    ``ov-replay-archives.py``; a flat top-level shape is also accepted. ``name`` is the
    URI's basename with ``.md`` stripped; ``operation`` is ``"add"`` / ``"update"`` /
    ``"delete"`` (the memory-type x operation typing D-6 asked for); ``abstract`` is
    the item's rendered ``# Summary`` line, best-effort, from ``after`` when present
    (add/update) else ``before`` (a delete has no ``after``)."""
    if not isinstance(diff, dict):
        return []
    operations = diff.get("operations")
    ops = operations if isinstance(operations, dict) else diff
    adds = list(ops.get("adds", []) or [])
    updates = list(ops.get("updates", []) or [])
    deletes = list(ops.get("deletes", []) or [])
    events = []
    for operation, items in (("add", adds), ("update", updates), ("delete", deletes)):
        for item in items:
            if not isinstance(item, dict):
                continue
            uri = item.get("uri")
            name = None
            if uri:
                base = uri.rsplit("/", 1)[-1]
                name = base.removesuffix(".md")
            content = item.get("after")
            if content is None:
                content = item.get("before")
            events.append(
                {
                    "uri": uri,
                    "memory_type": item.get("memory_type"),
                    "operation": operation,
                    "name": name,
                    "abstract": _extract_summary(content or ""),
                }
            )
    return events


def _events_for_archive(archive, reader):
    """The events an archive's extraction produced, via ``reader.read_memory_diff`` —
    ``[]`` when there is no archive, no reader, or the archive has no memory_diff.json
    (extraction failed, produced nothing, or has not run yet)."""
    if not archive or reader is None:
        return []
    parsed = parse_archive_uri(archive)
    if not parsed:
        return []
    diff = reader.read_memory_diff(
        parsed["user"], parsed["session_dir"], parsed["archive_id"]
    )
    return _diff_events(diff)


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


def backend_for(archive, ledgers, created_at_min=None, window_slack_seconds=0):
    """(backend, machine, candidates, window_status).

    ``backend`` is a real backend string, ``"unknown"`` (no archive, no ledger match),
    or ``"ambiguous"`` (the turn's ``created_at_min`` fits more than one launch window,
    or — with no ``created_at_min`` given — the session was resumed under more than one
    launch and there is nothing to disambiguate with). ``candidates`` is the list of
    ``{"machine", "backend"}`` dicts that fit when ``backend == "ambiguous"``, else
    ``None``. A record with no matching session is reported ``"unknown"``, never
    dropped.

    ``window_status`` (D-4, Codex #173 review) is ``"fit"`` (attributed cleanly),
    ``"outside-window"`` (a single-launch session attributed anyway, but the turn's
    timestamp actually falls outside that launch's own window), ``"ambiguous"``, or
    ``"unknown"``.

    D-4 finding: ``created_at`` is the server's receive time on the cluster clock, not
    the plugin's capture time -- a detached write landing after the launcher's own end
    row, a replayed pending queue after an outage, or ordinary cluster/machine clock
    skew can all put a turn just outside its true window. When the session has exactly
    one launch there is nothing to disambiguate against, so it is always attributed
    (flagged ``outside-window`` rather than discarded as ``unknown``); multi-launch
    sessions still need the window to pick the right one, now with
    ``window_slack_seconds`` of symmetric tolerance on both bounds.
    """
    session_uuid = bare_session_uuid(archive)
    if not session_uuid:
        return "unknown", None, None, "unknown"
    windows = _backend_windows_for_session(session_uuid, ledgers)
    if not windows:
        return "unknown", None, None, "unknown"

    def _backend(machine, launch_id):
        starts, _sessions = ledgers[machine]
        return starts.get(launch_id, {}).get("backend") or "unknown"

    slack = timedelta(seconds=window_slack_seconds)

    if len(windows) == 1:
        machine, launch_id, start_ts, end_ts = windows[0]
        backend = _backend(machine, launch_id)
        status = "fit"
        turn_ts = _parse_ts(created_at_min)
        start = _parse_ts(start_ts)
        if turn_ts is not None and start is not None:
            end = _parse_ts(end_ts) if end_ts else None
            if turn_ts < start - slack or (end is not None and turn_ts >= end + slack):
                status = "outside-window"
        return backend, machine, None, status

    if created_at_min is None:
        candidates = [
            {"machine": m, "backend": _backend(m, lid)} for m, lid, _s, _e in windows
        ]
        return "ambiguous", None, candidates, "ambiguous"

    turn_ts = _parse_ts(created_at_min)
    fitting = []
    for machine, launch_id, start_ts, end_ts in windows:
        start = _parse_ts(start_ts)
        if turn_ts is None or start is None or turn_ts < start - slack:
            continue
        end = _parse_ts(end_ts) if end_ts else None
        if end is not None and turn_ts >= end + slack:
            continue
        fitting.append((machine, launch_id))
    if len(fitting) == 1:
        machine, launch_id = fitting[0]
        return _backend(machine, launch_id), machine, None, "fit"
    if not fitting:
        return "unknown", None, None, "unknown"
    candidates = [{"machine": m, "backend": _backend(m, lid)} for m, lid in fitting]
    return "ambiguous", None, candidates, "ambiguous"


def mode_for(archive, ledgers, created_at_min=None, window_slack_seconds=0):
    """The resolved launch's recorded ``mode`` (``"recall"``/``"capture"``/...),
    mirroring ``backend_for``'s own window selection (D-5, Codex #173 review) so the
    two stay consistent for the same ``(archive, created_at_min)`` pair -- ``mode`` was
    indexed by ``index_ledger`` from day one but never actually emitted anywhere in the
    report. ``"unknown"`` (no archive/no ledger match) or ``"ambiguous"`` (more than
    one launch window fits, mirroring ``backend_for``'s own ambiguous case, without the
    per-candidate detail ``backend_for``'s ``candidates`` list carries)."""
    session_uuid = bare_session_uuid(archive)
    if not session_uuid:
        return "unknown"
    windows = _backend_windows_for_session(session_uuid, ledgers)
    if not windows:
        return "unknown"

    def _mode(machine, launch_id):
        starts, _sessions = ledgers[machine]
        return starts.get(launch_id, {}).get("mode") or "unknown"

    slack = timedelta(seconds=window_slack_seconds)
    if len(windows) == 1:
        machine, launch_id, _start_ts, _end_ts = windows[0]
        return _mode(machine, launch_id)
    if created_at_min is None:
        return "ambiguous"
    turn_ts = _parse_ts(created_at_min)
    fitting = []
    for machine, launch_id, start_ts, end_ts in windows:
        start = _parse_ts(start_ts)
        if turn_ts is None or start is None or turn_ts < start - slack:
            continue
        end = _parse_ts(end_ts) if end_ts else None
        if end is not None and turn_ts >= end + slack:
            continue
        fitting.append((machine, launch_id))
    if len(fitting) == 1:
        return _mode(*fitting[0])
    if not fitting:
        return "unknown"
    return "ambiguous"


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


def _candidate_coverage(record, ledgers, ts_slack_seconds, max_candidates):
    """(candidates_tried, candidates_total) for this record's search, or ``None`` when
    every available candidate was searched (nothing incomplete to report). D-2 (Codex
    #173 review): an unresolved record used to look identical whether the search
    covered every ledger candidate or silently stopped partway through the cap."""
    total = len(
        _candidate_sessions(ledgers, record.get("created_at_min"), ts_slack_seconds)
    )
    if total == 0:
        return None
    tried = total if max_candidates is None else min(total, max_candidates)
    return None if tried >= total else (tried, total)


ArchiveResolution = collections.namedtuple(
    "ArchiveResolution", ["archive", "status", "ambiguous_matches", "diagnostics"]
)


def resolve_archive_detail(
    record,
    ledgers,
    reader,
    ts_slack_seconds=DEFAULT_TS_SLACK_SECONDS,
    max_candidates=DEFAULT_MAX_CANDIDATES,
):
    """``ArchiveResolution`` for the record's ``first_message_id``, honoring upstream's
    own terminal-state precedence (D-3, Codex #173 review, 2026-09-27): ``.done``
    (completed) beats ``.failed.json`` (failed) beats neither (pending) --
    ``_archive_terminal_state``, ``openviking/session/session.py:3340-3349``. Only a
    *completed* archive counts as a match.

    ``status`` is ``"resolved"`` (exactly one completed match; ``archive`` is set),
    ``"ambiguous"`` (more than one completed match; ``archive`` is ``None`` and
    ``ambiguous_matches`` lists every completed URI), or ``"unresolved"`` (no completed
    match at all; ``diagnostics`` lists every failed/pending archive that also
    contained the id, for the report's transparency -- never used to pick a winner).

    Upstream's Phase 1 failure handling (``session.py:2215-2232``) restores the
    pre-commit message list and decrements ``compression_index`` on failure, so the
    *same* message ids get archived again as the next ``archive_NNN`` once the retry
    succeeds -- the failed archive's ``messages.jsonl`` still contains them, and the
    old first-wins behaviour returned it even though Phase 2 never ran there
    (BUG-1174's exact shape: a failed ``archive_001`` shadowed the completed
    ``archive_002`` that held the same eight message ids). Candidates are narrowed by
    ``created_at_min``/the ledger's session timestamps *before* any archive is read
    (see ``_candidate_sessions``); every candidate session (bounded by
    ``max_candidates``), user namespace (``_candidate_users``) and session-directory
    variant is searched to completion, never short-circuited on the first match, so a
    genuine ambiguity is never masked.
    """
    first_id = record.get("first_message_id")
    if not first_id:
        return ArchiveResolution(None, "unresolved", [], [])
    completed = []
    diagnostics = []
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
                    if not any(m.get("id") == first_id for m in messages):
                        continue
                    uri = f"viking://user/{user}/sessions/{session_dir}/history/{archive_id}"
                    status = reader.archive_status(user, session_dir, archive_id)
                    if status == "completed":
                        completed.append(uri)
                    else:
                        diagnostics.append((uri, status))
    if len(completed) == 1:
        return ArchiveResolution(completed[0], "resolved", [], diagnostics)
    if len(completed) > 1:
        return ArchiveResolution(None, "ambiguous", completed, diagnostics)
    return ArchiveResolution(None, "unresolved", [], diagnostics)


def resolve_archive(
    record,
    ledgers,
    reader,
    ts_slack_seconds=DEFAULT_TS_SLACK_SECONDS,
    max_candidates=DEFAULT_MAX_CANDIDATES,
):
    """The archive URI whose ``messages.jsonl`` contains the record's
    ``first_message_id`` and whose terminal marker says it completed, or ``None`` when
    it cannot be unambiguously resolved. See ``resolve_archive_detail`` for the
    ambiguous-vs-unresolved distinction and per-archive diagnostics, which
    ``build_report`` uses for the row's archive-status column."""
    return resolve_archive_detail(
        record,
        ledgers,
        reader,
        ts_slack_seconds=ts_slack_seconds,
        max_candidates=max_candidates,
    ).archive


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

    def read_memory_diff(self, user, session_dir, archive_id):
        key = (user, session_dir, archive_id, "memory_diff")
        if key in self._read_cache:
            return self._read_cache[key]
        path = os.path.join(
            self.root,
            user,
            "sessions",
            session_dir,
            "history",
            archive_id,
            "memory_diff.json",
        )
        diff = None
        if os.path.isfile(path):
            with open(path) as fh:
                diff = json.load(fh)
        self._read_cache[key] = diff
        return diff

    def archive_status(self, user, session_dir, archive_id):
        """``"completed"`` / ``"failed"`` / ``"pending"``, mirroring upstream's own
        ``_archive_terminal_state`` marker precedence (D-3): ``.done`` checked before
        ``.failed.json``."""
        archive_dir = os.path.join(
            self.root, user, "sessions", session_dir, "history", archive_id
        )
        if os.path.isfile(os.path.join(archive_dir, ".done")):
            return "completed"
        if os.path.isfile(os.path.join(archive_dir, ".failed.json")):
            return "failed"
        return "pending"


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

    def read_memory_diff(self, user, session_dir, archive_id):
        key = (user, session_dir, archive_id, "memory_diff")
        if key in self._read_cache:
            return self._read_cache[key]
        uri = f"viking://user/{user}/sessions/{session_dir}/history/{archive_id}/memory_diff.json"
        proc = self._run(
            [self.ov_bin, "read", uri, "--user", user, "-o", "json"],
            capture_output=True,
            text=True,
            timeout=self.timeout,
        )
        diff = None
        try:
            payload = json.loads(proc.stdout)
            raw = payload.get("result") if isinstance(payload, dict) else None
            if isinstance(raw, str):
                diff = json.loads(raw)
        except json.JSONDecodeError:
            diff = None
        self._read_cache[key] = diff
        return diff

    def archive_status(self, user, session_dir, archive_id):
        """``"completed"`` / ``"failed"`` / ``"pending"``, mirroring upstream's own
        ``_archive_terminal_state`` marker precedence (D-3): ``.done`` checked before
        ``.failed.json``, via a read-only ``ov ls`` on the archive directory."""
        paths = self._ls(
            f"viking://user/{user}/sessions/{session_dir}/history/{archive_id}", user
        )
        names = {p.rsplit("/", 1)[-1] for p in paths}
        if ".done" in names:
            return "completed"
        if ".failed.json" in names:
            return "failed"
        return "pending"


def resolve_record_archive(
    record,
    ledgers,
    reader,
    ts_slack_seconds=DEFAULT_TS_SLACK_SECONDS,
    max_candidates=DEFAULT_MAX_CANDIDATES,
):
    """The record's archive URI: explicit ``archive`` field if present (forward
    compatibility — no shadow record carries one today), else resolved via ``reader``
    from the turn's message ids (``resolve_archive``); ``None`` when neither yields one
    (no reader given, no ledger data, or no archive anywhere contains the id).

    D-2 (Codex #173 review): ``max_candidates`` used to be silently dropped here, so
    ``build_report``/``main`` could never actually raise the cap they claimed to
    expose. It is threaded through to ``resolve_archive`` now.
    """
    archive = record.get("archive")
    if archive:
        return archive
    if reader is None:
        return None
    return resolve_archive(
        record,
        ledgers,
        reader,
        ts_slack_seconds=ts_slack_seconds,
        max_candidates=max_candidates,
    )


def resolve_record_archive_detail(
    record,
    ledgers,
    reader,
    ts_slack_seconds=DEFAULT_TS_SLACK_SECONDS,
    max_candidates=DEFAULT_MAX_CANDIDATES,
):
    """``ArchiveResolution`` for a record, the detail-carrying counterpart to
    ``resolve_record_archive``: an explicit ``archive`` field resolves trivially
    (status ``"resolved"``, no ambiguity possible); no reader resolves to status
    ``"no-reader"``; otherwise delegates to ``resolve_archive_detail``."""
    archive = record.get("archive")
    if archive:
        return ArchiveResolution(archive, "resolved", [], [])
    if reader is None:
        return ArchiveResolution(None, "no-reader", [], [])
    return resolve_archive_detail(
        record,
        ledgers,
        reader,
        ts_slack_seconds=ts_slack_seconds,
        max_candidates=max_candidates,
    )


def _record_message_count(rec):
    """``message_count`` if the record carries it (the real emitted field), else
    derived from ``turn_end - turn_start`` when both are ints, else ``None``."""
    count = rec.get("message_count")
    if count is not None:
        return count
    start, end = rec.get("turn_start"), rec.get("turn_end")
    if isinstance(start, int) and isinstance(end, int):
        return end - start
    return None


def _turn_identity(rec, archive):
    """The dedup join key for D-1 (Codex #173 review, answer 1): ``(archive,
    first_message_id, last_message_id, message_count)`` -- *after* a verified archive
    join, never on the raw log line or a process-local seen-set (which would lose the
    occurrence/conflict diagnostics and miss cross-process duplicates). ``None`` when
    the record has no resolved archive (an unresolved turn is never deduplicated
    against another unresolved turn -- there is nothing verified to join them on) or
    is missing either message id."""
    if not archive:
        return None
    first_id = rec.get("first_message_id")
    last_id = rec.get("last_message_id")
    if not first_id or not last_id:
        return None
    return (archive, first_id, last_id, _record_message_count(rec))


def _canonicalize_records(records, ledgers, reader, ts_slack_seconds, max_candidates):
    """[(representative_record, resolution, occurrences, duplicate_verdicts), ...].

    Each input record's archive is resolved once (``resolution`` is an
    ``ArchiveResolution``); records sharing a verified identity (see
    ``_turn_identity``, keyed on ``resolution.archive``) collapse into a single
    canonical turn. The first-seen record in a group is kept as the representative
    (its own fields render in the table); ``occurrences`` counts every copy and
    ``duplicate_verdicts`` lists the distinct verdicts seen across the group when they
    conflict, so a retried extraction's inflated count is fixed without hiding a
    genuine disagreement between attempts.
    """
    groups = {}
    order = []
    for rec in records:
        resolution = resolve_record_archive_detail(
            rec,
            ledgers,
            reader,
            ts_slack_seconds=ts_slack_seconds,
            max_candidates=max_candidates,
        )
        identity = _turn_identity(rec, resolution.archive)
        if identity is None:
            # No verified join to dedup on: always its own canonical turn.
            key = object()
        else:
            key = identity
        if key in groups:
            entry = groups[key]
            entry["occurrences"] += 1
            entry["verdicts"].add(rec.get("verdict", "unknown"))
            continue
        entry = {
            "record": rec,
            "resolution": resolution,
            "occurrences": 1,
            "verdicts": {rec.get("verdict", "unknown")},
        }
        groups[key] = entry
        order.append(key)
    canonical = []
    for key in order:
        entry = groups[key]
        duplicate_verdicts = (
            sorted(entry["verdicts"]) if len(entry["verdicts"]) > 1 else None
        )
        canonical.append(
            (
                entry["record"],
                entry["resolution"],
                entry["occurrences"],
                duplicate_verdicts,
            )
        )
    return canonical


def filter_records_by_block(records, since=None, until=None):
    """Records whose ``created_at_min`` falls in ``[since, until)`` (D-5, Codex #173
    review: block scoping via ``--since``/``--until``). Either bound is optional; with
    neither, ``records`` is returned unchanged. A record with no ``created_at_min`` at
    all is always kept -- there is nothing to scope it out on, and dropping it would
    silently hide an unresolvable turn from the block's accounting rather than let it
    show up as unresolved."""
    if since is None and until is None:
        return records
    since_ts = _parse_ts(since) if since else None
    until_ts = _parse_ts(until) if until else None
    kept = []
    for rec in records:
        turn_ts = _parse_ts(rec.get("created_at_min"))
        if turn_ts is None:
            kept.append(rec)
            continue
        if since_ts is not None and turn_ts < since_ts:
            continue
        if until_ts is not None and turn_ts >= until_ts:
            continue
        kept.append(rec)
    return kept


def build_report(
    records,
    ledgers,
    reader=None,
    ts_slack_seconds=DEFAULT_TS_SLACK_SECONDS,
    max_candidates=DEFAULT_MAX_CANDIDATES,
    window_slack_seconds=0,
):
    by_verdict = collections.Counter()
    by_backend = {}
    rows = []
    canonical_turns = _canonicalize_records(
        records, ledgers, reader, ts_slack_seconds, max_candidates
    )
    for rec, resolution, occurrences, duplicate_verdicts in canonical_turns:
        archive = resolution.archive
        verdict = rec.get("verdict", "unknown")
        by_verdict[verdict] += 1
        backend, machine, candidates, window_status = backend_for(
            archive,
            ledgers,
            created_at_min=rec.get("created_at_min"),
            window_slack_seconds=window_slack_seconds,
        )
        mode = mode_for(
            archive,
            ledgers,
            created_at_min=rec.get("created_at_min"),
            window_slack_seconds=window_slack_seconds,
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
        candidate_coverage = None
        if (
            resolution.status == "unresolved"
            and reader is not None
            and not rec.get("archive")
        ):
            candidate_coverage = _candidate_coverage(
                rec, ledgers, ts_slack_seconds, max_candidates
            )
        # D-7 / Codex additional finding 1: reconstruct the complete source turn from
        # the archive and re-derive the verdict; only surface it when it actually
        # differs from what was logged.
        reclassified = reclassify_record(rec, archive, reader)
        reconstructed_verdict = (
            reclassified if reclassified and reclassified != verdict else None
        )
        rows.append(
            {
                "verdict": verdict,
                "reconstructed_verdict": reconstructed_verdict,
                "partial": rec.get("partial"),
                "backend": backend,
                "mode": mode,
                "machine": machine or "unknown",
                "window_status": window_status,
                "ambiguous_candidates": candidates,
                "lab_or_pilot": lab_or_pilot(archive) if archive else "unknown",
                "session": session_uuid,
                "archive": archive,
                "archive_status": resolution.status,
                "archive_ambiguous_matches": resolution.ambiguous_matches or None,
                "archive_diagnostics": resolution.diagnostics or None,
                "candidate_coverage": candidate_coverage,
                "turn_start": rec.get("turn_start"),
                "turn_end": rec.get("turn_end"),
                "first_message_id": rec.get("first_message_id"),
                "last_message_id": rec.get("last_message_id"),
                "ov_tools": rec.get("ov_tools", []),
                "other_tools": rec.get("other_tools", []),
                "errored": rec.get("errored", False),
                "events": _events_for_archive(archive, reader),
                "occurrences": occurrences,
                "duplicate_verdicts": duplicate_verdicts,
                "label": "",
            }
        )
    # D-6 (Codex #173 review): a memory_diff.json is the archive's diff, not any one
    # turn's -- when more than one canonical turn resolves to the same archive (a
    # segmented extraction, or several turns before the next commit), every row
    # sharing that archive is flagged so the Events cell doesn't imply single-turn
    # attribution.
    archive_counts = collections.Counter(
        row["archive"] for row in rows if row["archive"]
    )
    for row in rows:
        row["events_shared_archive"] = bool(
            row["archive"] and archive_counts[row["archive"]] > 1
        )
    return {"by_verdict": by_verdict, "by_backend": by_backend, "rows": rows}


def _row_aggregates(rows):
    """(by_verdict, by_backend) recomputed from a (possibly filtered) row list, in the
    same shape ``build_report`` produces -- used by ``filter_rows_by_lab_sessions`` so
    a filtered report's aggregate section reflects only the rows it actually kept."""
    by_verdict = collections.Counter(row["verdict"] for row in rows)
    by_backend = {}
    for row in rows:
        bucket = by_backend.setdefault(
            row["backend"],
            {"sessions": set(), "turns": 0, "strip": 0, "unresolved_turns": 0},
        )
        if row.get("session"):
            bucket["sessions"].add(row["session"])
        else:
            bucket["unresolved_turns"] += 1
        bucket["turns"] += 1
        if row["verdict"] == "strip":
            bucket["strip"] += 1
    return by_verdict, by_backend


def filter_rows_by_lab_sessions(report_dict, include_lab=True):
    """A new report dict (D-5, ``--lab-sessions include|exclude``, mirroring
    weekly-check) with lab-namespace rows dropped and ``by_verdict``/``by_backend``
    recomputed over what remains. ``include_lab=True`` (the default) returns
    ``report_dict`` unchanged."""
    if include_lab:
        return report_dict
    kept = [row for row in report_dict["rows"] if row["lab_or_pilot"] != "lab"]
    by_verdict, by_backend = _row_aggregates(kept)
    return {"by_verdict": by_verdict, "by_backend": by_backend, "rows": kept}


def _row_key(row) -> str:
    """A stable per-turn key for the label round trip (D-5): built from the resolved
    archive and both message ids when the turn has a verified archive join, so it is
    stable across runs of the same shadow log against the same archive tree. A turn
    with no resolved archive has nothing verified to key on; its fallback key is
    prefixed ``unresolved:`` so it reads as visibly less stable (a different search
    cap or ledger state can change which candidate it lands on, if any)."""
    if (
        row.get("archive")
        and row.get("first_message_id")
        and row.get("last_message_id")
    ):
        return f"{row['archive']}#{row['first_message_id']}..{row['last_message_id']}"
    return f"unresolved:{row.get('machine')}:{row.get('first_message_id')}:{row.get('turn_start')}"


VALID_LABELS = frozenset({"restatement", "new-info", "mixed"})


def parse_labels_file(path):
    """``{row_key: label}`` from a simple ``<key>\\t<label>`` file (D-5): one pair per
    non-blank, non-``#``-comment line. Raises ``ValueError`` on a malformed line or a
    label outside ``VALID_LABELS`` -- the round trip rejects unknown labels rather than
    silently accepting a typo."""
    labels = {}
    with open(path) as fh:
        for lineno, line in enumerate(fh, start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            parts = stripped.split("\t")
            if len(parts) != 2:
                raise ValueError(
                    f"{path}:{lineno}: expected '<key>\\t<label>', got {line!r}"
                )
            key, label = parts[0].strip(), parts[1].strip()
            if label not in VALID_LABELS:
                raise ValueError(
                    f"{path}:{lineno}: unknown label {label!r}, expected one of "
                    f"{sorted(VALID_LABELS)}"
                )
            labels[key] = label
    return labels


def apply_labels(report_dict, labels):
    """Apply a ``{row_key: label}`` mapping onto ``report_dict["rows"]`` in place (via
    ``_row_key``), returning ``report_dict``. Raises ``ValueError`` listing any label
    key that matched no row (D-5: the round trip rejects unknown keys, e.g. from a
    labels file built against a stale run of the report)."""
    remaining = dict(labels)
    for row in report_dict["rows"]:
        key = _row_key(row)
        if key in remaining:
            row["label"] = remaining.pop(key)
    if remaining:
        unmatched = sorted(remaining)
        raise ValueError(
            f"--labels file has {len(unmatched)} key(s) matching no row: "
            f"{unmatched[:5]}{'...' if len(unmatched) > 5 else ''}"
        )
    return report_dict


def compute_precision(rows):
    """``{"precision", "numerator", "denominator", "excluded"}`` over labelled
    ``strip`` rows (D-5). ``precision`` is ``numerator / denominator`` (restatement
    count over every eligible labelled strip row), or ``None`` when the denominator is
    0. ``excluded`` counts every ``strip`` row NOT in the denominator, by the first
    reason that applies (a row counts once): ``"unresolved"`` (no completed archive
    join), ``"ambiguous"`` (archive or backend/window ambiguity), ``"partial"`` (the
    record's own ``partial`` flag, or a D-7 reconstruction that found a different
    verdict than what was logged -- either way the logged strip can't be trusted),
    ``"errored"`` (a tool call in the turn errored), or ``"unlabelled"`` (no label
    supplied for an otherwise-eligible row). A non-``strip`` row is not counted
    anywhere -- precision is specifically about the strip population."""
    excluded = collections.Counter()
    eligible_labels = []
    for row in rows:
        if row["verdict"] != "strip":
            continue
        if row.get("archive_status") in ("unresolved", "no-reader"):
            excluded["unresolved"] += 1
            continue
        if (
            row.get("archive_status") == "ambiguous"
            or row.get("backend") == "ambiguous"
            or row.get("window_status") == "ambiguous"
        ):
            excluded["ambiguous"] += 1
            continue
        if row.get("partial") or row.get("reconstructed_verdict"):
            excluded["partial"] += 1
            continue
        if row.get("errored"):
            excluded["errored"] += 1
            continue
        label = row.get("label")
        if not label:
            excluded["unlabelled"] += 1
            continue
        eligible_labels.append(label)
    denominator = len(eligible_labels)
    numerator = sum(1 for label in eligible_labels if label == "restatement")
    precision = numerator / denominator if denominator else None
    return {
        "precision": precision,
        "numerator": numerator,
        "denominator": denominator,
        "excluded": dict(excluded),
    }


def _cell(value) -> str:
    """A markdown table cell: ``|`` and newlines can't survive in one, so escape/join
    them; ``""``/``None`` renders as an em dash so an empty cell is still visible."""
    if value is None or value == "":
        return "—"
    return str(value).replace("|", "\\|").replace("\n", " ")


def _format_tools(row) -> str:
    parts = []
    if row["ov_tools"]:
        parts.append("ov: " + ", ".join(row["ov_tools"]))
    if row["other_tools"]:
        parts.append("other: " + ", ".join(row["other_tools"]))
    return "; ".join(parts) if parts else "—"


def _format_events(row) -> str:
    events = row.get("events") or []
    if not events:
        return "—"
    rendered = []
    for event in events:
        name = event.get("name") or event.get("uri") or "?"
        operation = event.get("operation")
        label = f"{name} ({operation})" if operation else name
        abstract = event.get("abstract")
        rendered.append(f"{label}: {abstract}" if abstract else label)
    text = "; ".join(rendered)
    if row.get("events_shared_archive"):
        # D-6: the diff is the archive's, not attributed to this turn alone --
        # another canonical turn resolved to the same archive.
        text += " [archive-level diff, not attributed to a single turn]"
    return text


def _format_backend(row) -> str:
    if row["backend"] == "ambiguous" and row.get("ambiguous_candidates"):
        candidates = ", ".join(
            f"{c['machine']}:{c['backend']}" for c in row["ambiguous_candidates"]
        )
        return f"ambiguous ({candidates})"
    if row.get("window_status") == "outside-window":
        # D-4: a single-launch session attributed despite falling outside its own
        # window (a detached write, a replayed queue, or clock skew) -- flagged, not
        # hidden.
        return f"{row['backend']} (outside-window)"
    return row["backend"]


def _format_verdict(row) -> str:
    verdict = row["verdict"]
    if row.get("duplicate_verdicts"):
        verdict += f" (conflicting on retry: {', '.join(row['duplicate_verdicts'])})"
    if row.get("partial"):
        verdict += " (partial)"
    if row.get("reconstructed_verdict"):
        # D-7: the logged verdict didn't see the full source turn.
        verdict += f" [reconstructed: {row['reconstructed_verdict']}]"
    return verdict


def _format_occurrences(row) -> str:
    occurrences = row.get("occurrences", 1)
    return str(occurrences) if occurrences != 1 else "—"


def _format_archive(row) -> str:
    """The archive cell: the resolved URI; an ``ambiguous (…)`` note listing every
    completed candidate when D-3's terminal-state check found more than one; or an
    explicit search-coverage note (D-2) when the record is unresolved because
    ``--max-candidates`` cut the search short rather than because every candidate was
    actually searched and none matched."""
    archive = row.get("archive")
    if archive:
        return archive
    if row.get("archive_status") == "ambiguous" and row.get(
        "archive_ambiguous_matches"
    ):
        return "ambiguous (" + ", ".join(row["archive_ambiguous_matches"]) + ")"
    coverage = row.get("candidate_coverage")
    if coverage:
        tried, total = coverage
        return f"unresolved ({tried}/{total} candidates searched)"
    return archive


def render_markdown(report, precision=None) -> str:
    lines = [
        "## Turns",
        "",
        (
            "| Key | Machine | Backend | Mode | Session | Archive | Turn range | "
            "Verdict | Occ | Tools | Events | Label (restatement / new-info / mixed) |"
        ),
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in report["rows"]:
        turn_range = f"{row.get('turn_start')}-{row.get('turn_end')}"
        lines.append(
            "| "
            + " | ".join(
                _cell(v)
                for v in (
                    _row_key(row),
                    row["machine"],
                    _format_backend(row),
                    row.get("mode"),
                    row.get("session"),
                    _format_archive(row),
                    turn_range,
                    _format_verdict(row),
                    _format_occurrences(row),
                    _format_tools(row),
                    _format_events(row),
                    row.get("label"),
                )
            )
            + " |"
        )

    lines += [
        "",
        "## Aggregate summary",
        "",
        "### Turns by verdict",
        "",
        "| Verdict | Count |",
        "| --- | --- |",
    ]
    for verdict, count in sorted(report["by_verdict"].items()):
        lines.append(f"| {verdict} | {count} |")
    lines += [
        "",
        "### Per-backend summary",
        "",
        "| Backend | Sessions | Recall turns | Strip verdicts | Unresolved turns |",
        "| --- | --- | --- | --- | --- |",
    ]
    for backend, bucket in sorted(report["by_backend"].items()):
        lines.append(
            f"| {backend} | {len(bucket['sessions'])} | {bucket['turns']} | "
            f"{bucket['strip']} | {bucket['unresolved_turns']} |"
        )

    if precision is not None:
        # D-5: the gate number, with its denominator and every exclusion reason
        # explicit -- never just a bare percentage.
        lines += ["", "## Precision", ""]
        if precision["precision"] is None:
            lines.append("No eligible labelled strip rows -- precision is undefined.")
        else:
            lines.append(
                f"**{precision['precision']:.1%}** "
                f"({precision['numerator']} restatement / "
                f"{precision['denominator']} labelled strip rows)"
            )
        lines += ["", "| Excluded reason | Count |", "| --- | --- |"]
        for reason, count in sorted(precision["excluded"].items()):
            lines.append(f"| {reason} | {count} |")

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
    parser.add_argument(
        "--max-candidates",
        type=int,
        default=DEFAULT_MAX_CANDIDATES,
        help=(
            "Cap the number of closest-preceding ledger candidates searched per "
            "record (D-2, Codex #173 review). Unlimited by default; set this only to "
            "trade completeness for a faster search."
        ),
    )
    parser.add_argument(
        "--window-slack-seconds",
        type=int,
        default=0,
        help=(
            "Symmetric tolerance applied to both bounds of a multi-launch session's "
            "windows when a turn's created_at_min is used to disambiguate them (D-4, "
            "Codex #173 review). A single-launch session always attributes regardless "
            "of this value. 0 by default; set this to account for known cluster/"
            "machine clock skew."
        ),
    )
    parser.add_argument(
        "--since",
        help=(
            "Keep only records whose created_at_min is at or after this ISO8601 "
            "timestamp (D-5, block scoping). A record with no created_at_min is "
            "always kept."
        ),
    )
    parser.add_argument(
        "--until",
        help=(
            "Keep only records whose created_at_min is strictly before this ISO8601 "
            "timestamp (D-5, block scoping). A record with no created_at_min is "
            "always kept."
        ),
    )
    parser.add_argument(
        "--lab-sessions",
        choices=("include", "exclude"),
        default="include",
        help=(
            "Include or exclude noot-pilot-lab rows from the report (D-5, mirrors "
            "weekly-check). Included by default."
        ),
    )
    parser.add_argument(
        "--labels",
        help=(
            "Path to a <key>\\t<label> file (restatement|new-info|mixed per row key "
            "from the rendered Key column) to read back onto the Label column and "
            "compute precision over the labelled strip rows (D-5)."
        ),
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
    records = filter_records_by_block(records, since=args.since, until=args.until)
    ledgers = load_ledgers(args.ledger)
    reader = _build_reader(args)
    report = build_report(
        records,
        ledgers,
        reader=reader,
        ts_slack_seconds=args.ts_slack_seconds,
        max_candidates=args.max_candidates,
        window_slack_seconds=args.window_slack_seconds,
    )
    report = filter_rows_by_lab_sessions(
        report, include_lab=(args.lab_sessions == "include")
    )
    precision = None
    if args.labels:
        labels = parse_labels_file(args.labels)
        apply_labels(report, labels)
        precision = compute_precision(report["rows"])
    text = render_markdown(report, precision=precision)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text)
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
