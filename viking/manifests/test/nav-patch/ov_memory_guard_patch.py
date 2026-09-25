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

This patch guards every ``add_only`` upsert before it is written, at
``StreamingMemoryUpdater.submit`` (the whole request, before its append/merge split) and
again at ``MemoryUpdater.apply_operations`` (for direct callers). For each target it walks
``<name>.md``, ``<name>_2.md``, ``<name>_3.md``, …:

  * **duplicate** — a slot already holds (or an earlier op in the batch, or another
    in-flight submit, claimed) the same event, every structured field equal including
    ``source_extraction_id``: the operation is dropped and references to its target
    follow to that slot. Only a replay of the same extraction is a duplicate; another
    session's identical-looking event is kept apart.
  * **collision** — the first free slot takes the write. The op's
    ``old_memory_file_content`` is cleared so it is reported and diffed as a new write,
    and links, delete replacements and search tags that named the old path follow it.
  * **uncertain** — a slot that cannot be read (anything but not-found), or no free slot
    in 50: ``GuardError`` is raised and nothing is written. The stock body is never run
    unguarded, because that is the overwrite this patch exists to prevent.

``upsert`` memory types are untouched: merging into an existing file is their contract.

Guarded: applies only to openviking ``v0.4.20`` whose ``apply_operations``,
``_apply_upsert``, ``StreamingMemoryUpdater.submit`` and ``_split_append_only_request``
sources hash to the expected values; otherwise it logs "NOT applied" and stock behaviour
stays. Disable at runtime with ``OV_MEMORY_GUARD=0``.

**Echo guard** (``apply_echo_guard``, same module, ``OV_ECHO_GUARD=0``). The collision
came from an echo: recalled memory text pasted into a user turn was extracted again as new
facts. User-memory extraction already leaves tool calls and results out of its prompt
(``include_tool_parts_in_conversation = False``), so the agent's own ``read``/``search``
calls cannot echo; only text in a turn can. ``ExtractContext`` holds the one message list
that both the extraction prompt (``session_extract_context_provider.py``, ``[idx]`` lines)
and the ``events`` ChatLog (``read_message_ranges``) index into. The echo guard wraps
``ExtractContext.__init__`` and, in each **user** text part, replaces each block of
rendered OpenViking recall output with a one-line placeholder. A block opens at a line
that is unmistakably recall output:

  * a Claude Code render of an OpenViking MCP call (``⏺ plugin:openviking-memory:…``)
  * a search-result line (``- [memory 68%] viking://…``)
  * a memory body's ChatLog header (``# 2026-09-24 (Thursday) ChatLog:``)

and runs to the next pasted transcript event (a ``❯`` prompt or a non-OpenViking ``⏺``
line), which is kept. A final plain-prose paragraph inside the block, such as a
correction typed after the paste, is kept too. ``<openviking-context …>`` blocks are
removed. Every message keeps its place, so ``ranges`` still line up; assistant turns are
left alone.
"""

from __future__ import annotations

import contextvars
import dataclasses
import functools
import hashlib
import inspect
import logging
import os
import re

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


FREE = object()  # _existing: confirmed not found


class GuardError(RuntimeError):
    """The guard cannot prove an add_only write is safe; the write must not proceed."""


def _is_not_found(exc: BaseException) -> bool:
    if isinstance(exc, FileNotFoundError) or type(exc).__name__ == "NotFoundError":
        return True
    try:
        from openviking.storage.viking_fs import is_not_found_error

        return bool(is_not_found_error(exc))
    except Exception:  # noqa: BLE001 — helper unavailable: only the checks above count
        return False


def _event_fields(op) -> dict[str, str]:
    """The structured fields that identify one event occurrence (everything but content)."""
    fields = getattr(op, "memory_fields", None) or {}
    return {
        k: str(v).strip() for k, v in fields.items() if k != "content" and v is not None
    }


def _same_event(existing, fields: dict[str, str]) -> bool:
    """The stored memory carries every incoming field with the same value."""
    stored = getattr(existing, "extra_fields", None)
    if not isinstance(stored, dict) or not fields:
        return False
    return all(str(stored.get(k, "")).strip() == v for k, v in fields.items())


async def _existing(module, viking_fs, uri: str, ctx):
    """``FREE`` when ``uri`` is confirmed absent, else the parsed memory.

    Any failure other than not-found raises ``GuardError``: an unreadable target must
    never be treated as free, or the stock write would merge into it (Codex P0, 2026-09-24).
    """
    try:
        content = await viking_fs.read_file(uri, ctx=ctx)
    except Exception as exc:
        if _is_not_found(exc):
            return FREE
        raise GuardError(f"cannot read add_only target {uri}: {exc!r}") from exc
    if not content:
        return FREE
    try:
        return module.MemoryFileUtils.read(content, uri=uri)
    except Exception:  # noqa: BLE001 — unparseable still means "occupied"
        return object()


# Slots chosen by in-flight StreamingMemoryUpdater.submit calls in this process: slot uri
# -> Reservation. Selection runs under _reservation_lock(), so two concurrent submits
# never pick the same free slot, and the apply-time pass recognises its own submit's
# reservations (by the owner token in _OWNER) and verifies them instead of diverting
# again, which would strand links the split already sent to the merge request.
#
# A reservation is a pending write, not a stored one (Codex P1, round 3): a request whose
# event matches another submit's reservation waits for that submit to finish, then
# decides again from what is on disk, so it never reports success for a write that the
# owner may still fail. Release is synchronous (no await), so a cancelled submit cannot
# be interrupted mid-cleanup and leave a stale reservation (Codex P2, round 3).
#
# Only in-process: OpenViking here runs one server process; another writer that takes a
# reserved slot is caught by the verify step and raises GuardError.
MAX_WAITS = 20


@dataclasses.dataclass(eq=False)
class Reservation:
    fields: dict
    owner: object
    done: object  # asyncio.Event, set when the owning submit returns or fails


class _PendingDuplicate(Exception):
    """A matching event is reserved by another in-flight submit; wait, then retry."""

    def __init__(self, reservation):
        super().__init__(reservation)
        self.reservation = reservation


_RESERVED: dict[str, Reservation] = {}
_LOCKS: dict[int, object] = {}
_OWNER = contextvars.ContextVar("ov_memory_guard_owner", default=None)


def _reservation_lock():
    import asyncio

    loop = asyncio.get_running_loop()
    lock = _LOCKS.get(id(loop))
    if lock is None:
        lock = _LOCKS[id(loop)] = asyncio.Lock()
    return lock


def release_reservations(taken, table=None) -> None:
    """Drop this submit's reservations and wake anyone waiting on them. No awaits."""
    table = _RESERVED if table is None else table
    for slot, reservation in taken:
        if table.get(slot) is reservation:
            del table[slot]
        reservation.done.set()


async def guard_with_waits(run_guard):
    """Run ``run_guard`` under the reservation lock, waiting out pending duplicates."""
    for _ in range(MAX_WAITS):
        try:
            async with _reservation_lock():
                return await run_guard()
        except _PendingDuplicate as pending:
            await pending.reservation.done.wait()
    raise GuardError("add_only target stayed reserved by other in-flight submits")


async def guard_add_only(
    module,
    viking_fs,
    registry,
    operations,
    ctx,
    search_tags_by_uri=None,
    reserved=None,
    reserve=False,
    owner=None,
):
    """Drop duplicate and divert colliding add_only upserts, in place.

    For each add_only target, walk ``uri, uri_2, uri_3, …``. A slot that is stored
    with (or claimed earlier in this batch for) the same event, every structured field
    equal including ``source_extraction_id``, means the write already happened: the op
    is dropped and references to its target follow to that slot. The first free slot
    takes the write. Running out of slots, or failing to read one, raises ``GuardError``.

    ``reserved`` is the shared reservation table and ``owner`` the calling submit's
    token. A slot reserved by this owner for the same fields is its own pending write:
    verified in place (free, or holding this event) and never diverted again. A slot
    reserved by another owner is skipped, or, if it is for the same event, raises
    ``_PendingDuplicate`` so the caller waits for that submit's outcome. Nothing is
    mutated until the whole plan is made. With ``reserve=True`` (submit) the chosen
    slots are reserved for ``owner``.

    Returns ``(dropped, diverted, taken)``; ``taken`` holds ``(slot, Reservation)`` when
    reserving, else ``(slot, fields)``.
    """
    if registry is None or viking_fs is None or operations.has_errors():
        return [], [], []
    reserved = reserved if reserved is not None else {}

    claimed: dict[str, dict[str, str] | None] = {}
    remap: dict[str, str] = {}
    dropped: list[tuple[str, str]] = []
    diverted: list[tuple[str, str]] = []
    taken: list[tuple[str, dict[str, str]]] = []
    plan: list[tuple[object, list[str] | None]] = []  # (op, new uris or None to keep)
    for op in list(operations.upsert_operations or []):
        schema = registry.get(op.memory_type)
        if getattr(schema, "operation_mode", None) != ADD_ONLY or not op.uris:
            for uri in op.uris or []:
                claimed.setdefault(uri, None)
            plan.append((op, None))
            continue
        fields = _event_fields(op)
        new_uris: list[str] = []
        for uri in op.uris:
            for n in range(1, MAX_SIBLINGS + 1):
                slot = uri if n == 1 else sibling_uri(uri, n)
                if slot in claimed:
                    if claimed[slot] == fields:
                        dropped.append((uri, slot))
                        if slot != uri:
                            remap[uri] = slot
                        break
                    continue
                entry = reserved.get(slot)
                own = entry is not None and owner is not None and entry.owner is owner
                if entry is not None and not own:
                    if entry.fields == fields:
                        raise _PendingDuplicate(entry)
                    continue
                if own and entry.fields != fields:
                    continue  # another op of this request holds it
                state = await _existing(module, viking_fs, slot, ctx)
                if state is FREE:
                    claimed[slot] = fields
                    if not own:
                        taken.append((slot, fields))
                    new_uris.append(slot)
                    if slot != uri:
                        remap[uri] = slot
                        diverted.append((uri, slot))
                    break
                if _same_event(state, fields):
                    claimed[slot] = fields
                    dropped.append((uri, slot))
                    if slot != uri:
                        remap[uri] = slot
                    break
                if own:
                    raise GuardError(
                        f"reserved slot {slot} was taken by another writer"
                    )
            else:
                raise GuardError(f"no free sibling slot for add_only target {uri}")
        plan.append((op, new_uris))

    kept_ops = []
    for op, new_uris in plan:
        if new_uris is None:
            kept_ops.append(op)
            continue
        if not new_uris:
            continue  # every target already holds this event: nothing to write
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
    replacements = getattr(operations, "delete_replacements", None)
    if isinstance(replacements, dict):
        for key, value in list(replacements.items()):
            if value in remap:
                replacements[key] = remap[value]
    if isinstance(search_tags_by_uri, dict):
        for old, new in remap.items():
            if old in search_tags_by_uri and new not in search_tags_by_uri:
                search_tags_by_uri[new] = search_tags_by_uri[old]
    for uri, slot in dropped:
        logger.warning(
            "ov-memory-guard: add_only write to %s dropped: %s already holds this event",
            uri,
            slot,
        )
    for uri, new in diverted:
        logger.warning(
            "ov-memory-guard: add_only write to existing %s diverted to %s", uri, new
        )
    if reserve:
        import asyncio

        taken = [
            (slot, Reservation(slot_fields, owner, asyncio.Event()))
            for slot, slot_fields in taken
        ]
        for slot, reservation in taken:
            reserved[slot] = reservation
    return dropped, diverted, taken


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
        # No fallback to the stock body on failure: an unguarded add_only write is the
        # overwrite this patch exists to prevent. OV_MEMORY_GUARD=0 is the escape hatch.
        owner = _OWNER.get()
        await guard_with_waits(
            lambda: guard_add_only(
                module,
                self._get_viking_fs(),
                getattr(self, "_registry", None),
                operations,
                ctx,
                search_tags_by_uri,
                reserved=_RESERVED,
                owner=owner,
            )
        )
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


# --- Request-level guard: before the append/merge split -----------------------------
#
# StreamingMemoryUpdater.submit splits a request into an add_only (append) request and a
# merge request, and split_links_for_append_only_ops sends any link that also touches an
# upsert memory to the merge request, which is applied later from a deep copy. A diversion
# made inside apply_operations for the append request therefore never reaches that link
# (Codex P1, 2026-09-24). Guarding the whole request first means the split, and every link,
# delete replacement and op, already sees the final URIs. The request's provenance is
# attached first (stock submit does the same, idempotently), so ``source_extraction_id``
# is part of the event identity and only a replay of the same extraction is a duplicate.
# The apply_operations guard stays for callers that apply operations directly; for a
# submitted request it verifies the submit's reservations rather than choosing again.

EXPECTED_SUBMIT_SHA256 = (
    "67e4e598f003d0d46f6790ac19f469a367cdfa5d65203b12b8a10f596989c0ee"
)
EXPECTED_SPLIT_SHA256 = (
    "72cb16fd3f60e18ebf99da17c782c233fe231f3e1ff3073380b04baa497e0605"
)


def wrap_submit(module, orig, memory_module=None):
    @functools.wraps(orig)
    async def submit(self, request):
        operations = getattr(request, "operations", None)
        ctx = getattr(request, "ctx", None)
        if operations is None or ctx is None:
            return await orig(self, request)
        readers = memory_module
        if readers is None:
            from openviking.session.memory import memory_updater as readers
        module.attach_source_to_request_operations(request)
        registry = self.registry or module.create_default_registry()
        owner = object()
        token = _OWNER.set(owner)
        taken = []
        try:
            _, _, taken = await guard_with_waits(
                lambda: guard_add_only(
                    readers,
                    module.get_viking_fs(),
                    registry,
                    operations,
                    ctx,
                    reserved=_RESERVED,
                    reserve=True,
                    owner=owner,
                )
            )
            return await orig(self, request)
        finally:
            release_reservations(taken)
            _OWNER.reset(token)

    submit._ov_memory_guard = True
    return submit


def apply_streaming(module) -> bool:
    """Patch ``module.StreamingMemoryUpdater.submit`` in place; True if applied."""
    if not _enabled():
        return False
    import openviking

    version = getattr(openviking, "__version__", None)
    cls = getattr(module, "StreamingMemoryUpdater", None)
    orig = getattr(cls, "submit", None)
    if getattr(orig, "_ov_memory_guard", False):
        return True
    split = getattr(cls, "_split_append_only_request", None)
    submit_digest = _source_hash(orig) if orig else None
    split_digest = _source_hash(split) if split else None
    if (
        version != EXPECTED_VERSION
        or submit_digest != EXPECTED_SUBMIT_SHA256
        or split_digest != EXPECTED_SPLIT_SHA256
        or not hasattr(module, "get_viking_fs")
        or not hasattr(module, "create_default_registry")
        or not hasattr(module, "attach_source_to_request_operations")
    ):
        logger.warning(
            "ov-memory-guard: NOT applied to StreamingMemoryUpdater (version=%r "
            "submit_sha256=%s split_sha256=%s); request-level guard off",
            version,
            submit_digest,
            split_digest,
        )
        return False
    cls.submit = wrap_submit(module, orig)
    logger.warning(
        "ov-memory-guard: applied to StreamingMemoryUpdater.submit (pid %d)",
        os.getpid(),
    )
    return True


# --- Echo guard -------------------------------------------------------------------------

EXPECTED_EXTRACT_INIT_SHA256 = (
    "194316aaa85ed0cdacdb638dc6982db93fdb19d78dd840b775f2eaad4c96fcf3"
)
RECALL_LINE = re.compile(
    r"^(?:"
    r"\s*⏺ plugin:openviking-memory:openviking - "  # Claude Code render of an OV MCP call
    r"|\s*-\s*\[(?:memory|resource|skill) \d+%\] viking://"  # a search-result line
    r"|# \d{4}-\d{2}-\d{2} \([A-Z][a-z]+\) ChatLog:\s*$"  # a memory body's ChatLog header
    r")",
    re.MULTILINE,
)
CONTEXT_BLOCK = re.compile(
    r"<openviking-context\b[^>]*>.*?</openviking-context>", re.DOTALL
)
PASTE_PLACEHOLDER = "[pasted OpenViking recall output omitted]"
CONTEXT_PLACEHOLDER = "[recalled OpenViking context omitted]"
# The next event in a pasted Claude Code transcript ends a recall block: a user prompt
# (❯) or any tool call / assistant turn (⏺) other than another OpenViking call.
TRANSCRIPT_EVENT = re.compile(
    r"^\s*(?:❯ |⏺ (?!plugin:openviking-memory:openviking - ))"
)
# Lines shaped like rendered recall output rather than something a person typed.
RECALL_SHAPED = re.compile(r"^(?:\s|#|\*\*|- \[|[│├└┌┐┘⎿|>]|\.\.\.|…)")


def _recall_block_end(lines: list[str], start: int) -> tuple[int, int]:
    """``(end, keep_from)`` for the recall block opening at ``start``.

    The block runs to the next transcript event (or the end). Everything after its last
    recall-shaped line, from the first paragraph set off by a blank line, is handed back
    to the caller (``keep_from < end``): text typed after a paste, however many
    paragraphs, is the user's own and must reach extraction (Codex P1, 2026-09-24).

    Accepted trade-off: a pasted turn whose final paragraph is plain unindented prose
    lets that paragraph through. Free text has no explicit author boundary, and losing
    the user's words is the worse error; a leaked sentence can at most add a separate
    memory, because the collision guard never lets it overwrite one.
    """
    end = start + 1
    while end < len(lines) and not TRANSCRIPT_EVENT.match(lines[end]):
        end += 1
    last_shaped = max(
        i
        for i in range(start, end)
        if RECALL_LINE.match(lines[i]) or RECALL_SHAPED.match(lines[i])
    )
    seen_blank = False
    for i in range(last_shaped + 1, end):
        if not lines[i].strip():
            seen_blank = True
        elif seen_blank:
            return end, i
    return end, end


def strip_recall_text(text: str) -> str:
    """User text with rendered OpenViking recall output removed; unchanged otherwise.

    Each recall block (opened by a line ``RECALL_LINE`` matches) is replaced by one
    placeholder line. Everything outside the blocks is kept: text before them, later
    transcript events, and a trailing prose paragraph inside a block.
    """
    if not text:
        return text
    out = CONTEXT_BLOCK.sub(CONTEXT_PLACEHOLDER, text)
    lines = out.split("\n")
    if not any(RECALL_LINE.match(ln) for ln in lines):
        return out
    kept: list[str] = []
    i = 0
    while i < len(lines):
        if not RECALL_LINE.match(lines[i]):
            kept.append(lines[i])
            i += 1
            continue
        end, keep_from = _recall_block_end(lines, i)
        while kept and not kept[-1].strip():
            kept.pop()
        if kept:
            kept.append("")
        kept.append(PASTE_PLACEHOLDER)
        if keep_from < end:
            kept.append("")
            kept.extend(lines[keep_from:end])
        i = end
    return "\n".join(kept).rstrip("\n") if kept else PASTE_PLACEHOLDER


def strip_recall_message(message, text_part_cls):
    """A copy of a user message with recall output removed from its text parts."""
    if getattr(message, "role", None) != "user":
        return message
    parts = list(getattr(message, "parts", None) or [])
    changed = False
    new_parts = []
    for part in parts:
        if isinstance(part, text_part_cls) and part.text:
            stripped = strip_recall_text(part.text)
            if stripped != part.text:
                changed = True
                part = text_part_cls(stripped)
        new_parts.append(part)
    if not changed:
        return message
    return dataclasses.replace(message, parts=new_parts)


def wrap_extract_init(module, orig):
    @functools.wraps(orig)
    def __init__(self, messages, chunk_meta=None, *, split_long_text_messages=True):
        if chunk_meta is None and isinstance(messages, list):
            try:
                cleaned = [strip_recall_message(m, module.TextPart) for m in messages]
                stripped = sum(1 for a, b in zip(messages, cleaned) if a is not b)
                if stripped:
                    logger.warning(
                        "ov-echo-guard: removed recalled OpenViking output from %d "
                        "user message(s) before extraction",
                        stripped,
                    )
                messages = cleaned
            except Exception:
                logger.exception("ov-echo-guard: strip failed; messages left unchanged")
        orig(
            self,
            messages,
            chunk_meta,
            split_long_text_messages=split_long_text_messages,
        )

    __init__._ov_echo_guard = True
    return __init__


def apply_echo_guard(module) -> bool:
    """Patch ``module.ExtractContext.__init__`` in place; True if applied."""
    if os.environ.get("OV_ECHO_GUARD", "1") == "0":
        logger.warning("ov-echo-guard: disabled by OV_ECHO_GUARD=0")
        return False
    import openviking

    version = getattr(openviking, "__version__", None)
    cls = getattr(module, "ExtractContext", None)
    orig = cls.__dict__.get("__init__") if cls is not None else None
    if getattr(orig, "_ov_echo_guard", False):
        return True
    digest = _source_hash(orig) if orig else None
    if (
        version != EXPECTED_VERSION
        or digest != EXPECTED_EXTRACT_INIT_SHA256
        or not hasattr(module, "TextPart")
    ):
        logger.warning(
            "ov-echo-guard: NOT applied to ExtractContext (version=%r init_sha256=%s); "
            "extraction sees pasted recall output",
            version,
            digest,
        )
        return False
    cls.__init__ = wrap_extract_init(module, orig)
    logger.warning(
        "ov-echo-guard: applied to ExtractContext.__init__ (pid %d)", os.getpid()
    )
    return True
