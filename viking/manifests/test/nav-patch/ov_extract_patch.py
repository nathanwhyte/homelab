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
  * Every retry, recovery and final failure inside the wrapped ``retry_async`` writes one
    unwrapped ``ov-extract-outcome {json}`` line. On v0.5.1.1 only the ``long_term`` step
    runs through it (session.py:2271,2287), so ``archive_summary`` and an exception outside
    the step gather reach Phase 2's failure handler with no such line. It goes to stderr,
    naming the step, attempt and
    ``failure_class`` (``timeout``, ``lock_wait``, ``degraded`` or ``other``), so a lock
    wait reads differently from a model timeout in Loki or ``kubectl logs`` (Loki lost one
    such line, BUG-1279). A retry line is written when the
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

BUG-1237 adds one more: ``ExtractLoop`` passes raw ``memory_fields`` to the resolution
repair prompt, and a field the model changed with ``obj.field.edit(...)`` holds a pydantic
``StrPatch``. Both protocols render that prompt with a bare ``json.dumps``, so the
``TypeError`` failed the archive before the repair round could run.
``_build_resolution_repair_instruction`` now hands the renderer ``json_safe`` input, which
covers both protocols. ``_build_patch_repair_instruction`` gets the same wrapper as a
defence only: its ``patch_errors`` hold JSON-native values on v0.5.1.1. Upstream's default
branch still had the bare call on 2026-10-30.

IMPR-1453 adds two counters: ``openviking_session_archive_zero_memories_total{reason}``,
archives that finish with zero memories, and ``openviking_session_archive_measured_total``,
every archive that was measured, zero or not (the rate's denominator). Stock
``openviking_memory_extracted_total`` only increments above zero, and the 0-operations
WARNING above also fires for merge loops (327 lines against 89 zero-result archives in one
day), so neither can count archives. Both are counted in ``Session._write_done_file``
(session.py:2559), Phase 2's last step: the metadata save and ``tracker.complete`` run
before it, so a failure in either leaves ``.failed.json`` and no count, and an archive that
already has ``.done`` is not counted again. Only an archive this process ran whole is
measured: a wrapper around ``Session._run_memory_extraction`` reads ``.meta.json`` before
the run, and if any ``completed_memory_steps`` entry is already complete (a recovery, which
cannot see what the finished steps filed), a ``memory_diff.json`` or ``.failed.json``
already exists (the stock code writes them before the step checkpoint, so a stop between
the memory write and the checkpoint leaves no completed step), the start state cannot be
read, or
``_merge_and_save_commit_meta`` never reported this run's ``memories_extracted`` total, the
archive is left out of both counters and one "not measured" line is logged. Nothing new is
persisted in the archive. For a measured archive, ``reason="empty"`` means a
session-extraction loop ran (``wrap_run`` counts them) and filed nothing, ``reason="skipped"``
that none ran (extraction disabled, the preference gate left no type to extract, or no
message was left to extract); an archive whose only output was skipped operations counts as
``empty``, since no memory operation was applied. The 7-day rate of real zero results is
the ``empty`` series over ``measured``. No alert yet: a baseline comes first. Each wrapper
is hash-guarded and installed by ``apply_session`` after the loop half.
The three series are created at 0 as soon as the server builds its metrics registry
(``init_metrics_from_server_config`` is wrapped; the registry does not exist at import), and
at the first run start or count if that hook found none, so ``increase()`` sees the first
event. A missing registry fails open. One window remains: stock ``extract_long_term_memories``
writes ``memory_diff.json`` unconditionally, zero operations included (compressor_v3.py:513),
so only a stop, or a retryable error repeated inside one measured run, before that line
leaves no durable trace; a retry after a stop can be measured.
The ``.failed.json`` check guards a direct Phase 2 re-run only: ``resume_queued_commit``
returns on an existing ``.failed.json`` (session.py:1872-1886).

Guarded: applies only to openviking ``v0.5.1.1`` whose ``ExtractLoop.run`` source hashes to
``EXPECTED_RUN_SHA256``; otherwise it logs "NOT applied" and stock behaviour stays. The
retry and chunking wrappers, the two repair-prompt wrappers, and the streaming half's three
write-tracking wrappers (``openviking.session.memory.streaming_memory_updater``), are each
also guarded on the source hash of the function they wrap. Without the streaming half no lock wait is ever
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
# BUG-1237: the two repair-prompt builders on ExtractLoop on v0.5.1.1.
EXPECTED_RESOLUTION_REPAIR_SHA256 = (
    "1214a25e919bdd08a7ec4815d187a35ee76a51f16151785457f285d2a17d4692"
)
EXPECTED_PATCH_REPAIR_SHA256 = (
    "7c1c20a49f8b8988bbfa0edbae284a9415d937024afebdc8c9aeb92ec040c782"
)
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
# IMPR-1453: ``Session._run_memory_extraction``, ``_merge_and_save_commit_meta`` and
# ``_write_done_file`` on v0.5.1.1 (session.py:2091, 2965, 2605).
EXPECTED_RUN_EXTRACTION_SHA256 = (
    "d64ddfa93c3d0d33c665f372c746f22f9bca9f670c7c4542d4e68f3e1527e9d2"
)
EXPECTED_MERGE_META_SHA256 = (
    "9e560862c4b9c1a9255bfc027e789c70c8817d23ad64f6b3873d5da93742f50d"
)
EXPECTED_INIT_METRICS_SHA256 = (
    "d2619abf6acd1309f5f4906310d389d6074197add3788c5f057f6cf1d59d9243"
)
EXPECTED_WRITE_DONE_SHA256 = (
    "457fcfba31bb4d1608980343e3fbb3a47edb59beb595b46f915f29dc97d399bc"
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

# Per-archive state for the zero-result counter (IMPR-1453), set around each
# ``Session._run_memory_extraction``: ``loops`` counts session-extraction loops that
# returned. Loops run in gathered tasks that copy the context and share the dict.
_ARCHIVE: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "ov_extract_archive", default=None
)

ZERO_RESULT_METRIC = "openviking_session_archive_zero_memories_total"
MEASURED_METRIC = "openviking_session_archive_measured_total"  # no labels
ZERO_RESULT_LABELS = ("reason",)
ZERO_REASON_EMPTY = (
    "empty"  # a session-extraction loop ran and the archive got no memory
)
ZERO_REASON_SKIPPED = (
    "skipped"  # no loop ran: gate, disabled config, or nothing to extract
)

logger = logging.getLogger("ov_extract_patch")
SESSION_PROVIDER = (
    "SessionExtractContextProvider"  # exact class; subclasses are merge loops
)
_state = {"loop_applied": False, "series_registry": None}

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
            archive = _ARCHIVE.get()
            if archive is not None:
                archive["loops"] += 1
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


def json_safe(value):
    """Return a copy ``json.dumps`` accepts; a pydantic model becomes its JSON dump."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {
            key
            if key is None or isinstance(key, (str, int, float, bool))
            else str(key): json_safe(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [json_safe(item) for item in value]
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        # A repair prompt must never be what fails the archive.
        with contextlib.suppress(Exception):
            return json_safe(dump(mode="json"))
    return str(value)


def wrap_repair_instruction(orig):
    """Wrap a repair-prompt builder: its list reaches the renderer JSON-safe (BUG-1237)."""

    @functools.wraps(orig)
    def build(self, issues):
        return orig(self, json_safe(issues))

    build._ov_extract_patch = True  # type: ignore[attr-defined]
    return build


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


# --- zero-result archive counter (IMPR-1453) ----------------------------------------------


def _archive_uri_of(args, kwargs):
    uri = kwargs.get("archive_uri")
    if uri is None and len(args) > 1:
        uri = args[1]
    return uri


async def _started_fresh(session, archive_uri, is_not_found) -> tuple[bool | None, str]:
    """``(fresh, found)``: fresh is True when nothing shows an earlier attempt, False when
    something does (``found`` names it), None when the start state cannot be read.

    An earlier attempt shows as a completed step in ``.meta.json`` (persisted per finished
    step), or, because the stock code writes them before that checkpoint, a
    ``memory_diff.json`` (written by the extraction as each batch lands) or a
    ``.failed.json`` (Phase 2's failure marker). Existence only for the last two. A missing
    file is fresh; any other read or existence failure is unknown (``None``).
    """

    def missing(exc) -> bool:
        return isinstance(exc, FileNotFoundError) or (
            is_not_found is not None and is_not_found(exc)
        )

    base = archive_uri.rstrip("/")
    try:
        raw = await session._viking_fs.read_file(f"{base}/.meta.json", ctx=session.ctx)
        steps = (json.loads(raw or "{}") or {}).get("completed_memory_steps") or {}
        if any(steps.values()):
            return False, "a completed memory step"
    except Exception as exc:
        if not missing(exc):
            return None, ""
    for name in ("memory_diff.json", ".failed.json"):
        try:
            if await session._viking_fs.exists(f"{base}/{name}", ctx=session.ctx):
                return False, name
        except Exception as exc:
            if not missing(exc):
                return None, ""
    return True, ""


def ensure_series(registry=None) -> None:
    """Create the zero-result and measured series at 0 on the live registry, once.

    Stock registers a counter family on its first increment, so Prometheus would first see
    a series at 1 and ``increase()`` would lose that event. Never raises; a missing registry
    is a no-op and is retried at the next hook (metrics init, a run start, a count). Always
    called before the first increment on a registry, so it never resets a live series.
    """
    with contextlib.suppress(Exception):
        if registry is None:
            from openviking.metrics.global_api import try_get_metrics_registry

            registry = try_get_metrics_registry()
        if registry is None or _state["series_registry"] is registry:
            return
        for reason in (ZERO_REASON_EMPTY, ZERO_REASON_SKIPPED):
            registry.set_counter(
                ZERO_RESULT_METRIC,
                0,
                labels={"reason": reason},
                label_names=ZERO_RESULT_LABELS,
            )
        registry.set_counter(MEASURED_METRIC, 0)
        _state["series_registry"] = registry


def wrap_init_metrics(orig):
    """Create the series as soon as the server builds its metrics registry."""

    @functools.wraps(orig)
    def init_metrics_from_server_config(*args, **kwargs):
        result = orig(*args, **kwargs)
        ensure_series()
        return result

    init_metrics_from_server_config._ov_extract_patch = True  # type: ignore[attr-defined]
    return init_metrics_from_server_config


def apply_metrics(module) -> bool:
    """Wrap ``init_metrics_from_server_config``; the registry does not exist at import."""
    if not _enabled():
        return False
    import openviking

    orig = getattr(module, "init_metrics_from_server_config", None)
    if getattr(orig, "_ov_extract_patch", False):
        return True
    digest = _source_hash(orig) if orig else None
    if (
        getattr(openviking, "__version__", None) != EXPECTED_VERSION
        or digest != EXPECTED_INIT_METRICS_SHA256
    ):
        logger.warning(
            "ov-extract-patch: NOT applied to metrics init (source_sha256=%s); the "
            "zero-result series appear at their first increment",
            digest,
        )
        return False
    module.init_metrics_from_server_config = wrap_init_metrics(orig)
    ensure_series()  # a registry built before this import
    logger.warning("ov-extract-patch: applied metrics init hook (pid %d)", os.getpid())
    return True


def wrap_run_extraction(orig, is_not_found=None):
    """Open the per-run state around one Phase 2 run (swallows its own failures).

    The run is measured only if nothing showed an earlier attempt when it started (a
    completed memory step, a ``memory_diff.json`` or a ``.failed.json``): a recovery skips
    the finished steps and cannot report what they filed.
    """

    @functools.wraps(orig)
    async def _run_memory_extraction(self, *args, **kwargs):
        state = {"loops": 0, "memories": 0, "seen": False, "fresh": None, "found": ""}
        token = _ARCHIVE.set(state)
        try:
            ensure_series()
            uri = _archive_uri_of(args, kwargs)
            if uri:
                with contextlib.suppress(Exception):
                    state["fresh"], state["found"] = await _started_fresh(
                        self, uri, is_not_found
                    )
            return await orig(self, *args, **kwargs)
        finally:
            _ARCHIVE.reset(token)

    _run_memory_extraction._ov_extract_patch = True  # type: ignore[attr-defined]
    return _run_memory_extraction


def count_zero_result(archive_uri) -> None:
    """Count one measured archive just marked ``.done``; add a zero-result when it was one.

    Never raises. An archive this run did not see whole (recovered, unknown start, or the
    memory total never observed) is left out of both counters, with one log line.
    """
    # Metrics must never fail an archive.
    with contextlib.suppress(Exception):
        archive = _ARCHIVE.get()
        if archive is None:
            return
        if archive["fresh"] is not True or not archive["seen"]:
            why = (
                f"an earlier attempt left {archive['found']}"
                if archive["fresh"] is False
                else "start state unknown"
                if archive["fresh"] is None
                else "extraction result not observed"
            )
            logger.warning(
                "ov-extract-patch: archive %s not measured: %s", archive_uri, why
            )
            return
        from openviking.metrics.global_api import try_get_metrics_registry

        registry = try_get_metrics_registry()
        if registry is None:
            return
        ensure_series(registry)
        registry.inc_counter(MEASURED_METRIC)
        if archive["memories"] > 0:
            return
        registry.inc_counter(
            ZERO_RESULT_METRIC,
            labels={
                "reason": ZERO_REASON_EMPTY
                if archive["loops"] > 0
                else ZERO_REASON_SKIPPED
            },
            label_names=ZERO_RESULT_LABELS,
        )


def wrap_merge_meta(orig):
    """Record this run's memory total; counting waits for the durable ``.done``."""

    @functools.wraps(orig)
    async def _merge_and_save_commit_meta(
        self, archive_index, memories_extracted, *args, **kwargs
    ):
        result = await orig(self, archive_index, memories_extracted, *args, **kwargs)
        archive = _ARCHIVE.get()
        if archive is not None:
            with contextlib.suppress(Exception):
                archive["memories"] = sum((memories_extracted or {}).values())
                archive["seen"] = True
        return result

    _merge_and_save_commit_meta._ov_extract_patch = True  # type: ignore[attr-defined]
    return _merge_and_save_commit_meta


def wrap_write_done(orig):
    """Count once per measured archive when ``.done`` is durably written.

    ``_write_done_file`` is Phase 2's last step: ``_merge_and_save_commit_meta`` and
    ``tracker.complete`` run before it, so a failure in either leaves ``.failed.json``
    and no count. An existing ``.done`` is checked too, so one archive is counted once.
    """

    @functools.wraps(orig)
    async def _write_done_file(self, archive_uri, *args, **kwargs):
        already = False
        with contextlib.suppress(Exception):
            already = bool(
                await self._viking_fs.exists(
                    f"{archive_uri.rstrip('/')}/.done", ctx=self.ctx
                )
            )
        result = await orig(self, archive_uri, *args, **kwargs)
        if not already and getattr(self, "_viking_fs", None) is not None:
            count_zero_result(archive_uri)
        return result

    _write_done_file._ov_extract_patch = True  # type: ignore[attr-defined]
    return _write_done_file


def _apply_zero_result_counter(module) -> bool:
    """Wrap the three Session methods; all or none, each guarded on its source hash."""
    cls = getattr(module, "Session", None)
    targets = (
        (
            "_run_memory_extraction",
            EXPECTED_RUN_EXTRACTION_SHA256,
            lambda fn: wrap_run_extraction(
                fn, getattr(module, "_is_storage_not_found", None)
            ),
        ),
        ("_merge_and_save_commit_meta", EXPECTED_MERGE_META_SHA256, wrap_merge_meta),
        ("_write_done_file", EXPECTED_WRITE_DONE_SHA256, wrap_write_done),
    )
    current = [getattr(cls, name, None) for name, _, _ in targets]
    if current and all(getattr(fn, "_ov_extract_patch", False) for fn in current):
        return True
    digests = [_source_hash(fn) if fn else None for fn in current]
    if digests != [t[1] for t in targets]:
        logger.warning(
            "ov-extract-patch: NOT applied to the zero-result archive counter "
            "(source_sha256=%s); %s stays at zero",
            digests,
            ZERO_RESULT_METRIC,
        )
        return False
    for (name, _, wrap), fn in zip(targets, current, strict=True):
        setattr(cls, name, wrap(fn))
    logger.warning(
        "ov-extract-patch: applied zero-result archive counter (pid %d)", os.getpid()
    )
    return True


# --- installers -------------------------------------------------------------------------


def _apply_repair_wrappers(cls) -> None:
    """Wrap each repair-prompt builder whose source still hashes as expected."""
    for name, expected in (
        ("_build_resolution_repair_instruction", EXPECTED_RESOLUTION_REPAIR_SHA256),
        ("_build_patch_repair_instruction", EXPECTED_PATCH_REPAIR_SHA256),
    ):
        fn = getattr(cls, name, None)
        digest = _source_hash(fn) if fn else None
        if digest != expected:
            logger.warning(
                "ov-extract-patch: NOT applied to ExtractLoop.%s (source_sha256=%s); "
                "a StrPatch still fails that repair prompt",
                name,
                digest,
            )
            continue
        setattr(cls, name, wrap_repair_instruction(fn))


def apply_extract_loop(module) -> bool:
    """Patch ``module.ExtractLoop`` in place; True if ``run`` was patched."""
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
    _apply_repair_wrappers(cls)
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
    _apply_zero_result_counter(module)
    ensure_series()  # a no-op unless the registry already exists
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
    "openviking.metrics.global_api": apply_metrics,
}
