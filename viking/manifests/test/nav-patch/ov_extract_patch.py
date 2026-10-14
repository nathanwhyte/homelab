"""Degraded memory extraction becomes a retryable failure, not a silent empty diff (BUG-1176).

OpenViking v0.4.20's ``ExtractLoop.run`` gives the extraction LLM one format retry; when
that is exhausted with an empty, unparseable or refusal completion it returns a
``ResolvedOperations`` with no upserts and an ``errors`` list, and ``MemoryUpdater.apply``
turns that into ``result.errors`` and returns. The compressor then writes an empty
``memory_diff.json``, the session logs ``Extracted 0 memories`` and the archive gets its
``.done`` marker. A saturated VLM (the 2026-09-22 BUG-1173 backfill) therefore produced
three "successful" commits with nothing in them; the same archive replayed idle yielded
four memories.

This wrapper changes two things, both inside the existing Phase 2 machinery:

  * ``ExtractLoop.run`` — when the *session extraction* loop (context provider exactly
    ``SessionExtractContextProvider``) returns errors and no upserts, raise
    ``ExtractionDegradedError`` instead of returning. ``extract_long_term_memories`` runs with
    ``strict_extract_errors=True`` in the commit path, so the error reaches
    ``_run_retryable_phase2_step`` and is retried by ``retry_async``; if every retry fails the
    archive gets ``.failed.json`` (stage ``memory_extraction``) and the task fails — visible,
    exact, and re-drivable — instead of an empty diff recorded as success. The streaming
    memory updater's merge loops (``PatchMergeContextProvider``, ``max_iterations=1``) are
    left returning: they run after append-only events have already been written, and a
    whole-step retry there would regenerate and duplicate those events. A degraded merge
    is logged, not raised.
  * ``openviking.session.session`` — ``is_retryable_api_error`` also returns True for
    ``ExtractionDegradedError``, and the Phase 2 retry constants are widened from
    3 retries × (1 s … 8 s) to 5 retries × (15 s … 120 s): ``retry_async`` makes up to
    6 attempts with 5 capped, jittered sleeps (about 345–420 s of sleeping in all, no
    overall deadline), plus the extraction's own LLM time per attempt. Both are module
    globals the nested retry helper reads at call time. This half applies only after the
    loop half applied in the same process, so the guard governs both.

Also logs, at WARNING, an extraction that parsed fine but produced zero operations, with the
loop's last failure kind, so a future zero window is diagnosable from the pod log alone.
That parsed-empty outcome is indistinguishable from "nothing to extract" and is NOT raised.

Scope of the claim: this protects against a demonstrated source-code defect (an exhausted
retry becomes a successful empty diff). Whether the 2026-09-22 zero runs took this path or
returned a parsed empty program is not established by the INFO log; see BUG-1176.

BUG-1196 adds three things, each with its own guard and off switch (the session half,
plus a streaming half that tracks writes for the lock-wait retry):

  * Timeouts and lock waits are retried, each on its own small budget, only when nothing
    from the step's attempt has been written. Stock ``is_retryable_api_error`` matches
    the text "timeout", not "timed out", so ``openai.APITimeoutError`` ("Request timed
    out.") and ``LockAcquisitionError`` ("lock acquire timed out after 300000ms") both
    failed the archive on the first attempt. ``session.retry_async`` (the name
    ``_run_retryable_phase2_step`` looks up at call time) is wrapped with a per-call
    classifier that asks the caller's classifier first, so whatever stock already
    retries (a "504 Gateway Timeout", any text containing "timeout") keeps its stock
    retries; the class budgets only add retries stock would refuse. A timeout gets them
    only when the session-extraction ``ExtractLoop.run`` raised it before this attempt
    wrote anything (the loop half tags it). A lock wait gets them only when the
    ``StreamingMemoryUpdater`` append-only fast path failed to take its lease and it was
    the attempt's first write after session extraction (the streaming half tags it): the
    lease is taken before any write, and the fast path runs before the merge groups.
    Any other lock wait keeps stock behaviour, because a retry is a fresh LLM extraction
    with a new ``source_extraction_id``: the memory guard files its re-extracted events
    as collisions at ``<name>_2.md`` instead of dropping them, so retrying after events
    are written would duplicate them. Both stay under the overall
    ``_MEMORY_EXTRACTION_MAX_RETRIES`` cap. Budgets: ``OV_EXTRACT_PATCH_TIMEOUT_RETRIES``
    (default 2) and ``OV_EXTRACT_PATCH_LOCK_RETRIES`` (default 3); ``0`` turns that
    class's extra retries off and leaves stock retries alone.
  * Every retry, recovery and final failure of a Phase 2 step writes one unwrapped
    ``ov-extract-outcome {json}`` line on stderr naming the step, attempt and
    ``failure_class`` (``timeout``, ``lock_wait``, ``degraded`` or ``other``), so a lock
    wait reads differently from a model timeout in Loki. Not through ``logger``: the rich
    handler folds long records into several Loki entries (see ov_memory_guard_patch).
  * Long-term extraction is chunked. v0.4.20 already plans Phase 2 extraction batches,
    but only from a session's ``auto_commit_policy``, and Claude Code sessions carry none,
    so a 180-message archive went to the model as one prompt. The session module's
    ``resolve_extraction_batch_limits`` is wrapped to cap every session at
    ``OV_EXTRACT_PATCH_BATCH_MESSAGES`` (default 60) messages and
    ``OV_EXTRACT_PATCH_BATCH_TOKENS`` (default 24000) estimated tokens per batch, keeping a
    policy's tighter limit; ``0`` drops that cap and ``OV_EXTRACT_PATCH_CHUNK=0`` turns
    chunking off. Upstream records and retries each batch as its own step, so a failure
    redoes one batch, not the archive. The same limits batch the Working Memory summary.

IMPR-1303 (lever 2) runs a commit's Phase 2 batches concurrently. Stock
``Session._extract_long_term_memories_with_batching`` runs the planned batches in a
``for`` loop, so a chunked archive is N serial VLM calls on one session-commit worker;
after each batch it reads back the ``memory_diff.json`` the compressor just wrote for
that batch alone, appends it to the running list and rewrites the merged file. The loop
is replaced by ``asyncio.gather`` over the same ``record_batch`` steps under a semaphore
of ``OV_EXTRACT_PATCH_BATCH_PARALLEL`` (default 2; ``1`` or ``0`` keeps the stock
loop), so each batch is still its own recorded, retried step and BUG-1196's per-attempt
write tracking stays per batch (each step runs in its own task with its own context).
Two things cannot run concurrently and are handled so that the stock shape is kept:

  * The diff read-back: the compressor's per-batch ``memory_diff.json`` overwrite would
    be read by the wrong batch, so ``SessionCompressorV3._write_final_memory_diff`` is
    wrapped to hand its diff to the calling batch's collector (a context variable set
    only inside a batch task) instead of writing the file, and the batch step itself,
    still inside ``record_batch`` so before its messages are marked complete, rewrites
    the merged file with the finished batches' diffs in batch order under a lock. A
    recovery after a crash therefore finds the diff of every batch it will skip, as
    with stock; a failed batch (after its own retries) still fails the archive with
    the first failure in batch order.
  * The streaming apply: ``StreamingMemoryUpdater`` flushes requests that arrive in one
    window together and scopes the result back by archive and session id, which
    sibling batches share, so two batches in one flush would each receive the other's
    operations and contexts, and after the apply the compressor reads the memory
    files to build the diff and canonical cases. ``StreamingMemoryUpdater.submit`` is
    wrapped to take a per-archive lock (a gate in a context variable set only inside a
    batch task) at the batch's first submit and keep it until the batch's extraction
    step ends, so the extraction calls overlap and the applies, merge-loop calls and
    read-backs included, run one batch at a time as stock ran them; a failed submit
    first flushes every merge group (joining one already in flight), so a sibling
    cannot join or race it. The CaseSpec fast path writes its case memory without
    ``submit`` (``SessionCompressorV3._write_training_case_memory``), so that write
    takes the same gate.

One ``ov-extract-outcome {"event": "parallel_batches", …}`` line per archive names the
batch count, the parallelism, the failures and the wall time. Under gemma the four
Ollama slots still bound the work per hour, so this shortens one large commit's wall
time; behind the LiteLLM router the overflow goes to Haiku and it becomes a throughput
lever. Needs all the wrappers (session batching, the two compressor writes, submit
lock) applied, each on its own source hash; with any but the first missing the stock
loop runs.

Guarded: applies only to openviking ``v0.4.20`` whose ``ExtractLoop.run`` source hashes to
``EXPECTED_RUN_SHA256``; otherwise it logs "NOT applied" and stock behaviour stays. The
retry and chunking wrappers, the parallel-batch pair, and the streaming half's three
write-tracking wrappers (``openviking.session.memory.streaming_memory_updater``), are
each also guarded on the source hash of the function they wrap. Without the streaming
half no lock wait is ever tagged, so lock waits keep stock behaviour. Disable everything
at runtime with ``OV_EXTRACT_PATCH=0``. Retry budget overrides:
``OV_EXTRACT_PATCH_RETRIES``, ``OV_EXTRACT_PATCH_BASE_DELAY``,
``OV_EXTRACT_PATCH_MAX_DELAY``.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import hashlib
import inspect
import json
import logging
import os
import re
import sys
import time

EXPECTED_VERSION = "v0.4.20"
EXPECTED_RUN_SHA256 = "f9bf435228ca4a82200442f47752a0f4b7e2213a0c86dcb08a6a773357156876"
# BUG-1196: openviking.utils.model_retry.retry_async and
# openviking.session.extraction_batch.resolve_extraction_batch_limits on v0.4.20.
EXPECTED_RETRY_ASYNC_SHA256 = (
    "0b5f1eb95670455767e1b671c132cfd6a814d3d62bfc94b59b155f23816a8fa7"
)
EXPECTED_RESOLVE_LIMITS_SHA256 = (
    "811fff5754eee8fed3fdfde1c4c9750901249ef3c66041032f6082959c47b9b1"
)
# openviking.session.memory.streaming_memory_updater on v0.4.20: the append-only fast
# path, the merge-group submit, and the module-level lease acquisition.
EXPECTED_APPEND_ONLY_SHA256 = (
    "3e24e1c676c8bd718109d206c94e1b155c3d99af1ed367398efc4acba94cc58f"
)
EXPECTED_GROUPED_MERGE_SHA256 = (
    "f33842c5447652ad21db1d6763bfcec54f41e4521a5d96534a93b09388061af8"
)
EXPECTED_ACQUIRE_LEASE_SHA256 = (
    "01461f9e570eddbb4854149f24a0fe5455adb5b860142650513e77748936a23c"
)
# IMPR-1303 lever 2: Session._extract_long_term_memories_with_batching and
# SessionCompressorV3._write_final_memory_diff on v0.4.20.
EXPECTED_BATCHING_SHA256 = (
    "eca14bd38e10946d5b4838d1602ced15dc0a2c1234863a0f5183051da95abe1b"
)
EXPECTED_WRITE_FINAL_DIFF_SHA256 = (
    "d9b9a4734a348381f3cb71f9c5236f5b64455d7185f90063944a522835ef5029"
)
# StreamingMemoryUpdater.submit on v0.4.20.
EXPECTED_SUBMIT_SHA256 = (
    "67e4e598f003d0d46f6790ac19f469a367cdfa5d65203b12b8a10f596989c0ee"
)
# SessionCompressorV3._write_training_case_memory on v0.4.20: the CaseSpec fast
# path's direct write, which never goes through StreamingMemoryUpdater.submit.
EXPECTED_TRAINING_CASE_SHA256 = (
    "5ad1aa455c1c3c04ff5add35af9bae50b1365345b02ce9b214030f4aaf5808b2"
)
DEFAULT_BATCH_PARALLEL = 2
DEFAULT_RETRIES = 5
DEFAULT_BASE_DELAY_SECONDS = 15.0
DEFAULT_MAX_DELAY_SECONDS = 120.0
DEFAULT_TIMEOUT_RETRIES = 2
DEFAULT_LOCK_RETRIES = 3
DEFAULT_BATCH_MESSAGES = 60
DEFAULT_BATCH_TOKENS = 24000

FAILURE_TIMEOUT = "timeout"
FAILURE_LOCK_WAIT = "lock_wait"
FAILURE_DEGRADED = "degraded"
FAILURE_OTHER = "other"
OUTCOME_PREFIX = "ov-extract-outcome "
STAGE_ATTR = "_ov_extract_stage"
STAGE_SESSION_EXTRACT = "session_extract"
STAGE_BEFORE_WRITE = "before_write"  # lock wait from the attempt's first lease
LEASE_ATTR = "_ov_extract_lease_wait"

# Per-attempt write state, set by the retry wrapper around each attempt. ``armed``: the
# session-extraction loop returned in this attempt; ``wrote``: something may have been
# written. Tasks spawned inside the attempt copy the context and share the dict.
_STEP: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "ov_extract_step", default=None
)
# IMPR-1303 lever 2: inside a parallel batch task, the list the compressor's final
# memory_diff write hands its diffs to instead of writing ``memory_diff.json``.
_DIFFS: contextvars.ContextVar[list | None] = contextvars.ContextVar(
    "ov_extract_batch_diffs", default=None
)
# IMPR-1303 lever 2: inside a parallel batch task, the batch's gate on the archive's
# apply lock, taken at its first ``StreamingMemoryUpdater.submit`` and released when
# the batch's extraction step ends, so sibling batches never share a flush or read
# each other's fresh writes.
_SUBMIT_LOCK: contextvars.ContextVar[BatchGate | None] = contextvars.ContextVar(
    "ov_extract_batch_submit_gate", default=None
)

logger = logging.getLogger("ov_extract_patch")
SESSION_PROVIDER = (
    "SessionExtractContextProvider"  # exact class; subclasses are merge loops
)
_state = {
    "loop_applied": False,
    "compressor_applied": False,
    "submit_lock_applied": False,
}

_LOCK_WAIT_TEXT = re.compile(
    r"lock acquir\w* timed out|pathlock\S* .*timed out", re.IGNORECASE
)
_TIMEOUT_TEXT = re.compile(r"\btimed out\b|\btimeout\b", re.IGNORECASE)


class ExtractionDegradedError(RuntimeError):
    """The extraction LLM produced no usable operations after the loop's own retry."""


def _source_hash(fn) -> str | None:
    try:
        return hashlib.sha256(inspect.getsource(fn).encode()).hexdigest()
    except (OSError, TypeError):
        return None


def _enabled() -> bool:
    if os.environ.get("OV_EXTRACT_PATCH", "1") == "0":
        logger.warning("ov-extract-patch: disabled by OV_EXTRACT_PATCH=0")
        return False
    return True


# --- failure classification (BUG-1196) -------------------------------------------------


def _chain(error: BaseException) -> list[BaseException]:
    """``error`` plus its ``__cause__``/``__context__`` chain, cycle-safe."""
    seen: set[int] = set()
    out: list[BaseException] = []
    cur: BaseException | None = error
    while cur is not None and id(cur) not in seen:
        out.append(cur)
        seen.add(id(cur))
        cur = cur.__cause__ or cur.__context__
    return out


def _class_names(error: BaseException) -> set[str]:
    return {cls.__name__ for cls in type(error).__mro__}


def failure_class(error: BaseException) -> str:
    """Classify a Phase 2 step failure as degraded, lock_wait, timeout or other.

    Lock waits are checked before timeouts because their text says "timed out" too.
    Class names are matched instead of imported types so this module stays importable
    without openviking, openai or httpx.
    """
    chain = _chain(error)
    if any(isinstance(e, ExtractionDegradedError) for e in chain):
        return FAILURE_DEGRADED
    for e in chain:
        if "LockAcquisitionError" in _class_names(e) or _LOCK_WAIT_TEXT.search(str(e)):
            return FAILURE_LOCK_WAIT
    for e in chain:
        if (
            isinstance(e, TimeoutError)
            or any(
                n.endswith(("Timeout", "TimeoutError", "TimeoutException"))
                for n in _class_names(e)
            )
            or _TIMEOUT_TEXT.search(str(e))
        ):
            return FAILURE_TIMEOUT
    return FAILURE_OTHER


def _tag(error: BaseException, attr: str, value) -> None:
    with contextlib.suppress(AttributeError, TypeError):
        setattr(error, attr, value)


def _has_tag(error: BaseException, attr: str, value) -> bool:
    return any(getattr(e, attr, None) == value for e in _chain(error))


def is_session_extract_timeout(error: BaseException) -> bool:
    """True for a timeout the loop half tagged as raised by session extraction."""
    return failure_class(error) == FAILURE_TIMEOUT and _has_tag(
        error, STAGE_ATTR, STAGE_SESSION_EXTRACT
    )


def is_before_write_lock_wait(error: BaseException) -> bool:
    """True for a lock wait the streaming half tagged as raised before any write."""
    return failure_class(error) == FAILURE_LOCK_WAIT and _has_tag(
        error, STAGE_ATTR, STAGE_BEFORE_WRITE
    )


def _step_wrote() -> bool:
    state = _STEP.get()
    return state is not None and state["wrote"]


def _mark_wrote() -> None:
    state = _STEP.get()
    if state is not None:
        state["wrote"] = True


def emit_outcome(record: dict) -> None:
    """Write one ``ov-extract-outcome {json}`` line on stderr, never folded by rich."""
    # Diagnostics must never fail a step.
    with contextlib.suppress(Exception):
        sys.stderr.write(OUTCOME_PREFIX + json.dumps(record, sort_keys=True) + "\n")
        sys.stderr.flush()


# --- ExtractLoop half (BUG-1176) -------------------------------------------------------


def wrap_run(orig):
    """Wrap ``ExtractLoop.run``: errors-only result raises; zero-op result is logged."""

    @functools.wraps(orig)
    async def run(self):
        provider = type(getattr(self, "context_provider", None)).__name__
        try:
            operations, tools_used = await orig(self)
        except Exception as exc:
            # BUG-1196: session extraction runs before anything from this batch is
            # written, so its timeouts are safe for the Phase 2 retry to repeat. Tag them
            # unless this attempt already wrote; the session-half classifier gives extra
            # retries only to tagged timeouts.
            if (
                provider == SESSION_PROVIDER
                and failure_class(exc) == FAILURE_TIMEOUT
                and not _step_wrote()
            ):
                _tag(exc, STAGE_ATTR, STAGE_SESSION_EXTRACT)
            raise
        if provider == SESSION_PROVIDER:
            # Arms the streaming half: the next lease the attempt takes is its first write.
            state = _STEP.get()
            if state is not None and not state["wrote"]:
                state["armed"] = True
        if operations is None:
            return operations, tools_used
        upserts = getattr(operations, "upsert_operations", None) or []
        errors = list(getattr(operations, "errors", None) or [])
        kind = getattr(self, "_last_llm_failure_kind", None) or "unknown"
        if errors and not upserts:
            if provider != SESSION_PROVIDER:
                logger.warning(
                    "ov-extract-patch: degraded %s loop (failure_kind=%s: %s); returned as-is "
                    "because a retry here would duplicate already-appended events",
                    provider,
                    kind,
                    errors[0][:200],
                )
                return operations, tools_used
            logger.warning(
                "ov-extract-patch: extraction degraded (failure_kind=%s, %d error(s): %s); "
                "raising for Phase 2 retry instead of recording an empty diff",
                kind,
                len(errors),
                errors[0][:200],
            )
            raise ExtractionDegradedError(
                f"memory extraction degraded: failure_kind={kind}: {errors[0][:200]}"
            )
        if not upserts:
            deletes = getattr(operations, "delete_file_contents", None) or []
            logger.warning(
                "ov-extract-patch: extraction parsed but produced 0 upsert operations "
                "(deletes=%d, last_failure_kind=%s)",
                len(deletes),
                kind,
            )
        return operations, tools_used

    run._ov_extract_patch = True  # type: ignore[attr-defined]
    return run


def wrap_is_retryable(orig):
    @functools.wraps(orig)
    def is_retryable(error):
        if isinstance(error, ExtractionDegradedError):
            return True
        return orig(error)

    is_retryable._ov_extract_patch = True  # type: ignore[attr-defined]
    return is_retryable


# --- budgets ---------------------------------------------------------------------------


def _env_num(name, default, cast, *, allow_zero=False):
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = cast(raw)
    except ValueError:
        logger.warning("ov-extract-patch: ignoring %s=%r", name, raw)
        return default
    if value > 0 or (allow_zero and value == 0):
        return value
    return default


def retry_budget() -> tuple[int, float, float]:
    return (
        _env_num("OV_EXTRACT_PATCH_RETRIES", DEFAULT_RETRIES, int),
        _env_num("OV_EXTRACT_PATCH_BASE_DELAY", DEFAULT_BASE_DELAY_SECONDS, float),
        _env_num("OV_EXTRACT_PATCH_MAX_DELAY", DEFAULT_MAX_DELAY_SECONDS, float),
    )


def class_budget() -> dict[str, int]:
    """Per-step retry allowance for the classes stock OpenViking never retries."""
    return {
        FAILURE_TIMEOUT: _env_num(
            "OV_EXTRACT_PATCH_TIMEOUT_RETRIES",
            DEFAULT_TIMEOUT_RETRIES,
            int,
            allow_zero=True,
        ),
        FAILURE_LOCK_WAIT: _env_num(
            "OV_EXTRACT_PATCH_LOCK_RETRIES", DEFAULT_LOCK_RETRIES, int, allow_zero=True
        ),
    }


def batch_caps() -> tuple[int | None, int | None]:
    """(max messages, max estimated tokens) per extraction batch; None means no cap."""
    messages = _env_num(
        "OV_EXTRACT_PATCH_BATCH_MESSAGES", DEFAULT_BATCH_MESSAGES, int, allow_zero=True
    )
    tokens = _env_num(
        "OV_EXTRACT_PATCH_BATCH_TOKENS", DEFAULT_BATCH_TOKENS, int, allow_zero=True
    )
    return (messages or None, tokens or None)


def batch_parallelism() -> int:
    """Concurrent Phase 2 batches per commit; 1 is the stock sequential loop."""
    value = _env_num(
        "OV_EXTRACT_PATCH_BATCH_PARALLEL", DEFAULT_BATCH_PARALLEL, int, allow_zero=True
    )
    return max(value, 1)


# --- session half: retry classifier (BUG-1196) -----------------------------------------


def wrap_retry_async(orig, default_is_retryable):
    """Wrap ``retry_async`` with per-class budgets and one outcome line per event.

    ``retry_async`` asks ``is_retryable`` only while attempts remain, so each True
    returned here is one retry actually taken. The caller's classifier is asked first,
    so stock retries are never reduced; a class budget only adds retries for a tagged
    error the caller's classifier refused (a session-extraction timeout, or a lock wait
    raised before the attempt wrote anything).
    """

    @functools.wraps(orig)
    async def retry_async(func, *, max_retries, is_retryable=None, **kwargs):
        base = is_retryable or default_is_retryable
        operation = kwargs.get("operation_name", "operation")
        budget = class_budget()
        used = {FAILURE_TIMEOUT: 0, FAILURE_LOCK_WAIT: 0}
        attempts = 0

        def decide(error):
            cls = failure_class(error)
            allowed = bool(base(error))
            if not allowed and (
                is_session_extract_timeout(error) or is_before_write_lock_wait(error)
            ):
                allowed = used[cls] < budget[cls]
                if allowed:
                    used[cls] += 1
            if allowed:
                emit_outcome(
                    {
                        "event": "retry",
                        "step": operation,
                        "attempt": attempts,
                        "failure_class": cls,
                        "error": str(error)[:300],
                    }
                )
            return allowed

        async def counted():
            nonlocal attempts
            attempts += 1
            token = _STEP.set({"armed": False, "wrote": False})
            try:
                return await func()
            finally:
                _STEP.reset(token)

        try:
            result = await orig(
                counted, max_retries=max_retries, is_retryable=decide, **kwargs
            )
        except Exception as exc:
            emit_outcome(
                {
                    "event": "failed",
                    "step": operation,
                    "attempts": attempts,
                    "failure_class": failure_class(exc),
                    "error": str(exc)[:300],
                }
            )
            raise
        if attempts > 1:
            emit_outcome(
                {"event": "recovered", "step": operation, "attempts": attempts}
            )
        return result

    retry_async._ov_extract_patch = True  # type: ignore[attr-defined]
    return retry_async


# --- session half: chunking (BUG-1196) -------------------------------------------------


def _tighter(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def wrap_resolve_limits(orig, limits_cls):
    """Cap every session's extraction batches, keeping a policy's tighter limit."""

    @functools.wraps(orig)
    def resolve_extraction_batch_limits(auto_commit_policy):
        limits = orig(auto_commit_policy)
        if os.environ.get("OV_EXTRACT_PATCH_CHUNK", "1") == "0":
            return limits
        max_messages, max_tokens = batch_caps()
        return limits_cls(
            max_message_tokens=_tighter(limits.max_message_tokens, max_tokens),
            max_messages=_tighter(limits.max_messages, max_messages),
        )

    resolve_extraction_batch_limits._ov_extract_patch = True  # type: ignore[attr-defined]
    return resolve_extraction_batch_limits


# --- parallel Phase 2 batches (IMPR-1303 lever 2) ---------------------------------------


def wrap_write_final_memory_diff(orig):
    """Inside a parallel batch, hand the diff to the batch's collector; else write.

    ``extract_long_term_memories`` ends in exactly one call to this, on whichever of
    its two paths ran (the cached fast path at compressor_v3.py 553-570 or the normal
    path at 502-509), with every diff the batch produced, so the collector keeps the
    latest call's dicts, as the file would have held them.
    """

    @functools.wraps(orig)
    async def _write_final_memory_diff(self, *, archive_uri, ctx, memory_diffs):
        collector = _DIFFS.get()
        if collector is None:
            return await orig(
                self, archive_uri=archive_uri, ctx=ctx, memory_diffs=memory_diffs
            )
        collector[:] = [diff for diff in memory_diffs if isinstance(diff, dict)]
        return None

    _write_final_memory_diff._ov_extract_patch = True  # type: ignore[attr-defined]
    return _write_final_memory_diff


class BatchGate:
    """The per-archive apply lock a parallel batch holds from its first ``submit``
    until its extraction step ends, so the diff and case read-back after the apply
    see no sibling's writes either. Released by the batch step, not by ``submit``."""

    def __init__(self, lock):
        self.lock = lock
        self.held = False

    async def acquire(self):
        if not self.held:
            await self.lock.acquire()
            self.held = True

    def release(self):
        if self.held:
            self.held = False
            self.lock.release()


async def _settle_pending_flushes(updater):
    """Best effort: flush any merge group still buffered after a failed ``submit``.

    Stock ``_submit_grouped_merge_request`` gathers its group submissions fail-fast,
    so a failure can leave another group buffered in its batcher; a sibling submitting
    after the gate opened could join that pending flush and receive its operations.
    Flushing it first (its result is dropped, as the failed step's would have been)
    closes that window. Any error here is logged and swallowed: the step is failing
    already and will retry.
    """
    batchers = getattr(updater, "_group_batchers", None)
    if not isinstance(batchers, dict):
        return
    for batcher in list(batchers.values()):
        # Unconditionally: StreamingBatcher.flush empties its buffer before it awaits
        # the apply, so a buffered-count check misses a flush already in progress;
        # flush's own lock joins that flush and returns once it has applied.
        try:
            await batcher.flush("ov-extract-patch: settle after a failed submit")
        except Exception as exc:  # noqa: BLE001 — diagnostics must not mask the step's error
            logger.warning("ov-extract-patch: settling a pending flush failed: %r", exc)


def wrap_training_case_write(orig):
    """Take the batch's gate before the CaseSpec fast path's direct memory write.

    ``extract_long_term_memories``'s fast path (compressor_v3.py 553-570 via
    ``_write_training_case_memory``, 600-650) applies its case memory through
    ``MemoryUpdater.apply_operations`` directly and then builds its diff, never
    entering ``submit``; without the gate that write and read-back would run beside a
    sibling's. Outside a parallel batch the call is untouched.
    """

    @functools.wraps(orig)
    async def _write_training_case_memory(self, **kwargs):
        gate = _SUBMIT_LOCK.get()
        if gate is not None:
            await gate.acquire()
        return await orig(self, **kwargs)

    _write_training_case_memory._ov_extract_patch = True  # type: ignore[attr-defined]
    return _write_training_case_memory


def wrap_submit(orig):
    """Serialize the apply phase across the parallel batches of one archive.

    The streaming updater flushes requests that arrive inside one count/time window
    together and scopes the result back to each submitter by archive and session id,
    which sibling batches of one archive share, so two batches in one flush would each
    receive the other's operations, diffs and contexts; and after ``submit`` returns
    the compressor reads the memory files to build the diff and canonical cases. The
    batch's ``BatchGate`` is taken at its first ``submit`` and held until its
    extraction step ends, so applies and their read-backs run one batch at a time
    while the extraction calls still overlap. A failed ``submit`` first settles any
    group still buffered. Outside a parallel batch the call is untouched.
    """

    @functools.wraps(orig)
    async def submit(self, request):
        gate = _SUBMIT_LOCK.get()
        if gate is None:
            return await orig(self, request)
        await gate.acquire()
        try:
            return await orig(self, request)
        except BaseException:
            await _settle_pending_flushes(self)
            raise

    submit._ov_extract_patch = True  # type: ignore[attr-defined]
    return submit


def wrap_batching(orig, plan_batches, is_storage_not_found):
    """Run the planned Phase 2 batches concurrently, merging their diffs in order."""

    @functools.wraps(orig)
    async def _extract_long_term_memories_with_batching(
        self, *, messages, limits, archive_uri, extract_batch, record_batch
    ):
        parallel = batch_parallelism()

        async def stock():
            return await orig(
                self,
                messages=messages,
                limits=limits,
                archive_uri=archive_uri,
                extract_batch=extract_batch,
                record_batch=record_batch,
            )

        if (
            parallel <= 1
            or not _state["compressor_applied"]
            or not _state["submit_lock_applied"]
        ):
            return await stock()
        batches = plan_batches(messages, limits)
        if len(batches) <= 1:
            return await stock()

        previous: list = []
        viking_fs = getattr(self, "_viking_fs", None)
        if viking_fs:
            try:
                previous_raw = await viking_fs.read_file(
                    f"{archive_uri}/memory_diff.json", ctx=self.ctx
                )
                previous_diff = json.loads(previous_raw or "{}")
                if isinstance(previous_diff, dict):
                    previous.append(previous_diff)
            except Exception as exc:
                if not is_storage_not_found(exc):
                    raise

        collected: list[list] = [[] for _ in batches]
        finished: list[bool] = [False] * len(batches)
        semaphore = asyncio.Semaphore(parallel)
        write_lock = asyncio.Lock()
        submit_lock = asyncio.Lock()
        started = time.monotonic()

        async def write_merged():
            # The merged file holds the finished batches' diffs in batch order, so
            # it is current whenever a batch's step completes, as stock's per-batch
            # rewrite left it: a recovery after a crash finds the diff of every batch
            # whose messages are marked complete.
            if not viking_fs:
                return
            diffs = list(previous)
            for index, done in enumerate(finished):
                if done:
                    diffs.extend(collected[index])
            await self._session_compressor._write_final_memory_diff(
                archive_uri=archive_uri, ctx=self.ctx, memory_diffs=diffs
            )

        async def run_batch(index, batch):
            batch_messages = list(batch.messages)

            async def extract():
                # One collector per attempt: a retried attempt starts empty, as the
                # stock per-batch overwrite would have left the file.
                collected[index].clear()
                finished[index] = False
                gate = BatchGate(submit_lock)
                diffs_token = _DIFFS.set(collected[index])
                gate_token = _SUBMIT_LOCK.set(gate)
                try:
                    result = await extract_batch(batch_messages)
                finally:
                    _SUBMIT_LOCK.reset(gate_token)
                    _DIFFS.reset(diffs_token)
                    gate.release()
                # Still inside the recorded step: the diff reaches disk before the
                # step marks its messages complete, and a failed write retries the
                # step like stock's read-back would.
                async with write_lock:
                    finished[index] = True
                    await write_merged()
                return result

            async with semaphore:
                return await record_batch(
                    f"long_term_memory_extraction_batch_{index + 1}",
                    "long_term",
                    batch_messages,
                    extract,
                )

        results = await asyncio.gather(
            *(run_batch(i, batch) for i, batch in enumerate(batches)),
            return_exceptions=True,
        )
        failures = [r for r in results if isinstance(r, BaseException)]
        emit_outcome(
            {
                "event": "parallel_batches",
                "step": "long_term_memory_extraction",
                "batches": len(batches),
                "parallel": parallel,
                "failed": len(failures),
                "diffs": sum(len(d) for d in collected),
                "elapsed_s": round(time.monotonic() - started, 1),
            }
        )
        if failures:
            raise failures[0]

        contexts: list = []
        skills: list = []
        for result in results:
            if isinstance(result, dict):
                contexts.extend(result.get("contexts", []))
                skills.extend(result.get("session_skills", []))
            else:
                contexts.extend(result or [])
        if skills:
            return {"contexts": contexts, "session_skills": skills}
        return contexts

    _extract_long_term_memories_with_batching._ov_extract_patch = True  # type: ignore[attr-defined]
    return _extract_long_term_memories_with_batching


# --- streaming half: write tracking (BUG-1196) -----------------------------------------


def wrap_acquire_lease(orig):
    """Mark a lock wait raised while taking a memory-apply lease (before its writes)."""

    @functools.wraps(orig)
    async def _acquire_stable_operation_lease(*args, **kwargs):
        try:
            return await orig(*args, **kwargs)
        except Exception as exc:
            if failure_class(exc) == FAILURE_LOCK_WAIT:
                _tag(exc, LEASE_ATTR, True)
            raise

    _acquire_stable_operation_lease._ov_extract_patch = True  # type: ignore[attr-defined]
    return _acquire_stable_operation_lease


def wrap_append_only(orig):
    """Tag a lease wait in the attempt's first write; otherwise mark the attempt written.

    Runs in the submitter's own task, first in ``submit``. A lock wait is tagged
    ``before_write`` only when session extraction armed this attempt, nothing has been
    written since, and the wait came from taking the lease (which precedes every write
    of the fast path).
    """

    @functools.wraps(orig)
    async def _apply_append_only_request_now(self, request):
        state = _STEP.get()
        fresh = state is not None and state["armed"] and not state["wrote"]
        try:
            result = await orig(self, request)
        except Exception as exc:
            if fresh and _has_tag(exc, LEASE_ATTR, True):
                _tag(exc, STAGE_ATTR, STAGE_BEFORE_WRITE)
            else:
                _mark_wrote()
            raise
        _mark_wrote()
        return result

    _apply_append_only_request_now._ov_extract_patch = True  # type: ignore[attr-defined]
    return _apply_append_only_request_now


def wrap_grouped_merge(orig):
    """Mark the attempt written before merge groups run.

    Group flushes run in batcher tasks whose context belongs to whoever started them, so
    their writes are recorded here, in the submitter's task, before they can happen.
    """

    @functools.wraps(orig)
    async def _submit_grouped_merge_request(self, request):
        _mark_wrote()
        return await orig(self, request)

    _submit_grouped_merge_request._ov_extract_patch = True  # type: ignore[attr-defined]
    return _submit_grouped_merge_request


# --- installers -------------------------------------------------------------------------


def apply_extract_loop(module) -> bool:
    """Patch ``module.ExtractLoop.run`` in place; True if applied."""
    if not _enabled():
        return False
    import openviking

    version = getattr(openviking, "__version__", None)
    cls = getattr(module, "ExtractLoop", None)
    orig = getattr(cls, "run", None)
    if getattr(orig, "_ov_extract_patch", False):
        return True
    digest = _source_hash(orig) if orig else None
    if version != EXPECTED_VERSION or digest != EXPECTED_RUN_SHA256:
        logger.warning(
            "ov-extract-patch: NOT applied to ExtractLoop.run (version=%r source_sha256=%s); "
            "stock extraction behaviour kept",
            version,
            digest,
        )
        return False
    cls.run = wrap_run(orig)
    _state["loop_applied"] = True
    logger.warning("ov-extract-patch: applied to ExtractLoop.run (pid %d)", os.getpid())
    return True


def _apply_retry_wrapper(module) -> bool:
    orig = getattr(module, "retry_async", None)
    if getattr(orig, "_ov_extract_patch", False):
        return True
    digest = _source_hash(orig) if orig else None
    if digest != EXPECTED_RETRY_ASYNC_SHA256:
        logger.warning(
            "ov-extract-patch: NOT applied to session retry_async (source_sha256=%s); "
            "timeouts and lock waits stay unretried",
            digest,
        )
        return False
    module.retry_async = wrap_retry_async(orig, module.is_retryable_api_error)
    budget = class_budget()
    logger.warning(
        "ov-extract-patch: applied to session retry_async (timeout_retries=%d "
        "lock_retries=%d, pid %d)",
        budget[FAILURE_TIMEOUT],
        budget[FAILURE_LOCK_WAIT],
        os.getpid(),
    )
    return True


def _apply_chunking(module) -> bool:
    if os.environ.get("OV_EXTRACT_PATCH_CHUNK", "1") == "0":
        logger.warning(
            "ov-extract-patch: chunking disabled by OV_EXTRACT_PATCH_CHUNK=0"
        )
        return False
    orig = getattr(module, "resolve_extraction_batch_limits", None)
    limits_cls = getattr(module, "ExtractionBatchLimits", None)
    if getattr(orig, "_ov_extract_patch", False):
        return True
    digest = _source_hash(orig) if orig else None
    if digest != EXPECTED_RESOLVE_LIMITS_SHA256 or limits_cls is None:
        logger.warning(
            "ov-extract-patch: NOT applied to extraction batching (source_sha256=%s "
            "limits_cls=%r); stock batching kept",
            digest,
            limits_cls is not None,
        )
        return False
    module.resolve_extraction_batch_limits = wrap_resolve_limits(orig, limits_cls)
    max_messages, max_tokens = batch_caps()
    logger.warning(
        "ov-extract-patch: applied extraction batching (max_messages=%s max_tokens=%s, pid %d)",
        max_messages,
        max_tokens,
        os.getpid(),
    )
    return True


def apply_session(module) -> bool:
    """Patch the retry classifier, constants, retry wrapper and batching in the session."""
    if not _enabled():
        return False
    import openviking

    version = getattr(openviking, "__version__", None)
    orig = getattr(module, "is_retryable_api_error", None)
    needed = (
        "_MEMORY_EXTRACTION_MAX_RETRIES",
        "_MEMORY_EXTRACTION_RETRY_BASE_DELAY_SECONDS",
        "_MEMORY_EXTRACTION_RETRY_MAX_DELAY_SECONDS",
    )
    if getattr(orig, "_ov_extract_patch", False):
        return True
    if not _state["loop_applied"]:
        logger.warning(
            "ov-extract-patch: NOT applied to session retry — the ExtractLoop half was not "
            "applied in this process (guard failed or extract_loop not imported first); "
            "stock retry budget kept"
        )
        return False
    if (
        version != EXPECTED_VERSION
        or orig is None
        or not all(hasattr(module, n) for n in needed)
    ):
        logger.warning(
            "ov-extract-patch: NOT applied to session retry (version=%r classifier=%r constants=%s)",
            version,
            orig is not None,
            [hasattr(module, n) for n in needed],
        )
        return False
    retries, base_delay, max_delay = retry_budget()
    module.is_retryable_api_error = wrap_is_retryable(orig)
    module._MEMORY_EXTRACTION_MAX_RETRIES = retries
    module._MEMORY_EXTRACTION_RETRY_BASE_DELAY_SECONDS = base_delay
    module._MEMORY_EXTRACTION_RETRY_MAX_DELAY_SECONDS = max_delay
    logger.warning(
        "ov-extract-patch: applied to session Phase 2 retry (retries=%d base=%.0fs max=%.0fs, pid %d)",
        retries,
        base_delay,
        max_delay,
        os.getpid(),
    )
    # BUG-1196 halves: each guarded on its own source hash; one failing leaves the rest.
    _apply_retry_wrapper(module)
    _apply_chunking(module)
    _apply_parallel_batches(module)
    return True


def _apply_parallel_batches(module) -> bool:
    cls = getattr(module, "Session", None)
    orig = getattr(cls, "_extract_long_term_memories_with_batching", None)
    plan = getattr(module, "plan_extraction_batches", None)
    not_found = getattr(module, "_is_storage_not_found", None)
    if getattr(orig, "_ov_extract_patch", False):
        return True
    digest = _source_hash(orig) if orig else None
    if digest != EXPECTED_BATCHING_SHA256 or plan is None or not_found is None:
        logger.warning(
            "ov-extract-patch: NOT applied to parallel Phase 2 batches (source_sha256=%s "
            "plan=%r not_found=%r); stock sequential batches kept",
            digest,
            plan is not None,
            not_found is not None,
        )
        return False
    cls._extract_long_term_memories_with_batching = wrap_batching(orig, plan, not_found)
    logger.warning(
        "ov-extract-patch: applied parallel Phase 2 batches (parallel=%d, pid %d)",
        batch_parallelism(),
        os.getpid(),
    )
    return True


def apply_compressor(module) -> bool:
    """Route the compressor's final memory_diff write through a batch collector."""
    if not _enabled():
        return False
    import openviking

    version = getattr(openviking, "__version__", None)
    cls = getattr(module, "SessionCompressorV3", None)
    targets = (
        (
            "_write_final_memory_diff",
            EXPECTED_WRITE_FINAL_DIFF_SHA256,
            wrap_write_final_memory_diff,
        ),
        (
            "_write_training_case_memory",
            EXPECTED_TRAINING_CASE_SHA256,
            wrap_training_case_write,
        ),
    )
    current = [getattr(cls, name, None) for name, _, _ in targets]
    if all(getattr(fn, "_ov_extract_patch", False) for fn in current):
        _state["compressor_applied"] = True
        return True
    digests = [_source_hash(fn) if fn else None for fn in current]
    if version != EXPECTED_VERSION or digests != [t[1] for t in targets]:
        logger.warning(
            "ov-extract-patch: NOT applied to SessionCompressorV3 (version=%r "
            "source_sha256=%s); Phase 2 batches stay sequential",
            version,
            digests,
        )
        return False
    for (name, _, wrap), fn in zip(targets, current, strict=True):
        setattr(cls, name, wrap(fn))
    _state["compressor_applied"] = True
    logger.warning(
        "ov-extract-patch: applied to SessionCompressorV3 final diff write and "
        "training case write (pid %d)",
        os.getpid(),
    )
    return True


def _apply_submit_lock(cls, version) -> bool:
    """IMPR-1303 lever 2: the per-archive submit lock, guarded on its own hash."""
    orig = getattr(cls, "submit", None)
    if getattr(orig, "_ov_extract_patch", False):
        _state["submit_lock_applied"] = True
        return True
    digest = _source_hash(orig) if orig else None
    if version != EXPECTED_VERSION or digest != EXPECTED_SUBMIT_SHA256:
        logger.warning(
            "ov-extract-patch: NOT applied to StreamingMemoryUpdater.submit "
            "(version=%r source_sha256=%s); Phase 2 batches stay sequential",
            version,
            digest,
        )
        return False
    cls.submit = wrap_submit(orig)
    _state["submit_lock_applied"] = True
    logger.warning(
        "ov-extract-patch: applied to StreamingMemoryUpdater.submit (pid %d)",
        os.getpid(),
    )
    return True


def apply_streaming(module) -> bool:
    """Install the write-tracking wrappers in ``streaming_memory_updater``; all or none.

    Also installs the lever 2 submit lock first, on its own guard.

    Without them no lock wait is tagged, so the retry wrapper leaves every lock wait to
    the stock classifier.
    """
    if not _enabled():
        return False
    import openviking

    version = getattr(openviking, "__version__", None)
    cls = getattr(module, "StreamingMemoryUpdater", None)
    _apply_submit_lock(cls, version)
    targets = (
        (
            cls,
            "_apply_append_only_request_now",
            EXPECTED_APPEND_ONLY_SHA256,
            wrap_append_only,
        ),
        (
            cls,
            "_submit_grouped_merge_request",
            EXPECTED_GROUPED_MERGE_SHA256,
            wrap_grouped_merge,
        ),
        (
            module,
            "_acquire_stable_operation_lease",
            EXPECTED_ACQUIRE_LEASE_SHA256,
            wrap_acquire_lease,
        ),
    )
    current = [getattr(owner, name, None) for owner, name, _, _ in targets]
    if all(getattr(fn, "_ov_extract_patch", False) for fn in current):
        return True
    digests = [_source_hash(fn) if fn else None for fn in current]
    if version != EXPECTED_VERSION or digests != [t[2] for t in targets]:
        logger.warning(
            "ov-extract-patch: NOT applied to StreamingMemoryUpdater write tracking "
            "(version=%r source_sha256=%s); lock waits keep stock retry behaviour",
            version,
            digests,
        )
        return False
    for (owner, name, _, wrap), fn in zip(targets, current, strict=True):
        setattr(owner, name, wrap(fn))
    logger.warning(
        "ov-extract-patch: applied StreamingMemoryUpdater write tracking (pid %d)",
        os.getpid(),
    )
    return True


# module name -> apply function, consumed by sitecustomize.py
TARGETS = {
    "openviking.session.memory.extract_loop": apply_extract_loop,
    "openviking.session.session": apply_session,
    "openviking.session.compressor_v3": apply_compressor,
    "openviking.session.memory.streaming_memory_updater": apply_streaming,
}
