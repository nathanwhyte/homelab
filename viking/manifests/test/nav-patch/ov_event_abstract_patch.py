"""Store an event memory's ``# Summary`` as its vector-record abstract (IMPR-1200).

OpenViking v0.4.20 writes each memory file's vector record with the whole file body
as its ``abstract`` (``MemoryUpdater._vectorize_memories``: ``abstract =
strip_all_links(mf.content)``, capped at 50 KB), and the reindex path keeps that same
text (``ReindexExecutor._reindex_memory_vectors``). For an ``events`` memory the body is
a short ``# Summary`` followed by the full ChatLog, so ``/api/v1/search/find`` and
list-mode ``search`` return 270 B to 28 KB per hit, and ten hits overflow an MCP tool
result. Upstream notes the gap (``retrieve/context_assembler/params.py``: events are
read from the file at the overview tier only because "the writer stores no separate
summary scalar").

Both paths build their record through ``EmbeddingMsgConverter.from_context``. This
patch wraps it and, after the stock body has run, replaces ``context_data["abstract"]``
with the Summary section for a level-2 memory record under ``/memories/events/``. The
embedding input (``EmbeddingMsg.message``, taken from the context's vectorize text) is
never touched, so ranking is unchanged; a body without a Summary keeps its abstract.
Existing records pick the Summary up on ``POST /api/v1/content/reindex``.

Guarded: applies only to openviking ``v0.4.20`` whose ``from_context`` source hashes to
the expected value; otherwise it logs "NOT applied" and stock behaviour stays. Disable
at runtime with ``OV_EVENT_ABSTRACT_PATCH=0``.
"""

import functools
import hashlib
import inspect
import logging
import os
import re

EXPECTED_VERSION = "v0.4.20"
EXPECTED_SHA256 = "fb15c3af326be29f826a870a8e6284e1663c122089769ae5df1c74c1e5bc6081"
logger = logging.getLogger("ov_event_abstract_patch")

# Mirrors extract_summary_section in retrieve/context_assembler/tiers.py (v0.4.20), the
# helper the overview tier already uses for these files; kept local so the write path
# does not import the retrieval package.
_SUMMARY_SECTION = re.compile(
    r"^#{1,3}[ \t]*Summary[ \t]*$\n(.*?)(?=^#{1,3}[ \t]|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
_SUMMARY_INLINE = re.compile(
    r"^\s*Summary:\s*(.*?)(?:\n\s*\d{4}-\d{2}-\d{2}"
    r"(?:\s*\([^)]+\))?\s*ChatLog:|\n\s*ChatLog:|\n\s*<!--\s*MEMORY_FIELDS|$)",
    re.IGNORECASE | re.DOTALL,
)
_DETAIL_LEVEL = 2


def summary_section(text):
    """Leading ``# Summary`` block, tolerating the legacy ``Summary:`` prefix; "" if none."""
    if not text:
        return ""
    match = _SUMMARY_SECTION.search(text)
    if match:
        return match.group(1).strip()
    inline = _SUMMARY_INLINE.search(text)
    if inline:
        return re.sub(r"\s+", " ", inline.group(1)).strip()
    return ""


def is_event_record(data):
    uri = str(data.get("uri") or "")
    return (
        data.get("context_type") == "memory"
        and data.get("level") == _DETAIL_LEVEL
        and "/memories/events/" in uri
        and uri.endswith(".md")
    )


def summarize_event_abstract(data):
    """Swap an event record's full-body abstract for its Summary, in place.

    Returns True when the abstract changed.
    """
    if not isinstance(data, dict) or not is_event_record(data):
        return False
    abstract = data.get("abstract") or ""
    summary = summary_section(abstract)
    if not summary or summary == abstract:
        return False
    data["abstract"] = summary
    return True


def summary_abstract(original):
    @functools.wraps(original)
    def from_context(*args, **kwargs):
        msg = original(*args, **kwargs)
        if msg is not None:
            summarize_event_abstract(getattr(msg, "context_data", None))
        return msg

    from_context._ov_event_abstract_patch = True
    return from_context


def apply(module):
    if os.environ.get("OV_EVENT_ABSTRACT_PATCH", "1") == "0":
        logger.warning("ov-event-abstract: disabled by OV_EVENT_ABSTRACT_PATCH=0")
        return False
    import openviking

    converter = getattr(module, "EmbeddingMsgConverter", None)
    original = getattr(converter, "from_context", None)
    if getattr(original, "_ov_event_abstract_patch", False):
        return True
    try:
        digest = hashlib.sha256(inspect.getsource(original).encode()).hexdigest()
    except (OSError, TypeError):
        digest = None
    version = getattr(openviking, "__version__", None)
    if version != EXPECTED_VERSION or digest != EXPECTED_SHA256:
        logger.warning(
            "ov-event-abstract: NOT applied (version=%r sha256=%s)", version, digest
        )
        return False
    converter.from_context = staticmethod(summary_abstract(original))
    logger.warning(
        "ov-event-abstract: applied; events abstracts store the Summary (pid %d)",
        os.getpid(),
    )
    return True
