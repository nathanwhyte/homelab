"""Carry each recalled memory's last-write time into assembled context (IMPR-1215).

On v0.4.20 every retrieval hit already reads its vector record's ``updated_at``
(``RETRIEVAL_OUTPUT_FIELDS``) for the hotness blend, then drops it: ``MatchedContext``,
the context assembler's ``Candidate`` and ``AssembledEntry`` have no time field, so a
``<memory>`` tag in ``/api/v1/search/search`` (``mode: "context"``) says nothing about
when the memory was written. The Claude Code plugin worked around that with one
``fs/stat`` per URI, 280-660 ms each server-side and up to 2.8 s under write load.

Read path, no new I/O, all state request-owned:

  * ``gather.gather_candidates`` opens a per-request map (a ContextVar holding a dict,
    so retrieval sub-tasks write into the same object) and, when retrieval returns,
    copies each candidate's time onto the ``Candidate`` itself.
  * ``HierarchicalRetriever._convert_to_matched_contexts`` writes each hit's
    ``updated_at`` into that map, keyed by its raw and display URI. Outside an
    assembly (plain find) there is no map and nothing is recorded.
  * ``budget._make_entry`` copies the time onto the ``AssembledEntry`` and recounts its
    tokens at the length the plugin renders (local time with a UTC offset, five
    characters longer than ``…Z``), so the budget reserves room for the final form.
  * ``render.render_entry`` adds ``updated="YYYY-MM-DDTHH:MM:SSZ"`` after ``detail``;
    ``AssembledEntry.to_dict`` adds ``updated_at``.

Write path (the vector time must mean the content's last write):

  * v0.4.20's reindex builds a fresh ``Context`` whose ``created_at``/``updated_at``
    default to now, so the IMPR-1200 events backfill (2026-09-28 ~13:24Z) stamped
    every reindexed event with the backfill time. ``ReindexExecutor._upsert_context``
    now stats the file first and ``EmbeddingMsgConverter.from_context`` gives that
    context the file's ``modTime`` as both times. Reindexing unchanged content keeps
    its write time, and re-running the events reindex repairs the backfilled records.

Each hook is version/source guarded on its own. A read-path hook that does not apply
leaves the tag without ``updated`` (the plugin then falls back to its stat); a failed
stat on reindex leaves v0.4.20's default. ``OV_RECALL_TIME_PATCH=0`` rolls all back.
"""

import asyncio
import contextvars
import functools
import hashlib
import inspect
import logging
import os
from datetime import datetime, timezone

EXPECTED_VERSION = "v0.4.20"
EXPECTED_SHA256 = {
    "convert": "18a9911e3aa793b5e5d966b09604717d7d06c914d340bdd0d70db95dd184ee32",
    "gather": "66f8d0b6590491345cdaca6be94984ef9d4cb719891f869243eece98fac5aa0b",
    "make_entry": "565a34fbf4bb310aa21884bf2b50d0ebdb79d11bd16bf76db1670a653d0d7470",
    "render_entry": "4ff788f4804f55c841d95d1e331a2f4093f8b36641ea5dfdcd89db03d1d764f8",
    "to_dict": "60715e66d58ef5b110f5a256cfdfc6065f01bcd314a5bb13cae6960ac6a26089",
    "upsert_context": "ddfd3f03e673f1b396caef3d549d4efacc97a64fc78e9a30c21c8297a48feaad",
    "from_context": "fb15c3af326be29f826a870a8e6284e1663c122089769ae5df1c74c1e5bc6081",
}
# "…Z" becomes "…-05:00" in the plugin: reserve that length when counting tokens.
LOCALIZED_PLACEHOLDER = "0000-00-00T00:00:00+00:00"
REINDEX_STAT_TIMEOUT_S = 5.0
logger = logging.getLogger("ov_recall_time_patch")

_request_times = contextvars.ContextVar("ov_recall_time_request", default=None)
_reindex_time = contextvars.ContextVar("ov_recall_time_reindex", default=None)


def parse(value):
    """``updated_at``/``modTime`` as an aware UTC datetime, or None when unusable."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def normalize(value):
    """``updated_at`` as ``YYYY-MM-DDTHH:MM:SSZ`` in UTC, or None when unusable."""
    dt = parse(value)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


# --- read path ---------------------------------------------------------------


def recording_convert(original):
    @functools.wraps(original)
    async def convert(self, candidates, *args, **kwargs):
        results = await original(self, candidates, *args, **kwargs)
        times = _request_times.get()
        if times is None:
            return results
        for c in candidates or ():
            value = normalize(c.get("updated_at"))
            if not value:
                continue
            uri = c.get("uri", "")
            if uri:
                times[uri] = value
                times[self._append_level_suffix(uri, c.get("level", 2))] = value
        return results

    convert._ov_recall_time_patch = True
    return convert


def timed_gather(original):
    @functools.wraps(original)
    async def gather_candidates(*args, **kwargs):
        times = {}
        token = _request_times.set(times)
        try:
            candidates, stats = await original(*args, **kwargs)
        finally:
            _request_times.reset(token)
        for candidate in candidates:
            value = times.get(candidate.uri) or times.get(candidate.base_uri)
            if value:
                candidate.updated_at = value
        return candidates, stats

    gather_candidates._ov_recall_time_patch = True
    return gather_candidates


def timed_make_entry(original, fragment_tokens):
    @functools.wraps(original)
    def make_entry(candidate, tier, text):
        entry = original(candidate, tier, text)
        value = getattr(candidate, "updated_at", None)
        if value:
            entry.updated_at = LOCALIZED_PLACEHOLDER
            entry.tokens = fragment_tokens(entry)
            entry.updated_at = value
        return entry

    make_entry._ov_recall_time_patch = True
    return make_entry


def timed_render_entry(original):
    @functools.wraps(original)
    def render_entry(entry):
        text = original(entry)
        value = getattr(entry, "updated_at", None)
        if not value:
            return text
        # Every attribute before ``detail`` is escaped for ``"``, so the first
        # `` detail="<tier>"`` is the real one.
        marker = f' detail="{entry.detail}"'
        at = text.find(marker)
        if at < 0:
            return text
        at += len(marker)
        return f'{text[:at]} updated="{value}"{text[at:]}'

    render_entry._ov_recall_time_patch = True
    return render_entry


def timed_to_dict(original):
    @functools.wraps(original)
    def to_dict(self):
        data = original(self)
        value = getattr(self, "updated_at", None)
        if value:
            data["updated_at"] = value
        return data

    to_dict._ov_recall_time_patch = True
    return to_dict


# --- write path (reindex) ------------------------------------------------------


async def file_mod_time(uri, ctx):
    """The file's ``modTime`` as a UTC datetime, or None."""
    try:
        from openviking.storage.viking_fs import get_viking_fs

        stat = await asyncio.wait_for(
            get_viking_fs().stat(uri, ctx=ctx, skip_count=True), REINDEX_STAT_TIMEOUT_S
        )
    except Exception as exc:  # noqa: BLE001 — a failed stat keeps v0.4.20's default
        logger.warning("ov-recall-time: reindex stat failed for %s: %r", uri, exc)
        return None
    value = (
        stat.get("modTime")
        if isinstance(stat, dict)
        else getattr(stat, "modTime", None)
    )
    return parse(value)


async def existing_times(uri, ctx):
    """``(created_at, updated_at)`` of the record being replaced, or None."""
    try:
        from openviking.server.dependencies import get_service

        record = await asyncio.wait_for(
            get_service().vikingdb_manager.fetch_by_uri(uri, ctx=ctx),
            REINDEX_STAT_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001 — no record to preserve
        logger.warning(
            "ov-recall-time: reindex record lookup failed for %s: %r", uri, exc
        )
        return None
    if not isinstance(record, dict):
        return None
    updated = parse(record.get("updated_at"))
    if not updated:
        return None
    return parse(record.get("created_at")) or updated, updated


async def reindex_times(uri, ctx):
    """The times a rebuilt record keeps: the file's ``modTime`` (both), else the
    replaced record's own times, else None (v0.4.20's "now", logged)."""
    when = await file_mod_time(uri, ctx)
    if when:
        return when, when
    kept = await existing_times(uri, ctx)
    if kept:
        return kept
    logger.warning(
        "ov-recall-time: reindex of %s keeps no write time (defaults to now)", uri
    )
    return None


def mod_time_upsert(original):
    @functools.wraps(original)
    async def _upsert_context(self, *args, **kwargs):
        uri = kwargs.get("uri", "")
        times = await reindex_times(uri, kwargs.get("ctx")) if uri else None
        token = _reindex_time.set((uri, *times) if times else None)
        try:
            return await original(self, *args, **kwargs)
        finally:
            _reindex_time.reset(token)

    _upsert_context._ov_recall_time_patch = True
    return _upsert_context


def mod_time_from_context(original):
    @functools.wraps(original)
    def from_context(context, *args, **kwargs):
        pending = _reindex_time.get()
        if pending and getattr(context, "uri", None) == pending[0]:
            context.created_at = pending[1]
            context.updated_at = pending[2]
        return original(context, *args, **kwargs)

    from_context._ov_recall_time_patch = True
    return from_context


# --- installation --------------------------------------------------------------


def _guarded(name, func):
    """True when ``func`` is the v0.4.20 source this hook was written against."""
    if os.environ.get("OV_RECALL_TIME_PATCH", "1") == "0":
        logger.warning("ov-recall-time: %s disabled by OV_RECALL_TIME_PATCH=0", name)
        return False
    if func is None:
        logger.warning("ov-recall-time: %s NOT applied (target missing)", name)
        return False
    import openviking

    try:
        # getsource follows __wrapped__, so a hook stacked on another patch's
        # functools.wraps wrapper still hashes the v0.4.20 function.
        digest = hashlib.sha256(inspect.getsource(func).encode()).hexdigest()
    except (OSError, TypeError):
        digest = None
    version = getattr(openviking, "__version__", None)
    if version != EXPECTED_VERSION or digest != EXPECTED_SHA256[name]:
        logger.warning(
            "ov-recall-time: %s NOT applied (version=%r source_sha256=%s)",
            name,
            version,
            digest,
        )
        return False
    return True


def _install(name, owner, attr, wrap, *, static=False):
    original = (
        owner.__dict__.get(attr)
        if isinstance(owner, type)
        else getattr(owner, attr, None)
    )
    if isinstance(original, staticmethod):
        original = original.__func__
    if getattr(original, "_ov_recall_time_patch", False):
        return True
    if not _guarded(name, original):
        return False
    wrapped = wrap(original)
    setattr(owner, attr, staticmethod(wrapped) if static else wrapped)
    logger.warning("ov-recall-time: %s applied (pid %d)", name, os.getpid())
    return True


def apply_retriever(module):
    cls = getattr(module, "HierarchicalRetriever", None)
    if cls is None:
        return _guarded("convert", None)
    return _install("convert", cls, "_convert_to_matched_contexts", recording_convert)


def apply_gather(module):
    return _install("gather", module, "gather_candidates", timed_gather)


def apply_budget(module):
    return _install(
        "make_entry",
        module,
        "_make_entry",
        lambda original: timed_make_entry(original, module.fragment_tokens),
    )


def apply_render(module):
    return _install("render_entry", module, "render_entry", timed_render_entry)


def apply_models(module):
    cls = getattr(module, "AssembledEntry", None)
    if cls is None:
        return _guarded("to_dict", None)
    return _install("to_dict", cls, "to_dict", timed_to_dict)


def apply_reindex(module):
    cls = getattr(module, "ReindexExecutor", None)
    if cls is None:
        return _guarded("upsert_context", None)
    return _install("upsert_context", cls, "_upsert_context", mod_time_upsert)


def apply_converter(module):
    cls = getattr(module, "EmbeddingMsgConverter", None)
    if cls is None:
        return _guarded("from_context", None)
    return _install(
        "from_context", cls, "from_context", mod_time_from_context, static=True
    )
