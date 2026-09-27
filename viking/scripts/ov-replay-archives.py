#!/usr/bin/env python3
"""IMPR-1188 Phase 3.2: a hardened ov-test replay tool, stdlib only.

Talks only to an ov-test port-forward. Before any write it verifies the target's
identity from a server marker unique to ov-test — its ``ov.conf`` vectordb collection
name, read back through the read-only ``GET /api/v1/observer/system`` endpoint. Verified
live, 2026-09-27: ov-test's ``openviking-test-config`` ConfigMap sets
``storage.vectordb.name = "context_test"``; prod's ``openviking-standalone-config`` sets
it to ``"context"``. A target whose ``observer/system`` response does not mention
``context_test`` is refused before a single write is attempted.

Two modes:

``--self-test``     Round-trips one synthetic session: a fresh user, two messages (a
                     question and an answer with a fact), commit with
                     ``keep_recent_count=0``, wait for the extraction task, and check the
                     ``memory_diff.json`` for at least one operation — a terminal task
                     alone is not proof extraction ran. An empty diff is a legitimate
                     result (not raised as an error): the receipt is always written, with
                     ``successful_extraction``/``ok`` set accordingly, and ``main()``
                     exits nonzero when it is false. The session is deleted afterward.
                     Run live 2026-09-27 against real ov-test (the one sanctioned round
                     trip): the create/post/commit/poll/read/delete mechanics all worked,
                     and a trivial two-line exchange ("what did we decide about the
                     widget color?" / "We decided the widget ships in cobalt blue.")
                     extracted 0 operations — it proved the round trip, not a successful
                     extraction on this fixture.
``--replay``         Reads one production archive's ``messages.jsonl`` (read-only, from
                     ``--prod-base-url``) and replays it into ov-test under a fresh user.
                     Every part with externalization fields is hydrated through the
                     read-only tool-results API (``GET
                     /api/v1/sessions/{session_id}/tool-results/{tool_result_id}``,
                     ``read_tool_result``/``hydrate_tool_output``) — never a raw content
                     read of the pointer itself, which names a directory, not a file
                     (``tool_result_store.py:141,240,274``, confirmed live 2026-09-28). A
                     part is never dropped: a failed hydration keeps it with a marker and
                     lists it in the receipt's ``unresolved_tool_outputs``, and every
                     externalization field is stripped either way so ov-test never holds
                     a pointer into prod. **UNVERIFIED as of 2026-09-27**: this path was
                     written from the ``AddMessageRequest``/commit/task-polling shapes
                     read from ov-test's live OpenAPI schema and the real ``memory_diff.json``
                     shape captured by the self-test run above, but has not itself been run
                     end to end — the baseline Kinde-trio replay this feeds is explicit
                     manual verification, run separately once prod's Semantic queue is idle.

Scheduling (ov-test extracts on the same ``gemma4:vlm`` as prod, IMPR-1188 plan § 3.2):
run replays one at a time; ``--prod-base-url`` is required for both ``--self-test`` and
``--replay`` (a self-test commit extracts too), and ``preflight_prod_queue`` runs before
either ever writes to ov-test — it checks both the ``Semantic`` and ``Semantic-Nodes``
rows of prod's queue (``GET /api/v1/observer/system``, ``components.queue.status``; an
earlier version checked only ``Semantic`` and missed Semantic-Nodes work, caught live
2026-09-24), and treats any read failure as busy rather than assuming idle. A busy or
unreadable prod queue raises ``ProdBusyError`` and aborts before a session is created —
never a wait-and-retry loop. Keep clear of the IMPR-1199 idle-queue H7 rerun and of prod
pod restarts regardless.

Archived production messages carry only ``id``, ``role``, ``parts``, ``created_at``,
``peer_id`` in the common case (verified 2026-09-27 against sampled archives) — no
``turn_id``/``message_kind``/``source_message_ids``. ``_message_payload`` sends a field
only when the source has it; it never sends an explicit ``null`` for a missing one.

Usage:
    ov-replay-archives.py --self-test --base-url http://127.0.0.1:<forwarded-port>
    ov-replay-archives.py --replay --base-url http://127.0.0.1:<ov-test-port> \\
        --prod-base-url http://127.0.0.1:<prod-port> \\
        --session-uri viking://user/noot-pilot/sessions/cc-<id> --archive-id archive_003

The API key is read from ``--api-key`` or the ``OPENVIKING_API_KEY`` environment
variable (the same key both stacks use — it is not stack-specific in trusted mode).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

DEFAULT_TIMEOUT = 30
DEFAULT_EXTRACTION_TIMEOUT = 300
# ov-test's ov.conf storage.vectordb.name (prod's is "context"); the identity marker.
EXPECTED_VIKINGDB_COLLECTION = "context_test"
DONE_TASK_STATUSES = {"completed", "done", "success", "succeeded"}
FAILED_TASK_STATUSES = {"failed", "error", "cancelled"}


class TargetIdentityError(RuntimeError):
    """The target does not look like ov-test; refuse to write."""


class ReplayError(RuntimeError):
    """The replay could not complete."""


def _request(
    base_url, method, path, api_key, account, user, body=None, timeout=DEFAULT_TIMEOUT
):
    url = f"{base_url.rstrip('/')}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("X-OpenViking-Account", account)
    req.add_header("X-OpenViking-User", user)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        payload = json.loads(raw) if raw else {}
        raise ReplayError(f"{method} {path} -> HTTP {exc.code}: {payload}") from exc
    payload = json.loads(raw)
    if payload.get("status") != "ok":
        raise ReplayError(f"{method} {path} failed: {payload.get('error')}")
    return payload.get("result")


def verify_ov_test_identity(
    base_url,
    api_key,
    account="default",
    user="ov-replay-identity-check",
    timeout=DEFAULT_TIMEOUT,
):
    """Refuse anything that isn't ov-test. Read-only: GET /api/v1/observer/system."""
    result = _request(
        base_url,
        "GET",
        "/api/v1/observer/system",
        api_key,
        account,
        user,
        timeout=timeout,
    )
    vikingdb_status = (
        (result or {}).get("components", {}).get("vikingdb", {}).get("status", "")
    )
    if EXPECTED_VIKINGDB_COLLECTION not in vikingdb_status:
        raise TargetIdentityError(
            f"{base_url} does not look like ov-test: its vikingdb status does not "
            f"mention the {EXPECTED_VIKINGDB_COLLECTION!r} collection — refusing to write"
        )
    return result


class ProdBusyError(RuntimeError):
    """Prod's queue is not confirmed idle; refuse to start an ov-test round trip that
    would add load to the same shared VLM."""


# Both rows share the gemma4:vlm extraction queue with ov-test: "Semantic" is the
# extraction step itself, "Semantic-Nodes" is its sub-work. A round trip is only safe
# to start when neither has pending or in-progress work.
_QUEUE_ROWS_TO_CHECK = ("Semantic", "Semantic-Nodes")


def _queue_row(status_text, name):
    """(pending, in_progress) strings for `name`'s row in the fixed-width queue table,
    or ``None`` when that row is not present."""
    for line in status_text.splitlines():
        cells = [c.strip() for c in line.strip("|").split("|")]
        if cells and cells[0] == name and len(cells) >= 3:
            return cells[1], cells[2]
    return None


def prod_semantic_queue_idle(
    prod_base_url,
    api_key,
    account="default",
    user="ov-replay-queue-check",
    timeout=DEFAULT_TIMEOUT,
):
    """(idle: bool, raw queue status text) from prod's own observer/system. Read-only.

    Checks both ``Semantic`` and ``Semantic-Nodes`` — an earlier version checked only
    ``Semantic`` and reported idle while ``Semantic-Nodes`` still had pending/in-progress
    work (caught live, 2026-09-24 queue sample: Semantic 0/0, Semantic-Nodes 9/1).
    ``idle`` is ``None``, not ``True``, when any checked row's shape is unrecognised —
    an unreadable row is never assumed idle.
    """
    result = _request(
        prod_base_url,
        "GET",
        "/api/v1/observer/system",
        api_key,
        account,
        user,
        timeout=timeout,
    )
    status_text = (
        (result or {}).get("components", {}).get("queue", {}).get("status", "")
    )
    rows = {name: _queue_row(status_text, name) for name in _QUEUE_ROWS_TO_CHECK}
    if any(row is None for row in rows.values()):
        return None, status_text  # queue table shape unrecognised; caller decides
    idle = all(
        pending == "0" and in_progress == "0" for pending, in_progress in rows.values()
    )
    return idle, status_text


def preflight_prod_queue(
    prod_base_url,
    api_key,
    account="default",
    user="ov-replay-queue-check",
    timeout=DEFAULT_TIMEOUT,
):
    """Fail-closed gate: raise ``ProdBusyError`` unless prod's Semantic and
    Semantic-Nodes queues are both confirmed idle. Any error reading prod — a network
    failure, an auth error, an unrecognised table shape — counts as busy; this runs
    before every ov-test write in both ``--self-test`` and ``--replay``.
    """
    try:
        idle, status_text = prod_semantic_queue_idle(
            prod_base_url, api_key, account=account, user=user, timeout=timeout
        )
    except ReplayError as exc:
        raise ProdBusyError(
            f"could not read prod's queue status at {prod_base_url}; treating as busy: {exc}"
        ) from exc
    if idle is not True:
        raise ProdBusyError(
            f"prod's Semantic/Semantic-Nodes queue is not confirmed idle at "
            f"{prod_base_url}:\n{status_text}"
        )


def _fresh_user(prefix="ov-replay"):
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def create_session(base_url, api_key, account, user, timeout=DEFAULT_TIMEOUT):
    result = _request(
        base_url,
        "POST",
        "/api/v1/sessions",
        api_key,
        account,
        user,
        body={},
        timeout=timeout,
    )
    return result["session_id"]


def delete_session(
    base_url, api_key, account, user, session_id, timeout=DEFAULT_TIMEOUT
):
    return _request(
        base_url,
        "DELETE",
        f"/api/v1/sessions/{session_id}",
        api_key,
        account,
        user,
        timeout=timeout,
    )


def _message_payload(message: dict) -> dict:
    """An AddMessageRequest body from an archived (or synthetic) message dict.

    Only ``role`` and ``parts`` are required; every optional field is included only when
    present on the source. Archived production messages commonly carry none of
    ``turn_id``/``message_kind``/``source_message_ids`` — send nothing rather than null.
    """
    payload = {"role": message["role"], "parts": message.get("parts", [])}
    for key in (
        "peer_id",
        "created_at",
        "turn_id",
        "message_kind",
        "source_message_ids",
    ):
        value = message.get(key)
        if value is not None:
            payload[key] = value
    return payload


def post_message(
    base_url, api_key, account, user, session_id, message, timeout=DEFAULT_TIMEOUT
):
    return _request(
        base_url,
        "POST",
        f"/api/v1/sessions/{session_id}/messages",
        api_key,
        account,
        user,
        body=_message_payload(message),
        timeout=timeout,
    )


def commit_session(
    base_url,
    api_key,
    account,
    user,
    session_id,
    keep_recent_count=0,
    timeout=DEFAULT_TIMEOUT,
):
    return _request(
        base_url,
        "POST",
        f"/api/v1/sessions/{session_id}/commit",
        api_key,
        account,
        user,
        body={"keep_recent_count": keep_recent_count},
        timeout=timeout,
    )


def _task_id_from_commit_result(result):
    if isinstance(result, dict):
        for key in ("task_id", "extraction_task_id"):
            if result.get(key):
                return result[key]
    return None


def wait_for_task(
    base_url,
    api_key,
    account,
    user,
    task_id,
    timeout=DEFAULT_EXTRACTION_TIMEOUT,
    interval=5,
    sleep=time.sleep,
    clock=time.monotonic,
):
    deadline = clock() + timeout
    last = None
    while clock() < deadline:
        last = _request(
            base_url, "GET", f"/api/v1/tasks/{task_id}", api_key, account, user
        )
        status = str((last or {}).get("status", "")).lower()
        if status in DONE_TASK_STATUSES:
            return last
        if status in FAILED_TASK_STATUSES:
            raise ReplayError(f"task {task_id} failed: {last}")
        sleep(interval)
    raise ReplayError(f"task {task_id} did not finish within {timeout}s; last={last}")


def read_content(base_url, api_key, account, user, uri, timeout=DEFAULT_TIMEOUT):
    """Read-only: GET /api/v1/content/read for `uri`, raw and unbudgeted."""
    path = (
        f"/api/v1/content/read?uri={urllib.parse.quote(uri, safe='')}&raw=true&limit=-1"
    )
    result = _request(base_url, "GET", path, api_key, account, user, timeout=timeout)
    return result.get("content") if isinstance(result, dict) else result


def read_memory_diff(
    base_url,
    api_key,
    account,
    user,
    session_id,
    archive_id="archive_001",
    timeout=DEFAULT_TIMEOUT,
):
    uri = f"viking://user/{user}/sessions/{session_id}/history/{archive_id}/memory_diff.json"
    content = read_content(base_url, api_key, account, user, uri, timeout=timeout)
    return json.loads(content) if isinstance(content, str) else content


def _diff_operations(diff):
    """(adds, updates) from a ``memory_diff.json`` payload.

    The real shape (captured live, 2026-09-27, self-test run against ov-test) nests
    them under ``operations``: ``{"archive_uri":..., "operations": {"adds": [...],
    "updates": [...], "deletes": [...]}, "skipped_operations": [...], "summary": {...}}``.
    A flat top-level ``adds``/``updates`` is also accepted, in case another archive or a
    future version renders differently — never assumed, always the real key when present.
    """
    diff = diff or {}
    operations = diff.get("operations")
    if isinstance(operations, dict):
        return list(operations.get("adds", [])), list(operations.get("updates", []))
    return list(diff.get("adds", [])), list(diff.get("updates", []))


def self_test(
    base_url,
    api_key,
    prod_base_url,
    account="default",
    prod_account="default",
    prod_user="ov-replay-queue-check",
    timeout=DEFAULT_TIMEOUT,
    extraction_timeout=DEFAULT_EXTRACTION_TIMEOUT,
):
    """Preflight prod's queue, refuse a non-ov-test target, then round-trip one
    synthetic session on ov-test. `prod_base_url` is required: self-test's commit
    extracts on the same shared VLM as prod, so it is gated exactly like `--replay`.
    """
    preflight_prod_queue(
        prod_base_url, api_key, account=prod_account, user=prod_user, timeout=timeout
    )
    verify_ov_test_identity(base_url, api_key, account=account, timeout=timeout)
    user = _fresh_user("ov-replay-selftest")
    session_id = create_session(base_url, api_key, account, user, timeout=timeout)
    receipt = {"base_url": base_url, "user": user, "session_id": session_id}
    try:
        post_message(
            base_url,
            api_key,
            account,
            user,
            session_id,
            {
                "role": "user",
                "parts": [
                    {
                        "type": "text",
                        "text": "ov-replay-archives self-test: what did we decide about the widget color?",
                    }
                ],
            },
            timeout=timeout,
        )
        post_message(
            base_url,
            api_key,
            account,
            user,
            session_id,
            {
                "role": "assistant",
                "parts": [
                    {
                        "type": "text",
                        "text": "We decided the widget ships in cobalt blue.",
                    }
                ],
            },
            timeout=timeout,
        )
        commit_result = commit_session(
            base_url, api_key, account, user, session_id, timeout=timeout
        )
        receipt["commit_result"] = commit_result
        task_id = _task_id_from_commit_result(commit_result)
        if task_id:
            receipt["task"] = wait_for_task(
                base_url, api_key, account, user, task_id, timeout=extraction_timeout
            )
        diff = read_memory_diff(
            base_url, api_key, account, user, session_id, timeout=timeout
        )
        adds, updates = _diff_operations(diff)
        receipt["memory_diff"] = diff
        # A terminal task is not proof of a successful extraction -- an empty diff is
        # a legitimate result for a trivial exchange, not raised as an error, so the
        # receipt is always written and main() reports it via the exit code instead.
        receipt["successful_extraction"] = bool(adds or updates)
        receipt["ok"] = receipt["successful_extraction"]
    finally:
        try:
            delete_session(
                base_url, api_key, account, user, session_id, timeout=timeout
            )
            receipt["cleaned_up"] = True
        except Exception as exc:  # noqa: BLE001 -- cleanup best-effort; the receipt still stands
            receipt["cleanup_error"] = repr(exc)
    return receipt


# Externalization fields a ToolPart can carry (message/part.py). None of these may
# reach ov-test -- they are prod storage pointers -- so every part that has any of
# them gets a fields-stripped copy, hydrated or not.
EXTERNALIZATION_FIELDS = (
    "tool_output_ref",
    "tool_output_storage_uri",
    "tool_output_source_ref",
    "tool_output_truncated",
    "tool_output_source_offset",
    "tool_output_source_limit",
    "tool_output_original_chars",
)
UNRESOLVED_TOOL_OUTPUT_MARKER = (
    "[ov-replay-archives: externalized tool output could not be hydrated]"
)


def _tool_result_id_from_ref(ref: str) -> str:
    """``viking://user/u/sessions/cc-x/tool-results/<id>[/]`` -> ``<id>``."""
    return ref.rstrip("/").rsplit("/", 1)[-1]


def read_tool_result(
    prod_base_url,
    api_key,
    account,
    user,
    session_id,
    tool_result_id,
    offset=0,
    limit=-1,
    timeout=DEFAULT_TIMEOUT,
):
    """Read-only: GET /api/v1/sessions/{session_id}/tool-results/{tool_result_id}.

    The tool-result API, not a raw content read: the storage layout is a directory
    plus ``output.txt`` (``tool_result_store.py:141`` write, ``:240``/``:274`` read;
    ``ToolResultStore._result_uri`` returns the directory a part's ``tool_output_ref``
    names, confirmed live in the openviking-test v0.4.20 image, 2026-09-28), so a raw
    read of the ref itself is the wrong URI — this endpoint does the same
    offset/limit-scoped read the server's own extraction hydration
    (``Session._hydrate_tool_outputs_for_extraction``) uses internally.
    """
    params = urllib.parse.urlencode(
        {
            "offset": max(0, int(offset or 0)),
            "limit": int(limit) if limit is not None else -1,
            "include_metadata": "false",
        }
    )
    path = f"/api/v1/sessions/{session_id}/tool-results/{tool_result_id}?{params}"
    return _request(prod_base_url, "GET", path, api_key, account, user, timeout=timeout)


def hydrate_tool_output(
    prod_base_url, api_key, account, user, session_id, part, timeout=DEFAULT_TIMEOUT
):
    """(hydrated_part, unresolved: bool). The tool part is never dropped: on a failed
    read it is returned with ``tool_output`` set to a marker and ``unresolved=True``
    (the caller lists it in the receipt); every externalization field is cleared from
    the returned part either way, so ov-test never holds a pointer into prod.

    Mirrors the server's own hydration precedence
    (``Session._hydrate_tool_outputs_for_extraction``, session.py): when
    ``tool_output_source_ref`` is present it wins over ``tool_output_ref`` and is read
    with its own ``tool_output_source_offset``/``tool_output_source_limit`` (a source
    slice, preserved rather than re-fetching the whole thing); otherwise, only a
    ``tool_output_truncated`` part is re-fetched in full (offset 0, limit -1) — an
    untruncated part with no source ref already carries its full output inline and is
    just stripped of its (now-irrelevant) prod pointer fields, no read attempted.
    """
    has_externalization = any(key in part for key in EXTERNALIZATION_FIELDS)
    if not has_externalization:
        return dict(part), False

    cleared = {k: v for k, v in part.items() if k not in EXTERNALIZATION_FIELDS}
    ref = part.get("tool_output_source_ref") or part.get("tool_output_ref")
    needs_fetch = bool(
        part.get("tool_output_truncated") or part.get("tool_output_source_ref")
    )
    if not needs_fetch or not ref:
        return cleared, False

    tool_result_id = _tool_result_id_from_ref(ref)
    if part.get("tool_output_source_ref"):
        offset = part.get("tool_output_source_offset") or 0
        limit = part.get("tool_output_source_limit")
        if limit is None and part.get("tool_output_original_chars") is not None:
            limit = part["tool_output_original_chars"]
        if limit is None:
            limit = -1
    else:
        offset, limit = 0, -1

    try:
        result = read_tool_result(
            prod_base_url,
            api_key,
            account,
            user,
            session_id,
            tool_result_id,
            offset=offset,
            limit=limit,
            timeout=timeout,
        )
        content = (result or {}).get("content", "") if isinstance(result, dict) else ""
    except ReplayError:
        cleared["tool_output"] = UNRESOLVED_TOOL_OUTPUT_MARKER
        return cleared, True

    cleared["tool_output"] = content
    return cleared, False


def read_production_messages(
    prod_base_url,
    api_key,
    account,
    user,
    session_uri,
    archive_id,
    timeout=DEFAULT_TIMEOUT,
):
    """Read-only: one production archive's messages.jsonl, parsed."""
    uri = f"{session_uri}/history/{archive_id}/messages.jsonl"
    content = read_content(prod_base_url, api_key, account, user, uri, timeout=timeout)
    lines = content.splitlines() if isinstance(content, str) else []
    return [json.loads(line) for line in lines if line.strip()]


def replay_archive(
    base_url_test,
    api_key,
    account,
    prod_base_url,
    prod_account,
    prod_user,
    session_uri,
    archive_id,
    keep_recent_count=0,
    timeout=DEFAULT_TIMEOUT,
    extraction_timeout=DEFAULT_EXTRACTION_TIMEOUT,
):
    """UNVERIFIED end to end as of 2026-09-27 — see the module docstring. Reads from
    `prod_base_url` only; every write goes to `base_url_test`, whose identity is
    verified first, after prod's queue is confirmed idle.
    """
    preflight_prod_queue(
        prod_base_url, api_key, account=prod_account, user=prod_user, timeout=timeout
    )
    verify_ov_test_identity(base_url_test, api_key, account=account, timeout=timeout)
    messages = read_production_messages(
        prod_base_url,
        api_key,
        prod_account,
        prod_user,
        session_uri,
        archive_id,
        timeout=timeout,
    )
    # The tool-results API is scoped to the *source* (prod) session, not the fresh
    # target session created below.
    source_session_id = session_uri.rstrip("/").rsplit("/", 1)[-1]
    unresolved = []
    hydrated_messages = []
    for message in messages:
        parts = []
        for part in message.get("parts", []):
            hydrated, part_unresolved = hydrate_tool_output(
                prod_base_url,
                api_key,
                prod_account,
                prod_user,
                source_session_id,
                part,
                timeout=timeout,
            )
            if part_unresolved:
                unresolved.append(
                    {
                        "message_id": message.get("id"),
                        "tool_output_ref": part.get("tool_output_source_ref")
                        or part.get("tool_output_ref"),
                    }
                )
            parts.append(hydrated)  # never dropped, hydrated or not
        hydrated_messages.append({**message, "parts": parts})

    user = _fresh_user("ov-replay")
    session_id = create_session(base_url_test, api_key, account, user, timeout=timeout)
    receipt = {
        "source_session_uri": session_uri,
        "source_archive": archive_id,
        "target_user": user,
        "target_session_id": session_id,
        "unresolved_tool_outputs": unresolved,
    }
    for message in hydrated_messages:
        post_message(
            base_url_test, api_key, account, user, session_id, message, timeout=timeout
        )
    commit_result = commit_session(
        base_url_test,
        api_key,
        account,
        user,
        session_id,
        keep_recent_count=keep_recent_count,
        timeout=timeout,
    )
    receipt["commit_result"] = commit_result
    task_id = _task_id_from_commit_result(commit_result)
    if task_id:
        receipt["task"] = wait_for_task(
            base_url_test, api_key, account, user, task_id, timeout=extraction_timeout
        )
    diff = read_memory_diff(
        base_url_test, api_key, account, user, session_id, timeout=timeout
    )
    adds, updates = _diff_operations(diff)
    ops = adds + updates
    receipt["memory_diff"] = diff
    # "Successful" means a non-error operation, not just a terminal task.
    receipt["successful_extraction"] = bool(ops) and not any(
        op.get("error") for op in ops
    )
    return receipt


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--self-test",
        action="store_true",
        help="Round-trip one synthetic session on ov-test.",
    )
    mode.add_argument(
        "--replay",
        action="store_true",
        help="Replay one production archive into ov-test.",
    )
    parser.add_argument(
        "--base-url",
        required=True,
        help="ov-test port-forward base URL (e.g. http://127.0.0.1:1933).",
    )
    parser.add_argument("--api-key", default=os.environ.get("OPENVIKING_API_KEY"))
    parser.add_argument("--account", default="default")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument(
        "--extraction-timeout", type=float, default=DEFAULT_EXTRACTION_TIMEOUT
    )
    parser.add_argument(
        "--receipt", help="Write the JSON receipt to this path instead of stdout."
    )
    parser.add_argument(
        "--prod-base-url",
        required=True,
        help=(
            "Prod port-forward base URL, read-only. Required for both modes: the "
            "fail-closed preflight checks prod's queue before any ov-test write, "
            "since a self-test commit extracts on the same shared VLM as a replay."
        ),
    )
    parser.add_argument("--prod-account", default="default")
    parser.add_argument("--prod-user", default="noot-pilot")
    parser.add_argument(
        "--session-uri",
        help="Required with --replay: viking://user/<user>/sessions/cc-<id>.",
    )
    parser.add_argument(
        "--archive-id", default="archive_001", help="Required with --replay."
    )
    parser.add_argument("--keep-recent-count", type=int, default=0)
    args = parser.parse_args(argv)
    if not args.api_key:
        parser.error("--api-key or OPENVIKING_API_KEY is required")
    if args.replay and not args.session_uri:
        parser.error("--replay requires --session-uri")
    return args


def main(argv=None):
    args = parse_args(argv)
    try:
        if args.self_test:
            receipt = self_test(
                args.base_url,
                args.api_key,
                args.prod_base_url,
                account=args.account,
                prod_account=args.prod_account,
                prod_user=args.prod_user,
                timeout=args.timeout,
                extraction_timeout=args.extraction_timeout,
            )
        else:
            receipt = replay_archive(
                args.base_url,
                args.api_key,
                args.account,
                args.prod_base_url,
                args.prod_account,
                args.prod_user,
                args.session_uri,
                args.archive_id,
                keep_recent_count=args.keep_recent_count,
                timeout=args.timeout,
                extraction_timeout=args.extraction_timeout,
            )
    except (TargetIdentityError, ReplayError, ProdBusyError) as exc:
        print(f"ov-replay-archives: {exc}", file=sys.stderr)
        return 1

    text = json.dumps(receipt, indent=2, sort_keys=True, default=str)
    if args.receipt:
        with open(args.receipt, "w") as fh:
            fh.write(text + "\n")
    else:
        print(text)
    # The receipt is written either way; only the exit code reports success/failure --
    # a failed extraction requirement must not exit 0 for either mode.
    if not receipt.get("successful_extraction"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
