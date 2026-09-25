#!/usr/bin/env python3
"""IMPR-1200 event-abstract backfill / restore for OpenViking v0.4.20.

Moves existing ``events`` memory vector records onto the ``ov_event_abstract_patch``
Summary-only abstract (or back, with ``--restore``) through the real write path,
``MemoryUpdater._vectorize_memories``, so the embedding text is what a fresh write would
produce. ``POST /api/v1/content/reindex`` is the wrong tool: it embeds the raw file body
(MEMORY_FIELDS comment and links included) instead of the write-path text.

Three modes:

``--dry-run``   READ-ONLY. Talks to a running server over REST (``--server``, default
                the pod's own ``http://127.0.0.1:1933``) with the key in
                ``OPENVIKING_API_KEY`` (or ``API_KEY``, as the pod's env names it), lists
                and reads the event files, and reports what a backfill would change. It
                never constructs an ``OpenVikingService``, whose ``initialize()`` can
                create collections, fill preset-directory metadata, enqueue directory
                vectors and start workers. Runs anywhere ``openviking`` is importable
                (the helpers it uses are pure functions).
(default)       Backfill. Runs INSIDE an OpenViking pod as a second ``OpenVikingService``
                on a scratch workspace (``--workspace``, default a fresh temp dir; never
                the server's own ``storage.workspace``, which holds its PID lock), so it
                talks to the same vector DB / AGFS backends the pod's ``ov.conf`` names.
``--restore``   As backfill, but with the stock full-body abstract: resolves
                ``EmbeddingMsgConverter.from_context`` through ``inspect.unwrap`` for the
                run, so a rollback does not depend on the patch switch. It rebuilds
                today's full-body abstract from the current file; it is not a snapshot
                of whatever the record held before.

Safety (Codex review, 2026-09-25):

* **Scope.** ``root_uri`` must be the canonical events namespace of ``--user``:
  ``viking://user/<user>[/peers/<peer>]/memories/events`` or a date directory or single
  ``.md`` file under it. Every selected URI is re-checked against that before anything is
  enqueued; a mismatch aborts the run with nothing written.
* **Template.** The events schema's ``embedding_template`` is rendered here without the
  ``extract_context`` the write path passes. v0.4.20's template does not use it; a
  template that references ``extract_context`` is refused rather than rendered wrongly.
* **Delivery.** After enqueueing, the run waits for this process's queue counters to
  drain (``QueueManager.wait_complete``), then polls every record by
  ``vector_record_id(account, uri, 2)`` until its stored abstract matches what was
  enqueued, all within ``--wait-timeout``. The poll is what counts: the queue lives on
  shared AGFS storage, so the live server's workers can take a message this process
  enqueued, and these counters then read "complete" while the embedding is still being
  computed (seen on ov-test, 2026-09-25). The scratch workspace is removed only if the
  queue drained; otherwise it is kept and its path printed.
* **Receipts.** One JSONL receipt (``--receipt``), appended and fsynced as each URI moves
  through ``selected`` → ``converted`` → ``enqueued`` (or ``enqueue_failed``) →
  ``persisted`` (or ``missing`` / ``mismatch``), with a final ``summary`` line. A URI the
  write path never converted (e.g. its read failed) is recorded ``not_converted``. The
  exit status is 0 only when every selected URI is ``persisted`` (dry run: 0 on success).
  The rebuild is idempotent, so a failed URI is recovered by re-running for it.

Run inside the pod::

    kubectl cp ov-event-abstract-backfill.py <ns>/<pod>:/tmp/backfill.py -c <container>
    kubectl exec -it <pod> -n <ns> -c <container> -- /app/.venv/bin/python3 \\
        /tmp/backfill.py viking://user/<user>/peers/<peer>/memories/events \\
        --account default --user <user> --dry-run
    # then without --dry-run; afterwards remove /tmp/backfill.py

The pod must have ``ov_event_abstract_patch`` installed for a backfill to store the
Summary (a plain backfill uses whatever ``from_context`` is installed).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import inspect
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Memory-record leaf suffixes that are directory-level records, not event files
# (mirrors the skip list in MemoryUpdater._vectorize_memories).
_SKIP_SUFFIXES = ("/.overview.md", "/.abstract.md")

# viking://user/<user>[/peers/<peer>]/memories/events[/<rest>]
EVENTS_URI = re.compile(
    r"^viking://user/(?P<user>[^/]+)(?:/peers/(?P<peer>[^/]+))?/memories/events"
    r"(?:/(?P<rest>.+))?$"
)
# What may follow the events root: a date directory, or one event file under a day.
_ROOT_REST = re.compile(r"^\d{4}(?:/\d{2}(?:/\d{2})?)?$|^\d{4}/\d{2}/\d{2}/[^/]+\.md$")
_FILE_REST = re.compile(r"^\d{4}/\d{2}/\d{2}/[^/]+\.md$")


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Rebuild event memory vector records via the real write path "
            "(MemoryUpdater._vectorize_memories), not /api/v1/content/reindex."
        ),
    )
    parser.add_argument(
        "root_uri",
        help="viking://user/<u>[/peers/<p>]/memories/events[/YYYY[/MM[/DD[/<file>.md]]]]",
    )
    parser.add_argument(
        "--account", default="default", help="Account id (default: %(default)s)"
    )
    parser.add_argument("--user", required=True, help="User id that owns root_uri")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read-only report over REST; never constructs a writable service",
    )
    parser.add_argument(
        "--restore",
        action="store_true",
        help="Rebuild with the stock full-body abstract, independent of the patch switch",
    )
    parser.add_argument(
        "--limit",
        type=_positive_int,
        default=None,
        help="Stop after this many matched files (default: no limit)",
    )
    parser.add_argument(
        "--receipt",
        type=Path,
        default=None,
        help="Receipt JSONL path (default: ./ov-event-abstract-backfill-<mode>-<UTC>.jsonl)",
    )
    parser.add_argument(
        "--workspace",
        default=None,
        help=(
            "Scratch dir for this run's PID lock/queue state (default: a fresh temp dir, "
            "removed only after every record is verified). NEVER the pod's own "
            "storage.workspace."
        ),
    )
    parser.add_argument(
        "--wait-timeout",
        type=_positive_int,
        default=600,
        help="Seconds to wait for the embedding queue to drain (default: %(default)s)",
    )
    parser.add_argument(
        "--server",
        default="http://127.0.0.1:1933",
        help="Dry run only: server base URL (default: %(default)s)",
    )
    args = parser.parse_args(argv)
    if args.dry_run and args.restore:
        parser.error("--dry-run and --restore are mutually exclusive")
    try:
        validate_root(args.root_uri, args.user)
    except ValueError as exc:
        parser.error(str(exc))
    return args


def validate_root(root_uri: str, user: str) -> re.Match:
    """The root must be ``user``'s canonical events namespace (or a date/file under it)."""
    m = EVENTS_URI.match(root_uri.rstrip("/"))
    if not m:
        raise ValueError(
            f"root_uri must be viking://user/<user>[/peers/<peer>]/memories/events[...], "
            f"got {root_uri!r}"
        )
    if m.group("user") != user:
        raise ValueError(
            f"root_uri belongs to user {m.group('user')!r}, not --user {user!r}"
        )
    rest = m.group("rest")
    if rest is not None and not _ROOT_REST.match(rest):
        raise ValueError(
            f"root_uri must stop at a YYYY[/MM[/DD]] directory or a day's .md file, got {rest!r}"
        )
    return m


def validate_selection(root_uri: str, user: str, uris: list[str]) -> None:
    """Every selected URI must be an event file under ``root_uri`` for the same user and peer."""
    root = validate_root(root_uri, user)
    base = root_uri.rstrip("/")
    bad = []
    for uri in uris:
        m = EVENTS_URI.match(uri)
        ok = (
            m is not None
            and (uri == base or uri.startswith(base + "/"))
            and m.group("user") == root.group("user")
            and m.group("peer") == root.group("peer")
            and m.group("rest") is not None
            and _FILE_REST.match(m.group("rest"))
            and not uri.endswith(_SKIP_SUFFIXES)
        )
        if not ok:
            bad.append(uri)
    if bad:
        raise SystemExit(
            f"refusing: {len(bad)} selected URI(s) are not event files under {root_uri}: "
            + ", ".join(bad[:5])
        )


def _is_target_file(entry: dict[str, Any]) -> bool:
    if entry.get("isDir"):
        return False
    uri = str(entry.get("uri") or "")
    if not uri.endswith(".md"):
        return False
    return not uri.endswith(_SKIP_SUFFIXES)


async def _walk_event_files(viking_fs: Any, root_uri: str, ctx: Any) -> list[str]:
    """Every non-directory-record ``.md`` file at or under ``root_uri``, sorted."""
    if not await viking_fs.exists(root_uri, ctx=ctx):
        raise SystemExit(f"root_uri does not exist: {root_uri}")
    stat = await viking_fs.stat(root_uri, ctx=ctx, skip_count=True)
    if not stat.get("isDir", stat.get("is_dir")):
        return [root_uri] if root_uri.endswith(".md") else []
    entries = await viking_fs.tree(
        uri=root_uri,
        output="original",
        show_all_hidden=True,
        node_limit=None,
        level_limit=None,
        ctx=ctx,
    )
    return sorted(entry["uri"] for entry in entries if _is_target_file(entry))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _embedding_text_sha256(message: Any) -> str:
    text = (
        message
        if isinstance(message, str)
        else json.dumps(message, sort_keys=True, default=str)
    )
    return _sha256(text)


class ReceiptLog(list):
    """A list of receipt records that also appends each one to a JSONL file, fsynced."""

    def __init__(self, path: Optional[Path] = None):
        super().__init__()
        self._fh = open(path, "a", encoding="utf-8") if path else None

    def append(self, record: dict) -> None:  # type: ignore[override]
        record = {"ts": datetime.now(timezone.utc).isoformat(), **record}
        super().append(record)
        if self._fh:
            self._fh.write(json.dumps(record, sort_keys=True) + "\n")
            self._fh.flush()
            os.fsync(self._fh.fileno())

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None


def _resolve_base_from_context(installed_from_context: Any, *, restore: bool) -> Any:
    """The ``EmbeddingMsgConverter.from_context`` implementation a run should call.

    Plain backfill uses whatever is currently installed (patched or not: if the patch is
    not active, backfill cannot apply it either). ``--restore`` resolves to the stock
    implementation via ``inspect.unwrap``, which follows ``__wrapped__`` as many levels as
    needed. ``apply()`` installs ``staticmethod(summary_abstract(original))`` and, since
    Python 3.10, ``staticmethod`` forwards ``__wrapped__`` to its callable, so a single
    ``getattr(..., "__wrapped__")`` lands back on the patched closure. When the patch was
    never installed both modes resolve to the same callable, which is correct.
    """
    if not restore:
        return installed_from_context
    return inspect.unwrap(installed_from_context)


def _install_receipt_capture(converter: Any, base_from_context: Any, receipts: list):
    """Wrap ``base_from_context`` to record a ``converted`` receipt per record.

    Reads straight off the ``Context``/``EmbeddingMsg`` objects that flow through the real
    conversion, so the receipt reports exactly what was produced rather than a second,
    possibly drifted computation of it.
    """

    def capturing_from_context(*args, **kwargs):
        context = args[0] if args else kwargs.get("context")
        pre_abstract = getattr(context, "abstract", "") or ""
        msg = base_from_context(*args, **kwargs)
        if msg is not None:
            context_data = msg.context_data or {}
            post_abstract = context_data.get("abstract") or ""
            receipts.append(
                {
                    "stage": "converted",
                    "uri": context_data.get("uri", ""),
                    "old_abstract_len": len(pre_abstract.encode("utf-8")),
                    "new_abstract_len": len(post_abstract.encode("utf-8")),
                    "new_abstract_sha256": _sha256(post_abstract),
                    "embedding_sha256": _embedding_text_sha256(msg.message),
                }
            )
        return msg

    converter.from_context = staticmethod(capturing_from_context)


class _EnqueueRecorder:
    """Delegates to the vector DB manager, recording each enqueue's outcome by URI."""

    def __init__(self, inner: Any, receipts: list):
        self._inner = inner
        self._receipts = receipts

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def enqueue_embedding_msg(self, embedding_msg: Any) -> bool:
        uri = (getattr(embedding_msg, "context_data", None) or {}).get("uri", "")
        try:
            ok = await self._inner.enqueue_embedding_msg(embedding_msg)
        except Exception as exc:
            self._receipts.append(
                {"stage": "enqueue_failed", "uri": uri, "error": repr(exc)}
            )
            raise
        self._receipts.append(
            {"stage": "enqueued" if ok else "enqueue_failed", "uri": uri}
        )
        return ok


def refuse_context_templates(schema: Any) -> None:
    """The write path renders ``embedding_template`` with an ``extract_context`` this tool
    cannot rebuild; refuse a template that uses it rather than embed different text."""
    template = getattr(schema, "embedding_template", None) or ""
    if "extract_context" in template:
        raise SystemExit(
            "refusing: the events embedding_template references extract_context, "
            "which a backfill cannot reproduce byte-for-byte"
        )


def _latest(receipts: list, stage_prefixes: tuple[str, ...]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in receipts:
        if r.get("stage", "").startswith(stage_prefixes):
            out[r.get("uri", "")] = r
    return out


async def _verify_persisted(
    vikingdb: Any,
    ctx: Any,
    uris: list[str],
    receipts: list,
    record_id: Any = None,
    timeout: float = 0,
    interval: float = 2.0,
    sleep: Any = asyncio.sleep,
    clock: Any = time.monotonic,
) -> None:
    """Poll each record by id until its abstract matches what was converted.

    The embedding queue lives on shared AGFS storage (``mount_point=/queue``), so the
    live server's workers can take a message this process enqueued: this process's queue
    counters read "complete" while the embedding is still being computed elsewhere.
    Read-back is the only reliable signal. A record that is absent or still shows the old
    abstract is retried until ``timeout``; whatever is left then is recorded as
    ``missing`` / ``mismatch``.
    """
    if record_id is None:
        from openviking.storage.vector_ids import vector_record_id as record_id

    converted = _latest(receipts, ("converted",))
    for uri in uris:
        if uri not in converted:
            receipts.append({"stage": "not_converted", "uri": uri})
    waiting = [uri for uri in uris if uri in converted]
    deadline = clock() + timeout
    last: dict[str, tuple[str, int]] = {}
    while waiting:
        still = []
        for uri in waiting:
            rows = await vikingdb.get([record_id(ctx.account_id, uri, 2)], ctx=ctx)
            row = rows[0] if rows else None
            if row is None:
                last[uri] = ("missing", 0)
                still.append(uri)
                continue
            stored = row.get("abstract") or ""
            if _sha256(stored) == converted[uri]["new_abstract_sha256"]:
                receipts.append(
                    {
                        "stage": "persisted",
                        "uri": uri,
                        "stored_abstract_len": len(stored.encode("utf-8")),
                    }
                )
            else:
                last[uri] = ("mismatch", len(stored.encode("utf-8")))
                still.append(uri)
        waiting = still
        if waiting and clock() < deadline:
            await sleep(interval)
            continue
        break
    for uri in waiting:
        stage, length = last[uri]
        receipts.append({"stage": stage, "uri": uri, "stored_abstract_len": length})


async def _rebuild(
    service: Any,
    ctx: Any,
    uris: list[str],
    *,
    restore: bool,
    receipts: list,
    wait_timeout: int,
) -> bool:
    """Rebuild, drain, verify. True when the queue drained within the timeout."""
    from openviking.session.memory.memory_type_registry import create_default_registry
    from openviking.session.memory.memory_updater import (
        MemoryUpdater,
        MemoryUpdateResult,
    )
    from openviking.storage.queuefs.embedding_msg_converter import EmbeddingMsgConverter
    from openviking.storage.queuefs.queue_manager import get_queue_manager

    registry = create_default_registry()
    refuse_context_templates(registry.get("events"))

    converter = EmbeddingMsgConverter
    installed_from_context = converter.__dict__.get("from_context")
    try:
        base = _resolve_base_from_context(installed_from_context, restore=restore)
        _install_receipt_capture(converter, base, receipts)
        updater = MemoryUpdater(
            registry=registry,
            vikingdb=_EnqueueRecorder(service.vikingdb_manager, receipts),
        )
        updater._viking_fs = service.viking_fs
        result = MemoryUpdateResult()
        for uri in uris:
            result.add_edited(uri)
        await updater._vectorize_memories(
            result,
            ctx=ctx,
            uri_memory_type_map={uri: "events" for uri in uris},
        )
    finally:
        converter.from_context = installed_from_context

    started = time.monotonic()
    drained = True
    try:
        await get_queue_manager().wait_complete(timeout=wait_timeout)
    except TimeoutError:
        drained = False
    remaining = max(0.0, wait_timeout - (time.monotonic() - started))
    await _verify_persisted(
        service.vikingdb_manager, ctx, uris, receipts, timeout=remaining
    )
    return drained


class _RestReader:
    """Read-only REST access for the dry run (list + raw read, nothing else)."""

    def __init__(self, server: str, api_key: str, account: str, user: str):
        self._base = server.rstrip("/")
        self._headers = {
            "X-API-Key": api_key,
            "X-OpenViking-Account": account,
            "X-OpenViking-User": user,
        }

    def _get(self, path: str, **params: Any) -> Any:
        url = f"{self._base}{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers=self._headers, method="GET")
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read())["result"]

    def list_files(self, root_uri: str) -> list[str]:
        if root_uri.endswith(".md"):
            return [root_uri]
        entries = self._get(
            "/api/v1/fs/ls",
            uri=root_uri,
            recursive="true",
            output="original",
            node_limit=100000,
        )
        return sorted(e["uri"] for e in entries or [] if _is_target_file(e))

    def read(self, uri: str) -> str:
        text = self._get("/api/v1/content/read", uri=uri, raw="true", limit=-1)
        return text if isinstance(text, str) else json.dumps(text)


def _dry_run_report(reader: Any, uris: list[str], receipts: list) -> None:
    """Sizes a backfill would produce, from the same pure helpers the write path uses."""
    from openviking.session.memory.memory_updater import MemoryFileUtils, MemoryUpdater
    from openviking.session.memory.utils.link_renderer import LinkRenderer

    import ov_event_abstract_patch as patch_mod

    for uri in uris:
        try:
            mf = MemoryFileUtils.read(reader.read(uri) or "", uri=uri)
        except Exception as exc:  # noqa: BLE001 — reported per URI, the run continues
            receipts.append({"stage": "read_failed", "uri": uri, "error": repr(exc)})
            continue
        abstract = MemoryUpdater._truncate_memory_abstract(
            LinkRenderer.strip_all_links(mf.content or "")
        )
        summary = patch_mod.summary_section(abstract)
        receipts.append(
            {
                "stage": "dry_run",
                "uri": uri,
                "old_abstract_len": len(abstract.encode("utf-8")),
                "new_abstract_len": len((summary or abstract).encode("utf-8")),
                "would_change": bool(summary) and summary != abstract,
            }
        )


def _summarize(mode: str, uris: list[str], receipts: list, drained: bool) -> dict:
    if mode == "dry-run":
        rows = _latest(receipts, ("dry_run", "read_failed"))
        return {
            "stage": "summary",
            "mode": mode,
            "selected": len(uris),
            "would_change": sum(1 for r in rows.values() if r.get("would_change")),
            "read_failed": sum(1 for r in rows.values() if r["stage"] == "read_failed"),
            "complete": all(r["stage"] == "dry_run" for r in rows.values())
            and len(rows) == len(uris),
        }
    final = _latest(receipts, ("persisted", "missing", "mismatch", "not_converted"))
    counts: dict[str, int] = {}
    for r in final.values():
        counts[r["stage"]] = counts.get(r["stage"], 0) + 1
    return {
        "stage": "summary",
        "mode": mode,
        "selected": len(uris),
        "queue_drained": drained,
        **counts,
        "complete": drained and counts.get("persisted", 0) == len(uris),
    }


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    mode = "dry-run" if args.dry_run else ("restore" if args.restore else "backfill")
    receipt_path = args.receipt or Path(
        f"ov-event-abstract-backfill-{mode}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.jsonl"
    )
    receipts = ReceiptLog(receipt_path)
    receipts.append(
        {
            "stage": "start",
            "mode": mode,
            "root_uri": args.root_uri,
            "account": args.account,
            "user": args.user,
        }
    )

    if args.dry_run:
        api_key = os.environ.get("OPENVIKING_API_KEY") or os.environ.get("API_KEY")
        if not api_key:
            print("dry run needs OPENVIKING_API_KEY (or API_KEY)", file=sys.stderr)
            return 2
        reader = _RestReader(args.server, api_key, args.account, args.user)
        uris = reader.list_files(args.root_uri)[: args.limit]
        validate_selection(args.root_uri, args.user, uris)
        for uri in uris:
            receipts.append({"stage": "selected", "uri": uri})
        _dry_run_report(reader, uris, receipts)
        summary = _summarize(mode, uris, receipts, drained=True)
        receipts.append(summary)
        receipts.close()
        print(
            f"dry-run: {summary['selected']} file(s), {summary['would_change']} would change, "
            f"{summary['read_failed']} unreadable. Receipt: {receipt_path}"
        )
        return 0 if summary["complete"] else 1

    async def _run() -> tuple[list[str], bool, Optional[str]]:
        from openviking.server.identity import RequestContext, Role
        from openviking.service.core import OpenVikingService
        from openviking_cli.session.user_id import UserIdentifier

        ctx = RequestContext(
            user=UserIdentifier(args.account, args.user),
            role=Role.ROOT,
            bypass_acl=True,  # fenced by validate_root / validate_selection
        )
        workspace = args.workspace
        owns_workspace = workspace is None
        if owns_workspace:
            import tempfile

            workspace = tempfile.mkdtemp(prefix="ov-event-abstract-backfill-")
        service = OpenVikingService(path=workspace)
        await service.initialize()
        uris: list[str] = []
        drained = False
        try:
            uris = (await _walk_event_files(service.viking_fs, args.root_uri, ctx))[
                : args.limit
            ]
            validate_selection(args.root_uri, args.user, uris)
            for uri in uris:
                receipts.append({"stage": "selected", "uri": uri})
            if uris:
                drained = await _rebuild(
                    service,
                    ctx,
                    uris,
                    restore=args.restore,
                    receipts=receipts,
                    wait_timeout=args.wait_timeout,
                )
            else:
                drained = True
        finally:
            await service.close()
        kept = None
        if owns_workspace:
            if drained:
                import shutil

                shutil.rmtree(workspace, ignore_errors=True)
            else:
                kept = workspace
        return uris, drained, kept

    uris, drained, kept = asyncio.run(_run())
    summary = _summarize(mode, uris, receipts, drained)
    receipts.append({**summary, "workspace_kept": kept})
    receipts.close()
    detail = ", ".join(
        f"{k} {summary[k]}"
        for k in ("persisted", "missing", "mismatch", "not_converted")
        if summary.get(k)
    )
    print(
        f"{mode}: {len(uris)} file(s) under {args.root_uri}; {detail or 'nothing verified'}; "
        f"queue drained: {drained}. Receipt: {receipt_path}"
        + (f"; workspace kept at {kept}" if kept else "")
    )
    return 0 if summary["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
