"""An add_only memory write never replaces an existing memory (BUG-1180).

OpenViking v0.4.20 files an ``add_only`` memory (``events``, ``trajectories``) at a path
rendered from its fields: ``events`` is ``{year}/{month}/{day}/{event_name}.md``. When a
later extraction produces the same event name on the same day, the resolver hands the
operation to ``MemoryUpdater.apply_operations`` with that path. The streaming updater
passes ``add_only`` operations through with no merge decision
(``streaming_memory_updater.py`` "reason=add_only"), and ``_apply_upsert`` reads the
existing file and merges into it field by field. ``event_name`` and ``ranges`` are
``immutable``, so the old session's message ranges survive while the ChatLog is rendered
against the new session's messages, and ``summary`` is patched. The existing memory is
overwritten with a body that mixes two sessions.

Seen in the pilot on 2026-09-24: a recall session's first message was a pasted terminal
screen of OV ``read``/``search`` output; its extraction re-derived ``kinde_login_attempts``
and replaced the clean memory another session had written an hour earlier.

This patch wraps ``MemoryUpdater.apply_operations``. Before the stock body runs, each
``add_only`` upsert whose target path already exists on disk, or was already claimed by an
earlier operation in the same batch, is handled one of two ways:

  * **duplicate** — the existing memory's ``summary`` equals the incoming one: the
    operation is dropped. An echo or a retried commit adds nothing new.
  * **collision** — anything else: the operation is diverted to the first free sibling
    path (``<name>_2.md``, ``<name>_3.md``, …). Its ``old_memory_file_content`` is
    cleared so it is reported and diffed as a new write, and links and search tags that
    named the old path follow it.

``upsert`` memory types are untouched: merging into an existing file is their contract.

Guarded: applies only to openviking ``v0.4.20`` whose ``apply_operations`` and
``_apply_upsert`` sources hash to the expected values; otherwise it logs "NOT applied" and
stock behaviour stays. A failure inside the guard logs and falls through to the stock
body. Disable at runtime with ``OV_MEMORY_GUARD=0``.
"""

from __future__ import annotations

import functools
import hashlib
import inspect
import logging
import os

EXPECTED_VERSION = "v0.4.20"
EXPECTED_APPLY_SHA256 = (
    "3d193734e7cc836a7153915a170bca3076c64f30bd0db5a93bad9e1747004e0d"
)
EXPECTED_UPSERT_SHA256 = (
    "852ef6c47789e240ddd4072dae0c889f5fcfa9808c99667c54d11213fa796d09"
)
MAX_SIBLINGS = 50
ADD_ONLY = "add_only"

logger = logging.getLogger("ov_memory_guard_patch")

# module name -> apply function, consumed by sitecustomize.py
TARGETS = {"openviking.session.memory.memory_updater": "apply"}


def _source_hash(fn) -> str | None:
    try:
        return hashlib.sha256(inspect.getsource(fn).encode()).hexdigest()
    except (OSError, TypeError):
        return None


def _enabled() -> bool:
    if os.environ.get("OV_MEMORY_GUARD", "1") == "0":
        logger.warning("ov-memory-guard: disabled by OV_MEMORY_GUARD=0")
        return False
    return True


def sibling_uri(uri: str, n: int) -> str:
    """``…/kinde_login_attempts.md`` -> ``…/kinde_login_attempts_2.md``."""
    stem, dot, ext = uri.rpartition(".")
    if not dot or "/" in ext:
        return f"{uri}_{n}"
    return f"{stem}_{n}.{ext}"


def _summary(memory_file) -> str:
    fields = getattr(memory_file, "extra_fields", None) or {}
    value = fields.get("summary")
    return value.strip() if isinstance(value, str) else ""


async def _existing(module, viking_fs, uri: str, ctx):
    """The parsed memory at ``uri``, or None when nothing is there."""
    try:
        content = await viking_fs.read_file(uri, ctx=ctx)
    except Exception:  # noqa: BLE001 — a missing file raises; that means "free"
        return None
    if not content:
        return None
    try:
        return module.MemoryFileUtils.read(content, uri=uri)
    except Exception:  # noqa: BLE001 — unparseable still means "occupied"
        return object()


async def guard_add_only(module, updater, operations, ctx, search_tags_by_uri=None):
    """Drop duplicate and divert colliding add_only upserts, in place.

    Returns ``(dropped, diverted)`` for logging and tests: lists of ``(uri, new_uri)``.
    """
    registry = getattr(updater, "_registry", None)
    viking_fs = updater._get_viking_fs()
    if registry is None or viking_fs is None or operations.has_errors():
        return [], []

    claimed: set[str] = set()
    claimed_summary: dict[str, str] = {}
    remap: dict[str, str] = {}
    dropped: list[tuple[str, None]] = []
    diverted: list[tuple[str, str]] = []
    kept_ops = []
    for op in list(operations.upsert_operations or []):
        schema = registry.get(op.memory_type)
        if getattr(schema, "operation_mode", None) != ADD_ONLY or not op.uris:
            claimed.update(op.uris or [])
            kept_ops.append(op)
            continue
        incoming = op.memory_fields.get("summary") if op.memory_fields else None
        incoming = incoming.strip() if isinstance(incoming, str) else ""
        new_uris: list[str] = []
        for uri in op.uris:
            if uri in claimed:
                prior = claimed_summary.get(uri, "")
            else:
                existing = await _existing(module, viking_fs, uri, ctx)
                if existing is None:
                    claimed.add(uri)
                    claimed_summary[uri] = incoming
                    new_uris.append(uri)
                    continue
                prior = _summary(existing)
            if incoming and prior == incoming:
                dropped.append((uri, None))
                continue
            for n in range(2, MAX_SIBLINGS + 1):
                candidate = sibling_uri(uri, n)
                if candidate in claimed:
                    continue
                if await _existing(module, viking_fs, candidate, ctx) is None:
                    break
            else:
                logger.warning(
                    "ov-memory-guard: no free sibling for %s; add_only write skipped",
                    uri,
                )
                dropped.append((uri, None))
                continue
            claimed.add(candidate)
            claimed_summary[candidate] = incoming
            remap[uri] = candidate
            diverted.append((uri, candidate))
            new_uris.append(candidate)
        if not new_uris:
            continue  # every target was a duplicate: nothing to write
        if new_uris != list(op.uris):
            op.uris = new_uris
            op.old_memory_file_content = None
        kept_ops.append(op)

    operations.upsert_operations = kept_ops
    for link in getattr(operations, "resolved_links", None) or []:
        if link.from_uri in remap:
            link.from_uri = remap[link.from_uri]
        if link.to_uri in remap:
            link.to_uri = remap[link.to_uri]
    if isinstance(search_tags_by_uri, dict):
        for old, new in remap.items():
            if old in search_tags_by_uri and new not in search_tags_by_uri:
                search_tags_by_uri[new] = search_tags_by_uri[old]
    for uri, _ in dropped:
        logger.warning("ov-memory-guard: duplicate add_only write to %s dropped", uri)
    for uri, new in diverted:
        logger.warning(
            "ov-memory-guard: add_only write to existing %s diverted to %s", uri, new
        )
    return dropped, diverted


def wrap_apply_operations(module, orig):
    @functools.wraps(orig)
    async def apply_operations(
        self,
        operations,
        ctx,
        extract_context=None,
        isolation_handler=None,
        search_tags_by_uri=None,
    ):
        try:
            await guard_add_only(module, self, operations, ctx, search_tags_by_uri)
        except Exception:
            logger.exception("ov-memory-guard: guard failed; stock write path used")
        return await orig(
            self,
            operations,
            ctx,
            extract_context,
            isolation_handler,
            search_tags_by_uri,
        )

    apply_operations._ov_memory_guard = True
    return apply_operations


def apply(module) -> bool:
    """Patch ``module.MemoryUpdater.apply_operations`` in place; True if applied."""
    if not _enabled():
        return False
    import openviking

    version = getattr(openviking, "__version__", None)
    cls = getattr(module, "MemoryUpdater", None)
    orig = getattr(cls, "apply_operations", None)
    if getattr(orig, "_ov_memory_guard", False):
        return True
    upsert = getattr(cls, "_apply_upsert", None)
    apply_digest = _source_hash(orig) if orig else None
    upsert_digest = _source_hash(upsert) if upsert else None
    if (
        version != EXPECTED_VERSION
        or apply_digest != EXPECTED_APPLY_SHA256
        or upsert_digest != EXPECTED_UPSERT_SHA256
        or not hasattr(module, "MemoryFileUtils")
    ):
        logger.warning(
            "ov-memory-guard: NOT applied to MemoryUpdater (version=%r apply_sha256=%s "
            "upsert_sha256=%s); stock add_only writes kept",
            version,
            apply_digest,
            upsert_digest,
        )
        return False
    cls.apply_operations = wrap_apply_operations(module, orig)
    logger.warning(
        "ov-memory-guard: applied to MemoryUpdater.apply_operations (pid %d)",
        os.getpid(),
    )
    return True
