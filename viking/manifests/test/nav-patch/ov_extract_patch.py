"""Degraded memory extraction gets the Phase 2 retries before it fails the archive (BUG-1176).

OpenViking v0.4.20's ``ExtractLoop.run`` returned an exhausted format retry (an empty,
unparseable or refusal completion) as an errors-only ``ResolvedOperations``, and the
archive got ``.done`` with an empty diff: a saturated VLM (the 2026-09-22 BUG-1173
backfill) produced three "successful" commits with nothing in them. v0.5.1.1 fixed the
silent half upstream (#5171): ``run`` raises ``RuntimeError("Final response could not be
parsed as operations after N iterations (failure_kind=K)")`` (extract_loop.py:440-444),
the commit path runs with ``strict_extract_errors=True`` (session.py:2362), and the outer
handler writes ``.failed.json`` with stage ``memory_extraction`` (session.py:2588-2603).
It is not retried, though: no transient pattern matches that text (model_retry.py:175-188),
so one exhausted loop fails the archive for good (later commits never re-cover it,
session.py:2944-2962).

What this wrapper still does on v0.5.1.1, inside the existing Phase 2 machinery:

  * ``ExtractLoop.run`` — when the *session extraction* loop (context provider exactly
    ``SessionExtractContextProvider``) raises that parse-exhaustion ``RuntimeError``
    before its attempt has written anything, re-raise it as ``ExtractionDegradedError``
    (chained, same text). The streaming memory updater's merge loops
    (``PatchMergeContextProvider``, ``max_iterations=1``) keep the stock raise, which is
    not retried: they run after append-only events have already been written, and a
    whole-step retry there would regenerate and duplicate those events.
  * ``openviking.session.session`` — ``is_retryable_api_error`` also returns True for
    ``ExtractionDegradedError``, and the Phase 2 retry constants are widened from
    3 retries × (1 s … 8 s) to 5 retries × (15 s … 120 s) (session.py:105-107):
    ``retry_async`` makes up to 6 attempts with 5 capped, jittered sleeps (about
    345–420 s of sleeping in all, no overall deadline), plus the extraction's own LLM time
    per attempt. Both are module globals the nested retry helper reads at call time. This
    half applies only after the loop half applied in the same process, so the guard
    governs both. When every retry fails, the archive still gets upstream's
    ``.failed.json``.

Dropped in the v0.5.1.1 port: raising on an errors-only *returned* result, and logging a
degraded merge loop. Upstream now raises for both instead of returning, and
``resolve_operations`` never fills ``errors`` (extract_loop.py:765-971), so neither path
is reachable.

Also logs, at WARNING, an extraction that parsed fine but produced zero operations, with the
loop's last failure kind, so a future zero window is diagnosable from the pod log alone.
That parsed-empty outcome is indistinguishable from "nothing to extract" and is NOT raised.

BUG-1196 adds three things, each with its own guard and off switch (the session half,
plus a streaming half that tracks writes for the lock-wait retry):

  * Lock waits are retried only when nothing from the step's attempt has been written,
    on their own small budget. v0.5.1.1 added "timed out" to the transient patterns
    (#6102, model_retry.py:186), so stock now retries every ``LockAcquisitionError``
    ("lock acquire timed out after {elapsed_ms}ms") by re-running the whole step: a
    fresh LLM extraction with a new ``source_extraction_id`` (session.py:2350-2371,
    compressor_v3.py:749) after append-only events and merge groups may have landed
    (streaming_memory_updater.py:186-196, 457-462), so the retry files its re-extracted
    events, under new LLM-chosen names, beside the ones already written.
    ``session.retry_async`` (the name ``_run_retryable_phase2_step`` looks up at call
    time) is wrapped with a per-call classifier that takes lock waits away from the
    caller's classifier: one is retried only when the ``StreamingMemoryUpdater``
    append-only fast path failed to take its lease and it was the attempt's first write
    after session extraction (the streaming half tags it): the lease
    (``acquire_memory_operation_lease``) is taken before any write, and the fast path runs
    before the merge groups. Every other lock wait fails the step, as on v0.4.20. Timeouts
    keep the caller's classifier, which now retries them itself; a session-extraction
    timeout that it refuses (the loop half tags those raised before the attempt wrote
    anything) gets its own small budget, a fallback that v0.5.1.1's classifier leaves
    almost unused. Both stay under the overall ``_MEMORY_EXTRACTION_MAX_RETRIES`` cap.
    Budgets: ``OV_EXTRACT_PATCH_TIMEOUT_RETRIES`` (default 2) and
    ``OV_EXTRACT_PATCH_LOCK_RETRIES`` (default 3); ``0`` turns the timeout fallback off
    and stops every lock-wait retry.
  * Once an attempt has written anything (the streaming half marks the append-only fast
    path and the merge groups before they run), no failure of that attempt is retried,
    whatever its class: a lock wait, a builtin ``TimeoutError``, a ``ModelError`` or SDK
    5xx/429, anything. v0.5.1.1's ``retry_async`` retries recognised model errors
    without asking the classifier (``_retry_limit``, model_retry.py:432-451), so the
    wrapper hands such a failure to it as an opaque, unretryable ``_AttemptWrote`` and
    re-raises the original once it gives up. The step fails and Phase 2 writes
    ``.failed.json`` (stage ``memory_extraction``); v0.4.20 did not retry these either.
    Failures before any write keep stock retries plus the budgets above.
  * Every retry, recovery and final failure of a Phase 2 step writes one unwrapped
    ``ov-extract-outcome {json}`` line on stderr naming the step, attempt and
    ``failure_class`` (``timeout``, ``lock_wait``, ``degraded`` or ``other``), so a lock
    wait reads differently from a model timeout in Loki. A retry line is written when the
    next attempt starts, because v0.5.1.1's ``retry_async`` retries a recognised model
    error without asking the classifier (``_retry_limit``, model_retry.py:432-451). Not
    through ``logger``: the rich handler folds long records into several Loki entries
    (see ov_memory_guard_patch).
  * Long-term extraction is chunked. Upstream plans Phase 2 extraction batches, but only
    from a session's ``auto_commit_policy``, and Claude Code sessions carry none, so a
    180-message archive went to the model as one prompt. The session module's
    ``resolve_extraction_batch_limits`` is wrapped to cap every session at
    ``OV_EXTRACT_PATCH_BATCH_MESSAGES`` (default 60) messages and
    ``OV_EXTRACT_PATCH_BATCH_TOKENS`` (default 24000) estimated tokens per batch, keeping a
    policy's tighter limit; ``0`` drops that cap and ``OV_EXTRACT_PATCH_CHUNK=0`` turns
    chunking off. Upstream records and retries each batch as its own step
    (session.py:2041-2079), so a failure redoes one batch, not the archive. The same
    limits batch the Working Memory summary.

Guarded: applies only to openviking ``v0.5.1.1`` whose ``ExtractLoop.run`` source hashes to
``EXPECTED_RUN_SHA256``; otherwise it logs "NOT applied" and stock behaviour stays. The
retry and chunking wrappers, and the streaming half's three write-tracking wrappers
(``openviking.session.memory.streaming_memory_updater``), are each also guarded on the
source hash of the function they wrap. Without the streaming half no lock wait is ever
tagged, so no lock wait is retried. Disable everything at runtime with
``OV_EXTRACT_PATCH=0``. Retry budget overrides: ``OV_EXTRACT_PATCH_RETRIES``,
``OV_EXTRACT_PATCH_BASE_DELAY``, ``OV_EXTRACT_PATCH_MAX_DELAY``.
"""

from __future__ import annotations

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

EXPECTED_VERSION = "v0.5.1.1"
EXPECTED_RUN_SHA256 = "48d4bc4f14b03b0a157402779f5e73aad1ef58465df582e173ab34d7347076bb"
# BUG-1196: openviking.utils.model_retry.retry_async and
# openviking.session.extraction_batch.resolve_extraction_batch_limits on v0.5.1.1.
EXPECTED_RETRY_ASYNC_SHA256 = (
    "b0fc3cf82a2e6115928ad85f3a24b139791f48aaa66e0641bc6309e3a9e2fecc"
)
EXPECTED_RESOLVE_LIMITS_SHA256 = (
    "811fff5754eee8fed3fdfde1c4c9750901249ef3c66041032f6082959c47b9b1"
)
# openviking.session.memory.streaming_memory_updater on v0.5.1.1: the append-only fast
# path, the merge-group submit, and the module-level lease acquisition
# (``acquire_memory_operation_lease``, renamed from ``_acquire_stable_operation_lease``
# in #5178).
EXPECTED_APPEND_ONLY_SHA256 = (
    "3e24e1c676c8bd718109d206c94e1b155c3d99af1ed367398efc4acba94cc58f"
)
EXPECTED_GROUPED_MERGE_SHA256 = (
    "f33842c5447652ad21db1d6763bfcec54f41e4521a5d96534a93b09388061af8"
)
EXPECTED_ACQUIRE_LEASE_SHA256 = (
    "ca2c9480fd785fca2bf500789854e2369661137fd78c9be419d474b715196843"
)
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

logger = logging.getLogger("ov_extract_patch")
SESSION_PROVIDER = (
    "SessionExtractContextProvider"  # exact class; subclasses are merge loops
)
_state = {"loop_applied": False}

_LOCK_WAIT_TEXT = re.compile(
    r"lock acquir\w* timed out|pathlock\S* .*timed out", re.IGNORECASE
)
_TIMEOUT_TEXT = re.compile(r"\btimed out\b|\btimeout\b", re.IGNORECASE)
# ExtractLoop.run's raise once its format retry is spent (extract_loop.py:440-444).
_PARSE_EXHAUSTED_TEXT = re.compile(
    r"^Final response could not be parsed as operations\b"
)


class ExtractionDegradedError(RuntimeError):
    """The extraction LLM produced no usable operations after the loop's own retry."""


class _AttemptWrote(Exception):
    """Carries a failure of an attempt that wrote, past upstream's retry loop.

    Plain ``Exception`` with no cause or context, so upstream's model-error detection
    (``isinstance(TimeoutError)``, SDK modules, the exception chain) never matches it.
    """

    def __init__(self, error: BaseException):
        super().__init__("phase 2 attempt failed after writing")
        self.error = error


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
    """Wrap ``ExtractLoop.run``: session parse exhaustion becomes retryable; 0 ops logged."""

    @functools.wraps(orig)
    async def run(self):
        provider = type(getattr(self, "context_provider", None)).__name__
        try:
            operations, tools_used = await orig(self)
        except Exception as exc:
            # Session extraction runs before anything from this batch is written, so a
            # Phase 2 retry of its failures repeats no write. Not so once the attempt
            # wrote, nor for merge loops, which run after the append-only events.
            if provider != SESSION_PROVIDER or _step_wrote():
                raise
            if isinstance(exc, RuntimeError) and _PARSE_EXHAUSTED_TEXT.search(str(exc)):
                # BUG-1176: upstream fails the archive on the first exhausted loop; a
                # saturated VLM gets the Phase 2 retries first.
                logger.warning(
                    "ov-extract-patch: extraction degraded (%s); raising for Phase 2 retry",
                    str(exc)[:200],
                )
                raise ExtractionDegradedError(
                    f"memory extraction degraded: {str(exc)[:300]}"
                ) from exc
            # BUG-1196: the session-half classifier gives its fallback budget only to
            # tagged timeouts.
            if failure_class(exc) == FAILURE_TIMEOUT:
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
        kind = getattr(self, "_last_llm_failure_kind", None) or "unknown"
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


# --- session half: retry classifier (BUG-1196) -----------------------------------------


def wrap_retry_async(orig, default_is_retryable):
    """Wrap ``retry_async`` with per-class budgets and one outcome line per event.

    Once an attempt has written anything (the streaming half's write tracking), no
    failure of it is retried, whatever its class: a retry is a fresh LLM extraction that
    files its events again under new names. v0.5.1.1's ``retry_async`` retries a
    recognised model error (builtin ``TimeoutError``, ``ModelError``, SDK 5xx/429)
    without asking ``is_retryable`` (``_retry_limit``, model_retry.py:432-451), so such a
    failure reaches it wrapped in ``_AttemptWrote``, which carries no exception chain for
    ``model_error_from_exception`` to walk and which the classifier refuses; the original
    error is re-raised after it gives up.

    Before any write, ``retry_async`` asks ``is_retryable`` on every failure that is not
    a recognised model error, the final one included, so a budget is spent only while a
    retry can follow, and the retry line is written when the next attempt starts. Lock
    waits are decided here alone: retried, on the lock budget, only when tagged as
    raised before the attempt wrote anything. For every other error the caller's
    classifier is asked first, so its retries are never reduced; the timeout budget only
    adds retries for a tagged session-extraction timeout it refused.
    """

    @functools.wraps(orig)
    async def retry_async(func, *, max_retries, is_retryable=None, **kwargs):
        base = is_retryable or default_is_retryable
        operation = kwargs.get("operation_name", "operation")
        budget = class_budget()
        used = {FAILURE_TIMEOUT: 0, FAILURE_LOCK_WAIT: 0}
        attempts = 0
        last_error: BaseException | None = None

        def spend(cls):
            if used[cls] >= budget[cls]:
                return False
            used[cls] += 1
            return True

        def decide(error):
            if isinstance(error, _AttemptWrote):
                return False
            if attempts > max_retries:
                return False  # the final failure: no retry follows whatever we answer
            cls = failure_class(error)
            if cls == FAILURE_LOCK_WAIT:
                # Stock retries "timed out" (#6102), which would re-extract after the
                # attempt's events may have landed and file them a second time.
                return is_before_write_lock_wait(error) and spend(cls)
            if base(error):
                return True
            return is_session_extract_timeout(error) and spend(cls)

        async def counted():
            nonlocal attempts, last_error
            if last_error is not None:
                emit_outcome(
                    {
                        "event": "retry",
                        "step": operation,
                        "attempt": attempts,
                        "failure_class": failure_class(last_error),
                        "error": str(last_error)[:300],
                    }
                )
            attempts += 1
            state = {"armed": False, "wrote": False}
            token = _STEP.set(state)
            try:
                return await func()
            except Exception as exc:
                last_error = exc
                if not state["wrote"]:
                    raise
                wrote_error = exc
            finally:
                _STEP.reset(token)
            # Raised outside the handler so it has no __context__ leading back to exc.
            raise _AttemptWrote(wrote_error)

        failure: BaseException | None = None
        after_write = False
        try:
            result = await orig(
                counted, max_retries=max_retries, is_retryable=decide, **kwargs
            )
        except _AttemptWrote as stopped:
            failure, after_write = stopped.error, True
        except Exception as exc:
            failure = exc
        if failure is not None:
            record = {
                "event": "failed",
                "step": operation,
                "attempts": attempts,
                "failure_class": failure_class(failure),
                "error": str(failure)[:300],
            }
            if after_write:
                record["after_write"] = True
            emit_outcome(record)
            raise failure
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


# --- streaming half: write tracking (BUG-1196) -----------------------------------------


def wrap_acquire_lease(orig):
    """Mark any failure raised while taking a memory-apply lease (before its writes).

    Whatever its class (a lock wait, a ``TimeoutError`` from the pathlock call), it
    precedes every write the lease covers.
    """

    @functools.wraps(orig)
    async def acquire_memory_operation_lease(*args, **kwargs):
        try:
            return await orig(*args, **kwargs)
        except Exception as exc:
            _tag(exc, LEASE_ATTR, True)
            raise

    acquire_memory_operation_lease._ov_extract_patch = True  # type: ignore[attr-defined]
    return acquire_memory_operation_lease


def wrap_append_only(orig):
    """Tag a lease failure in the attempt's first write; otherwise mark the attempt written.

    Runs in the submitter's own task, first in ``submit``. A failure is tagged
    ``before_write`` (and the attempt left unwritten) only when session extraction armed
    this attempt, nothing has been written since, and it came from taking the lease
    (which precedes every write of the fast path). The lock budget applies to tagged
    lock waits; any other tagged failure keeps the caller's retries.
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
            "stock retry kept, which retries lock waits after writes",
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
    return True


def apply_streaming(module) -> bool:
    """Install the write-tracking wrappers in ``streaming_memory_updater``; all or none.

    Without them no lock wait is tagged, so the retry wrapper retries none.
    """
    if not _enabled():
        return False
    import openviking

    version = getattr(openviking, "__version__", None)
    cls = getattr(module, "StreamingMemoryUpdater", None)
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
            "acquire_memory_operation_lease",
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
            "(version=%r source_sha256=%s); no lock wait is retried",
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
    "openviking.session.memory.streaming_memory_updater": apply_streaming,
}
