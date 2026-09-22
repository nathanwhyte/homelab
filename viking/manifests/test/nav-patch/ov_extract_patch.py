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

  * ``ExtractLoop.run`` — when the loop returns errors and no upserts, raise
    ``ExtractionDegradedError`` instead of returning. ``extract_long_term_memories`` runs with
    ``strict_extract_errors=True`` in the commit path, so the error reaches
    ``_run_retryable_phase2_step`` and is retried by ``retry_async``; if every retry fails the
    archive gets ``.failed.json`` (stage ``memory_extraction``) and the task fails — visible,
    exact, and re-drivable — instead of an empty diff recorded as success.
  * ``openviking.session.session`` — ``is_retryable_api_error`` also returns True for
    ``ExtractionDegradedError``, and the Phase 2 retry constants are widened from
    3 × (1 s … 8 s) to 5 × (15 s … 120 s) so a short VLM contention window is ridden out.
    Both are module globals the nested retry helper reads at call time.

Also logs, at WARNING, an extraction that parsed fine but produced zero operations, with the
loop's last failure kind, so a future zero window is diagnosable from the pod log alone.

Guarded: applies only to openviking ``v0.4.20`` whose ``ExtractLoop.run`` source hashes to
``EXPECTED_RUN_SHA256``; otherwise it logs "NOT applied" and stock behaviour stays.
Disable at runtime with ``OV_EXTRACT_PATCH=0``. Retry budget overrides:
``OV_EXTRACT_PATCH_RETRIES``, ``OV_EXTRACT_PATCH_BASE_DELAY``, ``OV_EXTRACT_PATCH_MAX_DELAY``.
"""

from __future__ import annotations

import functools
import hashlib
import inspect
import logging
import os

EXPECTED_VERSION = "v0.4.20"
EXPECTED_RUN_SHA256 = "f9bf435228ca4a82200442f47752a0f4b7e2213a0c86dcb08a6a773357156876"
DEFAULT_RETRIES = 5
DEFAULT_BASE_DELAY_SECONDS = 15.0
DEFAULT_MAX_DELAY_SECONDS = 120.0

logger = logging.getLogger("ov_extract_patch")


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


def wrap_run(orig):
    """Wrap ``ExtractLoop.run``: errors-only result raises; zero-op result is logged."""

    @functools.wraps(orig)
    async def run(self):
        operations, tools_used = await orig(self)
        if operations is None:
            return operations, tools_used
        upserts = getattr(operations, "upsert_operations", None) or []
        errors = list(getattr(operations, "errors", None) or [])
        kind = getattr(self, "_last_llm_failure_kind", None) or "unknown"
        if errors and not upserts:
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


def retry_budget() -> tuple[int, float, float]:
    def _num(name, default, cast):
        raw = os.environ.get(name)
        if raw is None or not raw.strip():
            return default
        try:
            value = cast(raw)
        except ValueError:
            logger.warning("ov-extract-patch: ignoring %s=%r", name, raw)
            return default
        return value if value > 0 else default

    return (
        _num("OV_EXTRACT_PATCH_RETRIES", DEFAULT_RETRIES, int),
        _num("OV_EXTRACT_PATCH_BASE_DELAY", DEFAULT_BASE_DELAY_SECONDS, float),
        _num("OV_EXTRACT_PATCH_MAX_DELAY", DEFAULT_MAX_DELAY_SECONDS, float),
    )


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
    logger.warning("ov-extract-patch: applied to ExtractLoop.run (pid %d)", os.getpid())
    return True


def apply_session(module) -> bool:
    """Patch the retry classifier and constants in ``openviking.session.session``."""
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
    return True


# module name -> apply function, consumed by sitecustomize.py
TARGETS = {
    "openviking.session.memory.extract_loop": apply_extract_loop,
    "openviking.session.session": apply_session,
}
