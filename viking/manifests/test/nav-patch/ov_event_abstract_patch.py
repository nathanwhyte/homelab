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
with the Summary section for a level-2 memory record under ``/memories/events/``.
``EmbeddingMsg.message`` (the embedding input) is never modified — only read, to pick
the Summary source (see below) — so the text handed to the embedder is unchanged.

Summary source and reindex freshness: the wrapper prefers ``msg.message`` over
``context_data["abstract"]`` when ``message`` is a string. On the write path the two
already agree (``MemoryUpdater._vectorize_memories``: ``embedding_text = abstract =
strip_all_links(mf.content)`` unless a memory-type ``embedding_template`` renders a
different ``embedding_text`` — the abstract still carries the Summary either way).
On reindex (``ReindexExecutor._upsert_context``, ``vector_text=body``), ``message`` is
the untouched file body, while ``context_data["abstract"]`` prefers the *existing*
stored record's abstract (``_best_non_empty(self._record_abstract(existing), ...)``)
— so reading only ``abstract`` would keep a stale Summary after the file body changed
and the memory was reindexed. Reading ``message`` first means the Summary picked up is
always the one in the current file body. A body without a ``# Summary`` heading, or an
already-multimodal ``message`` (a list, not a string), falls back to the incoming
``abstract`` unchanged.

Reranking: ``HierarchicalRetriever`` only builds a rerank client when
``rerank_config.is_available()`` is true (``RerankConfig.is_available()``,
``openviking_cli/utils/config/rerank_config.py:99-110`` — requires a provider's
credentials: Cohere ``api_key``, OpenAI-compatible ``api_key``+``api_base``, LiteLLM
``model``, or VikingDB ``ak``+``sk``). Prod's ``rerank`` config
(``viking/manifests/openviking-configmap.yaml``) is ``{"threshold": 0.2}`` only, no
provider fields, so ``_effective_provider()`` returns ``None``, ``is_available()``
returns ``False``, and ``HierarchicalRetriever.__init__``
(``retrieve/hierarchical_retriever.py:88-96``) sets ``self._rerank_client = None`` and
defaults retrieval to ``RetrieverMode.QUICK`` (line 127) — no reranker runs today, so
this patch does not change ranking in production as currently configured. That is a
fact about the live config, not a code guarantee: if rerank is ever enabled,
``HierarchicalRetriever`` reranks each candidate on its stored ``abstract``
(``documents = [str(r.get("abstract", "")) for r in results]``,
``retrieve/hierarchical_retriever.py:526``, gated on ``mode == RetrieverMode.THINKING``
at line 525) — a Summary-only abstract would then be the rerank input instead of the
full body, and that tradeoff should be re-evaluated before rerank is turned on.

Existing records pick the Summary up on ``POST /api/v1/content/reindex``, but that
endpoint's embedding text is the raw file body (comment and links included), not the
write-path text — see ``viking/scripts/ov-event-abstract-backfill.py`` for a backfill
that reuses ``MemoryUpdater._vectorize_memories`` instead, so the embedding text
matches what a fresh write would produce; the same tool's ``--restore`` reverts to the
full-body abstract independent of the ``OV_EVENT_ABSTRACT_PATCH`` switch.

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


def summarize_event_abstract(data, message=None):
    """Swap an event record's abstract for its Summary, in place.

    Prefers ``message`` (the embedding text) as the Summary source when it is a
    string: on reindex that is the current file body, so a stale stored abstract
    never survives a body edit. Falls back to ``data["abstract"]`` when ``message``
    is absent, empty, or yields no Summary (e.g. a multimodal message). Never
    modifies ``message`` itself. Returns True when the abstract changed.
    """
    if not isinstance(data, dict) or not is_event_record(data):
        return False
    abstract = data.get("abstract") or ""
    source = message if isinstance(message, str) and message.strip() else ""
    summary = summary_section(source) if source else ""
    if not summary:
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
            summarize_event_abstract(
                getattr(msg, "context_data", None), getattr(msg, "message", None)
            )
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
