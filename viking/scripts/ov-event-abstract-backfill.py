#!/usr/bin/env python3
"""IMPR-1200 event-abstract backfill / restore, run INSIDE an OpenViking pod.

Codex review finding 3: ``POST /api/v1/content/reindex`` is the wrong tool for moving
existing event records onto the ``ov_event_abstract_patch`` Summary-only abstract,
because reindex embeds the raw file body (MEMORY_FIELDS comment and links included)
instead of the write-path text (``MemoryUpdater._vectorize_memories``: links stripped,
capped at 50 KB, optional ``embedding_template`` rendering). This tool instead
constructs the server's own ``OpenVikingService`` (the same remote vectordb/agfs
backends the pod's ``ov.conf`` already points at — no extra wiring, no duplicated
backend config) and calls the real ``MemoryUpdater._vectorize_memories`` for a
synthetic ``MemoryUpdateResult`` of the target URIs, so the enqueued embedding text
is byte-identical to what a fresh write would produce. Run it with ``kubectl exec``:

    kubectl cp ov-event-abstract-backfill.py <ns>/<pod>:/tmp/backfill.py -c <container>
    kubectl exec -it <pod> -n <ns> -c <container> -- /app/.venv/bin/python3 \\
        /tmp/backfill.py viking://user/<user>/peers/<peer>/memories/events \\
        --account default --user <user> --dry-run
    # drop --dry-run to actually enqueue; then:
    kubectl exec ... -- rm /tmp/backfill.py   # this tool never deletes itself

This process is a SECOND OpenViking instance running alongside the pod's server, so
it never touches the server's own workspace directory (``storage.workspace`` in
``ov.conf``, e.g. ``/app/data``): that directory holds the live server's PID lock
(``openviking.utils.process_lock``), and a second ``OpenVikingService()`` pointed at
the same workspace path raises ``DataDirectoryLocked`` instead of racing it. Pass
``--workspace`` to pick a scratch directory explicitly, or leave it unset for a fresh
``tempfile.mkdtemp()`` per run (removed on exit) — either way this only changes where
*this run's* local PID lock/queue state lives, not which vectordb/agfs backend it
talks to (that is still read from the pod's ``ov.conf``, unchanged).

Only the vector-store record is rebuilt — the markdown source files are never
written. ``--dry-run`` walks the tree and reports per-file sizes with no enqueue.

``--restore`` rebuilds the same URIs with the stock, full-body abstract, independent
of whether ``OV_EVENT_ABSTRACT_PATCH`` is set in the pod's environment: it resolves
``EmbeddingMsgConverter.from_context`` down to the stock implementation via
``inspect.unwrap`` (following the ``__wrapped__`` chain ``functools.wraps`` attaches
inside ``ov_event_abstract_patch.summary_abstract``) and calls that directly for the
duration of the run, so a rollback does not depend on an operator remembering to flip
the env var (and works even when the patch failed to apply for some other reason). If
the patch was never installed in this process, there is nothing to unwrap and
``--restore``/a plain backfill run produce the same result — which is correct.

Every URI attempted gets one JSON receipt line: ``uri``, ``old_abstract_len`` (bytes;
the full-body abstract ``_vectorize_memories`` computes before any patch runs),
``new_abstract_len`` (bytes; what actually got stored — equal to ``old_abstract_len``
under ``--restore``), and ``embedding_sha256`` (sha256 of the exact embedding text
enqueued; a multimodal message is JSON-serialized first). Receipts are captured by
temporarily wrapping ``EmbeddingMsgConverter.from_context`` around whichever
implementation the run should use (patched or stock) — never by recomputing the
write-path logic a second time — so what the receipt reports is what was enqueued,
not an approximation of it.

Verified so far (ov-test, 2026-09-25): the receipt values above are correct against
the real v0.4.20 code — confirmed by running this tool as a second process in the
``openviking-test`` pod, applying ``ov_event_abstract_patch`` manually in that
process (ov-test's deployed nav-patch ConfigMap predates IMPR-1200), and checking the
captured ``old_abstract_len``/``new_abstract_len`` against hand-built Context objects;
that run also caught and fixed the ``inspect.unwrap`` bug documented above. NOT yet
verified: the enqueued message actually landing in ov-test's vectordb when run this
way — the local embedding queue drains (0 errors logged) but ``VikingDBManager.get()``
/``get_stats()`` never showed the record after 60s of polling. Whether that is
particular to a bare second process racing the live server on the same remote
collection, or something else, is unresolved; the next verification pass should
confirm a real persisted write (e.g. once IMPR-1200 is deployed and this tool can
run inside the same already-initialized server process, or by extending the wait /
inspecting the embedding handler's circuit breaker and error paths further).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import inspect
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Memory-record leaf suffixes that are directory-level records, not event files
# (mirrors the skip list in MemoryUpdater._vectorize_memories).
_SKIP_SUFFIXES = ("/.overview.md", "/.abstract.md")


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
        help="viking:// URI to walk, e.g. viking://user/<u>/peers/<p>/memories/events",
    )
    parser.add_argument(
        "--account", default="default", help="Account id (default: %(default)s)"
    )
    parser.add_argument("--user", required=True, help="User id to run the rebuild as")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report counts/sizes only; never enqueue a vector-record write",
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
        help="Receipt JSON path (default: ./ov-event-abstract-backfill-<UTC timestamp>.json)",
    )
    parser.add_argument(
        "--workspace",
        default=None,
        help=(
            "Local scratch dir for this run's PID lock/queue state (default: a fresh "
            "tempfile.mkdtemp(), removed on exit). NEVER the pod's own "
            "storage.workspace — the live server's PID lock lives there; see the "
            "module docstring."
        ),
    )
    args = parser.parse_args(argv)
    if args.dry_run and args.restore:
        parser.error("--dry-run and --restore are mutually exclusive")
    return args


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


def _embedding_text_sha256(message: Any) -> str:
    text = (
        message
        if isinstance(message, str)
        else json.dumps(message, sort_keys=True, default=str)
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _resolve_base_from_context(installed_from_context: Any, *, restore: bool) -> Any:
    """The ``EmbeddingMsgConverter.from_context`` implementation a run should call.

    Plain backfill uses whatever is currently installed (patched or not — a
    dry-run-style honesty: if the patch isn't active, backfill can't apply it
    either). ``--restore`` always resolves to the stock implementation via
    ``inspect.unwrap`` — which follows ``__wrapped__`` (set by ``functools.wraps``
    inside ``ov_event_abstract_patch.summary_abstract``) as many levels as needed —
    independent of ``OV_EVENT_ABSTRACT_PATCH``. ``apply()`` installs the patched
    closure as ``staticmethod(summary_abstract(original))``, so
    ``converter.__dict__["from_context"]`` is a ``staticmethod`` object, not the
    plain function: since Python 3.10, ``staticmethod`` forwards ``__wrapped__`` to
    its underlying callable, so a single ``getattr(..., "__wrapped__", ...)`` only
    unwraps that one level and lands back on the patched closure, not the true
    original — ``inspect.unwrap`` keeps going until no ``__wrapped__`` remains. When
    the patch was never installed, there is nothing to unwrap and both modes resolve
    to the same callable — which is correct.
    """
    if not restore:
        return installed_from_context
    return inspect.unwrap(installed_from_context)


def _install_receipt_capture(
    converter: Any, base_from_context: Any, receipts: list[dict]
):
    """Wrap ``base_from_context`` to record one receipt per converted record.

    Reads straight off the ``Context``/``EmbeddingMsg`` objects that flow through the
    real conversion, so the receipt reports exactly what got enqueued rather than a
    second, possibly-drifted computation of the same thing.
    """

    def capturing_from_context(*args, **kwargs):
        context = args[0] if args else kwargs.get("context")
        pre_abstract = (getattr(context, "abstract", "") or "").encode("utf-8")
        msg = base_from_context(*args, **kwargs)
        if msg is not None:
            context_data = msg.context_data or {}
            post_abstract = (context_data.get("abstract") or "").encode("utf-8")
            receipts.append(
                {
                    "uri": context_data.get("uri", ""),
                    "old_abstract_len": len(pre_abstract),
                    "new_abstract_len": len(post_abstract),
                    "embedding_sha256": _embedding_text_sha256(msg.message),
                }
            )
        return msg

    converter.from_context = staticmethod(capturing_from_context)


async def _rebuild(
    service: Any, ctx: Any, uris: list[str], *, restore: bool
) -> list[dict]:
    from openviking.session.memory.memory_type_registry import create_default_registry
    from openviking.session.memory.memory_updater import (
        MemoryUpdater,
        MemoryUpdateResult,
    )
    from openviking.storage.queuefs.embedding_msg_converter import EmbeddingMsgConverter

    receipts: list[dict] = []
    converter = EmbeddingMsgConverter
    installed_from_context = converter.__dict__.get("from_context")
    try:
        base = _resolve_base_from_context(installed_from_context, restore=restore)
        _install_receipt_capture(converter, base, receipts)

        updater = MemoryUpdater(
            registry=create_default_registry(), vikingdb=service.vikingdb_manager
        )
        updater._viking_fs = service.viking_fs
        result = MemoryUpdateResult()
        for uri in uris:
            result.add_edited(uri)
        uri_memory_type_map = {
            uri: MemoryUpdater.memory_type_from_uri(uri) for uri in uris
        }
        await updater._vectorize_memories(
            result,
            ctx=ctx,
            uri_memory_type_map=uri_memory_type_map,
        )
    finally:
        converter.from_context = installed_from_context
    return receipts


async def _dry_run_report(service: Any, ctx: Any, uris: list[str]) -> list[dict]:
    """Report sizes without touching the vector store: reads each file and reuses
    the exact static helpers the write path uses for link-stripping and truncation,
    plus the patch module's own Summary extraction (imported directly — this report
    is meaningless without ov_event_abstract_patch on PYTHONPATH)."""
    from openviking.session.memory.memory_updater import MemoryFileUtils, MemoryUpdater
    from openviking.session.memory.utils.link_renderer import LinkRenderer

    import ov_event_abstract_patch as patch_mod

    report = []
    for uri in uris:
        content = await service.viking_fs.read_file(uri, ctx=ctx) or ""
        mf = MemoryFileUtils.read(content, uri=uri)
        abstract = LinkRenderer.strip_all_links(mf.content or "")
        abstract = MemoryUpdater._truncate_memory_abstract(abstract)
        summary = patch_mod.summary_section(abstract)
        report.append(
            {
                "uri": uri,
                "old_abstract_len": len(abstract.encode("utf-8")),
                "new_abstract_len": len((summary or abstract).encode("utf-8")),
                "would_change": bool(summary) and summary != abstract,
            }
        )
    return report


def _write_receipt(path: Path, mode: str, root_uri: str, entries: list[dict]) -> None:
    payload = {
        "mode": mode,
        "root_uri": root_uri,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(entries),
        "entries": entries,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    mode = "dry-run" if args.dry_run else ("restore" if args.restore else "backfill")
    receipt_path = args.receipt or Path(
        f"ov-event-abstract-backfill-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    )

    async def _run() -> list[dict]:
        from openviking.server.identity import RequestContext, Role
        from openviking.service.core import OpenVikingService
        from openviking_cli.session.user_id import UserIdentifier

        ctx = RequestContext(
            user=UserIdentifier(args.account, args.user),
            role=Role.ROOT,
            bypass_acl=True,
        )
        workspace = args.workspace
        owns_workspace = workspace is None
        if owns_workspace:
            import tempfile

            workspace = tempfile.mkdtemp(prefix="ov-event-abstract-backfill-")
        service = OpenVikingService(path=workspace)
        await service.initialize()
        try:
            uris = await _walk_event_files(service.viking_fs, args.root_uri, ctx)
            if args.limit is not None:
                uris = uris[: args.limit]
            if not uris:
                print(f"No matching .md files under {args.root_uri}", file=sys.stderr)
                return []
            if args.dry_run:
                return await _dry_run_report(service, ctx, uris)
            return await _rebuild(service, ctx, uris, restore=args.restore)
        finally:
            await service.close()
            if owns_workspace:
                import shutil

                shutil.rmtree(workspace, ignore_errors=True)

    entries = asyncio.run(_run())
    _write_receipt(receipt_path, mode, args.root_uri, entries)
    changed = sum(
        1
        for e in entries
        if e.get("would_change", e.get("new_abstract_len") != e.get("old_abstract_len"))
    )
    print(
        f"{mode}: {len(entries)} file(s) processed under {args.root_uri}, "
        f"{changed} abstract(s) changed. Receipt: {receipt_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
