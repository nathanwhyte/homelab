"""Skip preference extraction for an archive with no typed user text on v0.4.20 (IMPR-1404).

``SessionCompressorV3._extract_user_memories`` sends every enabled ``stage: user`` schema
to the model in one call, ``preferences`` included, whatever the archive holds. The
capture plugin sends every turn of a subagent session as ``assistant`` (BUG-1215), so such
an archive reaches the extractor with no ``[user]`` line at all, and the homelab#248
``preferences`` override ("only from [user] messages") has nothing to apply to. All 13
false preferences flagged by ``ov-pref-audit.py`` up to 2026-10-21 came from archives like
that (BUG-1260), three of them written after the override went live.

This wrapper checks the archive's messages before extraction. When no ``user`` message
has a non-blank text part (the same test the extractor uses to render a line, so a
tool-result-only ``user`` turn does not count), it runs the extraction with
``preferences`` removed from ``allowed_memory_types``. That one argument is OpenViking's
own per-session ``memory_policy.memory_types`` seam: the isolation handler then leaves the
schema out of the prompt and the prefetch, and an operation the model still emits for it
is skipped with a code in ``memory_diff.json``'s ``skipped_operations``. Every other
memory type extracts as before. An archive with typed user text, or one whose policy
already excludes ``preferences``, passes through untouched.

Each gated archive writes one stderr line after its extraction finishes, ``ov-pref-gate
{"action": …, "session_id": …, "archive_uri": …, "messages": …, "roles": …, "writes": …,
"skipped_preferences": …}``. ``writes`` lists the ``preferences`` and ``profile`` URIs the
archive's ``memory_diff`` added or updated. ``profile`` is watched because the bundled
``profile.yaml`` routes preference-like facts to PREFERENCES, and with that schema gone the
model may file them in ``profile.md`` instead, where ``ov-pref-audit.py`` does not look.
A failed extraction logs ``"outcome"`` with the exception class. Modes (``OV_PREF_GATE``, read per call):
``1`` skips (``"action": "skipped"``), ``0`` is off, and anything else, unset included,
only logs (``"action": "would_skip"``), so a typo never changes what is extracted.

Version/source guarded on ``SessionCompressorV3._extract_user_memories``. Remove it once
OpenViking checks message roles before a ``preferences`` write, or the capture plugin can
set ``memory_policy`` per session.
"""

import contextlib
import functools
import hashlib
import inspect
import json
import logging
import os
import sys
from collections import Counter

EXPECTED_VERSION = "v0.4.20"
EXPECTED_SHA256 = "3c355a049198505eeeeb0efea5881a43f6505dc3e886f5fa5fc2b3e823d557b2"
GATED_TYPE = "preferences"
WATCHED_TYPES = (GATED_TYPE, "profile")
PREFIX = "ov-pref-gate "
logger = logging.getLogger("ov_pref_gate_patch")

_MODE_WARNED = set()


def read_mode() -> str:
    """``1``, ``0`` or ``shadow``; skipping needs an explicit ``1``."""
    raw = os.environ.get("OV_PREF_GATE", "shadow").strip().lower()
    if raw in ("0", "1", "shadow"):
        return raw
    if raw not in _MODE_WARNED:
        _MODE_WARNED.add(raw)
        logger.warning(
            "ov-pref-gate: OV_PREF_GATE=%r is not 0, 1 or shadow; using shadow", raw
        )
    return "shadow"


def _has_text(message) -> bool:
    return any(
        isinstance(text := getattr(part, "text", None), str) and text.strip()
        for part in getattr(message, "parts", None) or []
    )


def has_typed_user_text(messages) -> bool:
    return any(
        getattr(message, "role", None) == "user" and _has_text(message)
        for message in messages or []
    )


def role_counts(messages) -> dict:
    """Messages per role, plus ``user_text``: user messages with a non-blank text part."""
    counts = Counter(str(getattr(message, "role", None)) for message in messages or [])
    counts["user_text"] = sum(
        1
        for message in messages or []
        if getattr(message, "role", None) == "user" and _has_text(message)
    )
    return dict(sorted(counts.items()))


def outcome_fields(result) -> dict:
    """The watched writes and preference skips an extraction result records."""
    writes = {memory_type: [] for memory_type in WATCHED_TYPES}
    diff = getattr(result, "memory_diff", None)
    operations = diff.get("operations") if isinstance(diff, dict) else None
    for kind in ("adds", "updates"):
        for entry in (operations or {}).get(kind) or []:
            memory_type = entry.get("memory_type") if isinstance(entry, dict) else None
            if memory_type in writes:
                writes[memory_type].append(entry.get("uri"))
    skipped = sum(
        1
        for item in getattr(result, "skipped_operations", None) or []
        if isinstance(item, dict) and item.get("memory_type") == GATED_TYPE
    )
    return {
        "diff": operations is not None,
        "skipped_preferences": skipped,
        "writes": writes,
    }


def emit(record: dict) -> None:
    """One stderr line, best effort: a failing stderr never fails the extraction."""
    with contextlib.suppress(Exception):
        sys.stderr.write(
            PREFIX + json.dumps(record, sort_keys=True, default=str) + "\n"
        )
        sys.stderr.flush()


def gating(original, all_types):
    """Wrap ``_extract_user_memories``; ``all_types()`` stands in for a ``None`` policy."""
    signature = inspect.signature(original)

    @functools.wraps(original)
    async def _extract_user_memories(self, *args, **kwargs):
        mode = read_mode()
        if mode == "0":
            return await original(self, *args, **kwargs)
        try:
            bound = signature.bind(self, *args, **kwargs)
        except TypeError:
            return await original(self, *args, **kwargs)
        messages = bound.arguments.get("messages")
        allowed = bound.arguments.get("allowed_memory_types")
        if (
            not messages
            or has_typed_user_text(messages)
            or (allowed is not None and GATED_TYPE not in allowed)
        ):
            return await original(self, *args, **kwargs)
        record = {
            "action": "skipped" if mode == "1" else "would_skip",
            "archive_uri": bound.arguments.get("archive_uri"),
            "messages": len(messages),
            "roles": role_counts(messages),
            "session_id": bound.arguments.get("session_id"),
        }
        if mode == "1":
            types = set(all_types()) if allowed is None else set(allowed)
            bound.arguments["allowed_memory_types"] = types - {GATED_TYPE}
        try:
            result = await original(*bound.args, **bound.kwargs)
        except BaseException as exc:
            record["outcome"] = type(exc).__name__
            emit(record)
            raise
        with contextlib.suppress(Exception):
            record.update(outcome_fields(result))
        emit(record)
        return result

    _extract_user_memories._ov_pref_gate_patch = True
    return _extract_user_memories


def apply(module):
    if read_mode() == "0":
        logger.warning("ov-pref-gate-patch: disabled by OV_PREF_GATE=0")
        return False
    import openviking

    cls = getattr(module, "SessionCompressorV3", None)
    original = getattr(cls, "_extract_user_memories", None)
    registry = getattr(module, "create_default_registry", None)
    if original is None or registry is None:
        logger.warning(
            "ov-pref-gate-patch: NOT applied (SessionCompressorV3._extract_user_memories "
            "or create_default_registry missing)"
        )
        return False
    if getattr(original, "_ov_pref_gate_patch", False):
        return True
    try:
        digest = hashlib.sha256(inspect.getsource(original).encode()).hexdigest()
    except (OSError, TypeError):
        digest = None
    version = getattr(openviking, "__version__", None)
    if version != EXPECTED_VERSION or digest != EXPECTED_SHA256:
        logger.warning(
            "ov-pref-gate-patch: NOT applied (version=%r sha256=%s)", version, digest
        )
        return False

    def all_types():
        return registry().list_names(include_disabled=False)

    cls._extract_user_memories = gating(original, all_types)
    logger.warning(
        "ov-pref-gate-patch: applied in %s mode; %s skipped for archives with no typed "
        "user text (pid %d)",
        read_mode(),
        GATED_TYPE,
        os.getpid(),
    )
    return True
