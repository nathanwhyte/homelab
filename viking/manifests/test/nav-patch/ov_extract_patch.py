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

BUG-1196 adds three things to the session half, each with its own guard and off switch:

  * Timeouts and lock waits are retried, each on its own small budget. Stock
    ``is_retryable_api_error`` matches the text "timeout", not "timed out", so
    ``openai.APITimeoutError`` ("Request timed out.") and ``LockAcquisitionError`` ("lock
    acquire timed out after 300000ms") both failed the archive on the first attempt.
    ``session.retry_async`` (the name ``_run_retryable_phase2_step`` looks up at call
    time) is wrapped with a per-call classifier. A timeout is retried only when the
    session-extraction ``ExtractLoop.run`` raised it (the loop half tags it): that loop
    runs before anything from the batch is written, so repeating it cannot duplicate
    events. A timeout from anywhere else keeps stock behaviour. A lock wait is retried on
    its own budget; it is raised while memories are being applied, so a retry re-runs the
    batch's extraction against files a sibling group may already have written (the same
    exposure upstream accepts for its own transient retries; the memory guard drops exact
    ``add_only`` duplicates). Both stay under the overall ``_MEMORY_EXTRACTION_MAX_RETRIES``
    cap. Budgets: ``OV_EXTRACT_PATCH_TIMEOUT_RETRIES`` (default 2) and
    ``OV_EXTRACT_PATCH_LOCK_RETRIES`` (default 3); ``0`` turns that class off.
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

Guarded: applies only to openviking ``v0.4.20`` whose ``ExtractLoop.run`` source hashes to
``EXPECTED_RUN_SHA256``; otherwise it logs "NOT applied" and stock behaviour stays. The
retry and chunking wrappers are each also guarded on the source hash of the function they
wrap. Disable everything at runtime with ``OV_EXTRACT_PATCH=0``. Retry budget overrides:
``OV_EXTRACT_PATCH_RETRIES``, ``OV_EXTRACT_PATCH_BASE_DELAY``, ``OV_EXTRACT_PATCH_MAX_DELAY``.
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import inspect
import json
import logging
import os
import re
import sys

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

logger = logging.getLogger("ov_extract_patch")
SESSION_PROVIDER = (
    "SessionExtractContextProvider"  # exact class; subclasses are merge loops
)
_state = {"loop_applied": False}

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


def is_session_extract_timeout(error: BaseException) -> bool:
    """True for a timeout the loop half tagged as raised by session extraction."""
    return failure_class(error) == FAILURE_TIMEOUT and any(
        getattr(e, STAGE_ATTR, None) == STAGE_SESSION_EXTRACT for e in _chain(error)
    )


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
            # written, so its timeouts are safe for the Phase 2 retry to repeat. Tag them;
            # the session-half classifier retries only tagged timeouts.
            if provider == SESSION_PROVIDER and failure_class(exc) == FAILURE_TIMEOUT:
                try:
                    setattr(exc, STAGE_ATTR, STAGE_SESSION_EXTRACT)
                except (AttributeError, TypeError):
                    pass
            raise
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


# --- session half: retry classifier (BUG-1196) -----------------------------------------


def wrap_retry_async(orig, default_is_retryable):
    """Wrap ``retry_async`` with per-class budgets and one outcome line per event.

    ``retry_async`` asks ``is_retryable`` only while attempts remain, so each True
    returned here is one retry actually taken. Timeouts not tagged by the loop half, and
    every other class, defer to the caller's classifier (stock behaviour).
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
            if cls == FAILURE_LOCK_WAIT or (
                cls == FAILURE_TIMEOUT and is_session_extract_timeout(error)
            ):
                allowed = used[cls] < budget[cls]
                if allowed:
                    used[cls] += 1
            else:
                allowed = bool(base(error))
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
            return await func()

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
    return True


# module name -> apply function, consumed by sitecustomize.py
TARGETS = {
    "openviking.session.memory.extract_loop": apply_extract_loop,
    "openviking.session.session": apply_session,
}
