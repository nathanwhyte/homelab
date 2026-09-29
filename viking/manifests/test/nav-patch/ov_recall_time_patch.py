"""Carry each recalled memory's last-write time into assembled context (IMPR-1215).

On v0.4.20 every retrieval hit already reads its vector record's ``updated_at``
(``RETRIEVAL_OUTPUT_FIELDS``), and the retriever uses it for the hotness blend, then
drops it: ``MatchedContext``, the context assembler's ``Candidate`` and
``AssembledEntry`` have no time field, so a ``<memory>`` tag in
``/api/v1/search/search`` (``mode: "context"``) says nothing about when the memory was
written. The Claude Code plugin worked around that with one ``fs/stat`` per URI,
~300 ms each and slower under write load.

Four hooks carry the time through without new I/O:

  * ``HierarchicalRetriever._convert_to_matched_contexts`` records each candidate's
    ``updated_at`` in a bounded per-process map keyed by its URI (display form and
    raw form). The value is a fact about that URI, not about the request, so a
    process-wide map is safe; only candidates a request retrieved are looked up.
  * ``budget._make_entry`` copies the time onto the ``AssembledEntry`` and recounts
    its tokens, so the budget planner pays for the attribute.
  * ``render.render_entry`` adds ``updated="YYYY-MM-DDTHH:MM:SSZ"`` after ``detail``.
  * ``AssembledEntry.to_dict`` adds ``updated_at`` to the JSON entries.

Measured 2026-09-29 on the pilot user: for 8 of 8 sampled memories the vector
``updated_at`` fell within 2 minutes of the file's ``modTime``. Access counting
(``increment_active_count``) re-upserts the full record, so it keeps the time.

Each hook is version/source guarded on its own; a hook that does not apply leaves the
tag without ``updated`` (the plugin then falls back to its stat). ``OV_RECALL_TIME_PATCH=0``
rolls all four back.
"""

import functools
import hashlib
import inspect
import logging
import os
from collections import OrderedDict
from datetime import datetime, timezone

EXPECTED_VERSION = "v0.4.20"
EXPECTED_SHA256 = {
    "convert": "18a9911e3aa793b5e5d966b09604717d7d06c914d340bdd0d70db95dd184ee32",
    "make_entry": "565a34fbf4bb310aa21884bf2b50d0ebdb79d11bd16bf76db1670a653d0d7470",
    "render_entry": "4ff788f4804f55c841d95d1e331a2f4093f8b36641ea5dfdcd89db03d1d764f8",
    "to_dict": "60715e66d58ef5b110f5a256cfdfc6065f01bcd314a5bb13cae6960ac6a26089",
}
MAX_TIMES = 20000
logger = logging.getLogger("ov_recall_time_patch")

_times = OrderedDict()


def normalize(value):
    """``updated_at`` as ``YYYY-MM-DDTHH:MM:SSZ`` in UTC, or None when unusable."""
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
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def remember(uri, value):
    if not uri or not value:
        return
    _times[uri] = value
    _times.move_to_end(uri)
    while len(_times) > MAX_TIMES:
        _times.popitem(last=False)


def lookup(*uris):
    for uri in uris:
        if uri and uri in _times:
            return _times[uri]
    return None


def recording_convert(original):
    @functools.wraps(original)
    async def convert(self, candidates, *args, **kwargs):
        results = await original(self, candidates, *args, **kwargs)
        for c in candidates or ():
            value = normalize(c.get("updated_at"))
            if not value:
                continue
            uri = c.get("uri", "")
            remember(uri, value)
            remember(self._append_level_suffix(uri, c.get("level", 2)), value)
        return results

    convert._ov_recall_time_patch = True
    return convert


def timed_make_entry(original, fragment_tokens):
    @functools.wraps(original)
    def make_entry(candidate, tier, text):
        entry = original(candidate, tier, text)
        value = lookup(
            getattr(candidate, "uri", ""), getattr(candidate, "base_uri", "")
        )
        if value:
            entry.updated_at = value
            entry.tokens = fragment_tokens(entry)
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


def apply_retriever(module):
    cls = getattr(module, "HierarchicalRetriever", None)
    original = getattr(cls, "_convert_to_matched_contexts", None)
    if getattr(original, "_ov_recall_time_patch", False):
        return True
    if not _guarded("convert", original):
        return False
    cls._convert_to_matched_contexts = recording_convert(original)
    logger.warning("ov-recall-time: convert applied (pid %d)", os.getpid())
    return True


def apply_budget(module):
    original = getattr(module, "_make_entry", None)
    if getattr(original, "_ov_recall_time_patch", False):
        return True
    if not _guarded("make_entry", original):
        return False
    module._make_entry = timed_make_entry(original, module.fragment_tokens)
    logger.warning("ov-recall-time: make_entry applied (pid %d)", os.getpid())
    return True


def apply_render(module):
    original = getattr(module, "render_entry", None)
    if getattr(original, "_ov_recall_time_patch", False):
        return True
    if not _guarded("render_entry", original):
        return False
    module.render_entry = timed_render_entry(original)
    logger.warning("ov-recall-time: render_entry applied (pid %d)", os.getpid())
    return True


def apply_models(module):
    cls = getattr(module, "AssembledEntry", None)
    original = getattr(cls, "to_dict", None)
    if getattr(original, "_ov_recall_time_patch", False):
        return True
    if not _guarded("to_dict", original):
        return False
    cls.to_dict = timed_to_dict(original)
    logger.warning("ov-recall-time: to_dict applied (pid %d)", os.getpid())
    return True
