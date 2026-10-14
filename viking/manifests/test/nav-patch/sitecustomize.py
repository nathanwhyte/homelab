"""Post-import hook that installs the OpenViking v0.4.20 runtime patches.

Mounted from a ConfigMap onto PYTHONPATH. Python's ``site`` imports this file at
interpreter start in every process, including the uvicorn workers. Each patch is
installed only in a process that imports its target module, so the ``ov`` CLI and
the entrypoint's helper interpreters are unaffected.

Patches (each guarded on the installed version + a source hash, each with its own
off switch; a patch failure never breaks the import):

  * ov_s3_cache_patch — BUG-1174: disable process-local S3 metadata caches so a
                        worker sees directories created by another worker.
  * ov_nav_patch      — IMPR-1185: deterministic Quick Navigation in directory
                        overviews (``SemanticProcessor._generate_overview``).
  * ov_extract_patch  — BUG-1176: a degraded memory extraction (empty/unparseable
                        LLM output after the loop's own retry) becomes a retryable
                        Phase 2 failure instead of an empty ``memory_diff.json``
                        recorded as success (``ExtractLoop.run`` + the session's
                        Phase 2 retry classifier and budget). BUG-1196: session-
                        extraction timeouts, and lock waits raised before the attempt
                        wrote anything, get extra retries on their own budgets with one
                        ``ov-extract-outcome {json}`` stderr line per retry/failure, and
                        long-term extraction is chunked for every session
                        (``retry_async`` + ``resolve_extraction_batch_limits``, plus
                        write tracking on ``StreamingMemoryUpdater``;
                        ``OV_EXTRACT_PATCH_CHUNK=0`` turns chunking off alone).
                        IMPR-1303: a commit's Phase 2 batches run concurrently,
                        ``OV_EXTRACT_PATCH_BATCH_PARALLEL`` (default 2, ``1`` is the
                        stock loop) at a time, each still its own recorded, retried
                        step; the compressor's per-batch ``memory_diff.json`` write
                        is collected instead and the merged file is written once in
                        batch order (``Session._extract_long_term_memories_with_batching``
                        + ``SessionCompressorV3._write_final_memory_diff``).
  * ov_chatlog_patch  — BUG-1177: ChatLog speaker labels follow the turn's role
                        (assistant turns read ``assistant``) and tool-only turns no
                        longer render as empty lines (``MessageRange._speaker_for`` +
                        ``_format_contiguous_group``).
  * ov_memory_guard_patch — BUG-1180: an ``add_only`` memory write (events,
                        trajectories) never replaces an existing memory; an exact
                        duplicate is dropped and a collision goes to a free sibling
                        path (``MemoryUpdater.apply_operations``); its echo
                        guard cuts pasted OpenViking recall output from user
                        turns before extraction (``ExtractContext.__init__``,
                        ``OV_ECHO_GUARD=0``); its noise filter drops ``events``
                        that only restate git lifecycle with no reasoning
                        (``OV_EVENT_NOISE_FILTER``: ``1`` drops, ``0`` off,
                        anything else logs only).
  * ov_event_abstract_patch — IMPR-1200: an ``events`` memory's vector record
                        stores its ``# Summary`` as the abstract instead of the
                        whole body; the embedding text is unchanged
                        (``EmbeddingMsgConverter.from_context``). Its reindex hook
                        makes ``/api/v1/content/reindex`` embed an event's write-path
                        text instead of the raw body (``ReindexExecutor._upsert_context``,
                        ``OV_EVENT_REINDEX_PATCH=0``), so the backfill runs in-server.
  * ov_grep_scope_patch — BUG-1189: a grep over a whole user root skips its
                        ``sessions/`` archives unless the caller names an exclusion,
                        and every grep is bounded by ``OV_GREP_TIMEOUT_S`` (default
                        12 s), returning a marked timeout line instead of an error
                        (``FSService.grep``).
  * ov_recall_time_patch — IMPR-1215: each ``<memory>`` tag in assembled context
                        carries its vector record's ``updated_at`` as
                        ``updated="…Z"``, through request-owned state
                        (``gather.gather_candidates``,
                        ``HierarchicalRetriever._convert_to_matched_contexts``,
                        ``budget._make_entry``, ``render.render_entry``,
                        ``AssembledEntry.to_dict``); reindex keeps a record's times
                        at the file's ``modTime`` (``ReindexExecutor._upsert_context``
                        + ``EmbeddingMsgConverter.from_context``, stacked on the
                        event-abstract hooks).
  * ov_fetch_items_patch — BUG-1218: fetch-by-id reads ``ov-vectordb``'s
                        ``data.items`` as well as upstream's ``data.fetch``, so
                        ``set_tags``, account-bound ``delete(ids)``, ``exists`` and
                        ``increment_active_count`` see the record
                        (``HttpCollection.fetch_data``).
  * ov_wm_update_patch — IMPR-1303: the session-commit ``update_working_memory``
                        tool call, which gemma4 answers in a form Ollama's parser
                        rejects every time (unquoted keys with spaces), is answered
                        locally with an empty ``VLMResponse`` so Branch 2 takes its
                        creation-prompt fallback without first spending a 60-250 s
                        generation (``OpenAIVLM.get_completion_async``); one
                        ``ov-wm-update-skip {json}`` stderr line per skip.
  * ov_claude_params_patch — IMPR-1311: a VLM request to a ``claude-*`` model drops
                        ``temperature``, which Claude Haiku 5.5 rejects and
                        ``VLMConfig`` cannot omit
                        (``OpenAIVLM._apply_completion_params``); inert for every
                        other model.

Rollback of one patch: its env switch (``OV_S3_CACHE_PATCH=0``, ``OV_EXTRACT_PATCH=0``, ``OV_CHATLOG_PATCH=0``, ``OV_MEMORY_GUARD=0``, ``OV_EVENT_NOISE_FILTER=0``, ``OV_EVENT_ABSTRACT_PATCH=0``, ``OV_GREP_SCOPE_PATCH=0``, ``OV_RECALL_TIME_PATCH=0``, ``OV_FETCH_ITEMS_PATCH=0``, ``OV_WM_UPDATE_SKIP=0``, ``OV_CLAUDE_PARAMS_PATCH=0``; ``OV_NAV_PATCH=0``
only after the model-built overview template is restored, see the Deployment).
Rollback of everything: remove PYTHONPATH from the Deployment
(``kubectl set env … PYTHONPATH-``), again only after that template restore.
"""

import importlib.abc
import importlib.machinery
import sys

# target module -> (patch module, apply function name)
TARGETS = {
    "openviking.utils.agfs_utils": ("ov_s3_cache_patch", "apply"),
    "openviking.storage.queuefs.semantic_processor": ("ov_nav_patch", "apply"),
    "openviking.session.memory.extract_loop": (
        "ov_extract_patch",
        "apply_extract_loop",
    ),
    "openviking.session.session": ("ov_extract_patch", "apply_session"),
    "openviking.session.compressor_v3": ("ov_extract_patch", "apply_compressor"),
    "openviking.session.memory.streaming_memory_updater": [
        ("ov_memory_guard_patch", "apply_streaming"),
        ("ov_extract_patch", "apply_streaming"),
    ],
    "openviking.session.memory.memory_updater": [
        ("ov_chatlog_patch", "apply"),
        ("ov_memory_guard_patch", "apply"),
        ("ov_memory_guard_patch", "apply_echo_guard"),
    ],
    # Recall-time wraps outermost: it sets a context's times, then event-abstract
    # swaps its abstract/vector text.
    "openviking.storage.queuefs.embedding_msg_converter": [
        ("ov_event_abstract_patch", "apply"),
        ("ov_recall_time_patch", "apply_converter"),
    ],
    "openviking.service.reindex_executor": [
        ("ov_event_abstract_patch", "apply_reindex"),
        ("ov_recall_time_patch", "apply_reindex"),
    ],
    "openviking.service.fs_service": ("ov_grep_scope_patch", "apply"),
    "openviking.retrieve.hierarchical_retriever": (
        "ov_recall_time_patch",
        "apply_retriever",
    ),
    "openviking.retrieve.context_assembler.models": (
        "ov_recall_time_patch",
        "apply_models",
    ),
    "openviking.retrieve.context_assembler.render": (
        "ov_recall_time_patch",
        "apply_render",
    ),
    "openviking.retrieve.context_assembler.budget": (
        "ov_recall_time_patch",
        "apply_budget",
    ),
    "openviking.retrieve.context_assembler.gather": (
        "ov_recall_time_patch",
        "apply_gather",
    ),
    "openviking.storage.vectordb.collection.http_collection": (
        "ov_fetch_items_patch",
        "apply",
    ),
    "openviking.models.vlm.backends.openai_vlm": [
        ("ov_wm_update_patch", "apply"),
        ("ov_claude_params_patch", "apply"),
    ],
}


def _entries(entry):
    """A target maps to one ``(patch, apply)`` pair or a list of them, applied in order."""
    return [entry] if isinstance(entry, tuple) else list(entry)


class _PatchingLoader(importlib.abc.Loader):
    def __init__(self, loader, entries):
        self._loader = loader
        self._entries = entries

    def create_module(self, spec):
        return self._loader.create_module(spec)

    def exec_module(self, module):
        self._loader.exec_module(module)
        for patch_module, apply_name in self._entries:
            try:
                patch = __import__(patch_module)
                getattr(patch, apply_name)(module)
            except Exception as exc:  # noqa: BLE001 — a patch failure must never break the import
                print(
                    f"{patch_module}: install failed for {module.__name__}: {exc!r}",
                    file=sys.stderr,
                )


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        entry = TARGETS.get(fullname)
        if entry is None:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is not None and spec.loader is not None:
            spec.loader = _PatchingLoader(spec.loader, _entries(entry))
        return spec


if not any(isinstance(f, _Finder) for f in sys.meta_path):
    sys.meta_path.insert(0, _Finder())
