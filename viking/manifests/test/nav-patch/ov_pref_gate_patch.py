"""Skip preference extraction for an archive with no typed user text on v0.5.1.1 (IMPR-1404).

``SessionCompressorV3._extract_user_memories`` sends every enabled ``stage: user`` schema
to the model in one call, ``preferences`` included, whatever the archive holds. The
capture plugin sends every turn of a subagent session as ``assistant`` (BUG-1215), so such
an archive reaches the extractor with no ``[user]`` line at all, and the homelab#248
``preferences`` override ("only from [user] messages") has nothing to apply to. All 13
false preferences flagged by ``ov-pref-audit.py`` up to 2026-10-21 came from archives like
that (BUG-1260), three of them written after the override went live.

The decision is made once per archive. ``Session._run_memory_extraction`` (Phase 2 of a
commit) receives the archive's full message list before Phase 2 splits it into
extraction batches (ov_extract_patch caps a batch at 60 messages) or drops the batches a
resumed archive already finished. Its wrapper checks that full list: when no ``user``
message has a non-blank text part (the same test the extractor uses to render a line, so
a tool-result-only ``user`` turn does not count), every ``_extract_user_memories`` call
for that archive runs with ``preferences`` removed from ``allowed_memory_types``. That one
argument is OpenViking's own per-session ``memory_policy.memory_types`` seam: the
isolation handler then leaves the schema out of the prompt, the prefetch and the
resolver, so the model is never asked for a preference. Every other memory type extracts
as before. An archive with typed user text, or one whose policy already excludes
``preferences``, passes through untouched. A call outside Phase 2 (the manual
``/extract`` endpoint, which passes the whole session unbatched) is judged on its own
messages.

Each Phase 2 run of a gated archive that makes at least one user-memory extraction call
writes one stderr line when it finishes. A resumed run whose extraction batches all
finished on an earlier attempt extracts nothing and writes none; the earlier run logged
the archive. The line is ``ov-pref-gate
{"action": …, "session_id": …, "archive_uri": …, "messages": …, "roles": …,
"extractions": …, "writes": …}``, plus ``"errors"`` when an extraction call raised.
``writes`` lists the ``preferences`` and ``profile`` URIs the archive's extraction
results added or updated. ``profile`` is watched because the bundled ``profile.yaml``
routes preference-like facts to PREFERENCES, and with that schema gone the model may file
them in ``profile.md`` instead, where ``ov-pref-audit.py`` does not look. Modes
(``OV_PREF_GATE``, read once per archive): ``1`` skips (``"action": "skipped"``), ``0`` is
off, and anything else, unset included, only logs (``"action": "would_skip"``), so a typo
never changes what is extracted.

When the policy is ``None`` (every type), enforce mode passes the enabled names of the
deployment's default registry (``get_default_registry()``, ``memory_type_registry.py``
L322) minus ``preferences``. v0.5.1.1 resolves an account registry per extraction
(``resolve_account_memory_registry``, ``compressor_v3.py`` L692-L697); it holds one
schema per default schema, so the names match, but an account template that enables a
type the default leaves disabled is not seen here.

Two halves, each guarded on v0.5.1.1 and the source hash of the method it wraps. Without
the session half a Phase 2 batch cannot be told from a whole archive, so the compressor
half then gates nothing and extraction stays stock. Remove the patch once OpenViking checks
message roles before a ``preferences`` write, or the capture plugin can set
``memory_policy`` per session.
"""

import contextlib
import contextvars
import functools
import hashlib
import inspect
import json
import logging
import os
import sys
from collections import Counter

EXPECTED_VERSION = "v0.5.1.1"
# openviking.session.compressor_v3.SessionCompressorV3._extract_user_memories on v0.5.1.1.
EXPECTED_SHA256 = "29befc9a5f91a6c91f1c0b74fb0bf6dad52ebd7e95304759cb4408093c0f21a6"
# openviking.session.session.Session._run_memory_extraction on v0.5.1.1.
EXPECTED_PHASE2_SHA256 = (
    "d64ddfa93c3d0d33c665f372c746f22f9bca9f670c7c4542d4e68f3e1527e9d2"
)
GATED_TYPE = "preferences"
WATCHED_TYPES = (GATED_TYPE, "profile")
PREFIX = "ov-pref-gate "
logger = logging.getLogger("ov_pref_gate_patch")

_MODE_WARNED = set()
_state = {"phase2_applied": False}
_ARCHIVE = contextvars.ContextVar("ov_pref_gate_archive", default=None)


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


def watched_writes(result) -> dict | None:
    """The watched URIs an extraction result added or updated; ``None`` without a diff."""
    diff = getattr(result, "memory_diff", None)
    operations = diff.get("operations") if isinstance(diff, dict) else None
    if not isinstance(operations, dict):
        return None
    writes = {memory_type: [] for memory_type in WATCHED_TYPES}
    for kind in ("adds", "updates"):
        for entry in operations.get(kind) or []:
            memory_type = entry.get("memory_type") if isinstance(entry, dict) else None
            if memory_type in writes:
                writes[memory_type].append(entry.get("uri"))
    return writes


def emit(record: dict) -> None:
    """One stderr line, best effort: a failing stderr never fails the extraction."""
    with contextlib.suppress(Exception):
        sys.stderr.write(
            PREFIX + json.dumps(record, sort_keys=True, default=str) + "\n"
        )
        sys.stderr.flush()


class Gate:
    """One gating decision and its outcome: an archive's Phase 2, or a lone call."""

    def __init__(self, archive_uri, session_id, messages, mode):
        self.archive_uri = archive_uri
        self.session_id = session_id
        self.mode = mode
        self.typed = has_typed_user_text(messages)
        self.messages = len(messages or [])
        self.roles = role_counts(messages)
        self.extractions = 0
        self.diff = False
        self.errors = []
        self.writes = {memory_type: [] for memory_type in WATCHED_TYPES}

    def gates(self, messages, allowed) -> bool:
        return (
            bool(messages)
            and not self.typed
            and (allowed is None or GATED_TYPE in allowed)
        )

    def record_result(self, result) -> None:
        with contextlib.suppress(Exception):
            writes = watched_writes(result)
            if writes is not None:
                self.diff = True
                for memory_type, uris in writes.items():
                    self.writes[memory_type].extend(uris)

    def record(self) -> dict:
        record = {
            "action": "skipped" if self.mode == "1" else "would_skip",
            "archive_uri": self.archive_uri,
            "diff": self.diff,
            "extractions": self.extractions,
            "messages": self.messages,
            "roles": self.roles,
            "session_id": self.session_id,
            "writes": self.writes,
        }
        if self.errors:
            record["errors"] = self.errors
        return record


def gating(original, all_types):
    """Wrap ``_extract_user_memories``; ``all_types()`` stands in for a ``None`` policy."""
    signature = inspect.signature(original)

    @functools.wraps(original)
    async def _extract_user_memories(self, *args, **kwargs):
        archive = _ARCHIVE.get()
        if archive is None and read_mode() == "0":
            return await original(self, *args, **kwargs)
        try:
            bound = signature.bind(self, *args, **kwargs)
        except TypeError:
            return await original(self, *args, **kwargs)
        messages = bound.arguments.get("messages")
        allowed = bound.arguments.get("allowed_memory_types")
        archive_uri = bound.arguments.get("archive_uri")
        if archive is not None and archive.archive_uri == archive_uri:
            gate, lone = archive, False
        elif _state["phase2_applied"]:
            # A call outside Phase 2 (or for another archive) is judged on its own messages.
            gate = Gate(
                archive_uri, bound.arguments.get("session_id"), messages, read_mode()
            )
            lone = True
        else:
            # Phase 2 batches cannot be told from a lone call: leave extraction stock.
            return await original(self, *args, **kwargs)
        if gate.mode == "0" or not gate.gates(messages, allowed):
            return await original(self, *args, **kwargs)
        if gate.mode == "1":
            types = set(all_types()) if allowed is None else set(allowed)
            bound.arguments["allowed_memory_types"] = types - {GATED_TYPE}
        gate.extractions += 1
        try:
            result = await original(*bound.args, **bound.kwargs)
        except BaseException as exc:
            gate.errors.append(type(exc).__name__)
            if lone:
                emit(gate.record())
            raise
        gate.record_result(result)
        if lone:
            emit(gate.record())
        return result

    _extract_user_memories._ov_pref_gate_patch = True
    return _extract_user_memories


def archive_scope(original):
    """Wrap ``Session._run_memory_extraction``: one decision per archive, and one line per
    run that extracted under it."""
    signature = inspect.signature(original)

    @functools.wraps(original)
    async def _run_memory_extraction(self, *args, **kwargs):
        mode = read_mode()
        try:
            bound = signature.bind(self, *args, **kwargs)
        except TypeError:
            return await original(self, *args, **kwargs)
        gate = Gate(
            bound.arguments.get("archive_uri"),
            getattr(self, "session_id", None),
            bound.arguments.get("messages"),
            mode,
        )
        token = _ARCHIVE.set(gate)
        try:
            return await original(self, *args, **kwargs)
        finally:
            _ARCHIVE.reset(token)
            if gate.extractions:
                emit(gate.record())

    _run_memory_extraction._ov_pref_gate_patch = True
    return _run_memory_extraction


def _source_hash(fn):
    try:
        return hashlib.sha256(inspect.getsource(fn).encode()).hexdigest()
    except (OSError, TypeError):
        return None


def _version():
    import openviking

    return getattr(openviking, "__version__", None)


def apply(module):
    if read_mode() == "0":
        logger.warning("ov-pref-gate-patch: disabled by OV_PREF_GATE=0")
        return False
    cls = getattr(module, "SessionCompressorV3", None)
    original = getattr(cls, "_extract_user_memories", None)
    registry = getattr(module, "get_default_registry", None)
    if original is None or registry is None:
        logger.warning(
            "ov-pref-gate-patch: NOT applied (SessionCompressorV3._extract_user_memories "
            "or get_default_registry missing)"
        )
        return False
    if getattr(original, "_ov_pref_gate_patch", False):
        return True
    version, digest = _version(), _source_hash(original)
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


def apply_session(module):
    """Install the per-archive half on ``Session._run_memory_extraction``."""
    if read_mode() == "0":
        return False
    cls = getattr(module, "Session", None)
    original = getattr(cls, "_run_memory_extraction", None)
    if original is None:
        logger.warning(
            "ov-pref-gate-patch: NOT applied to Phase 2 (Session._run_memory_extraction "
            "missing); the gate stays off"
        )
        return False
    if getattr(original, "_ov_pref_gate_patch", False):
        _state["phase2_applied"] = True
        return True
    version, digest = _version(), _source_hash(original)
    if version != EXPECTED_VERSION or digest != EXPECTED_PHASE2_SHA256:
        logger.warning(
            "ov-pref-gate-patch: NOT applied to Phase 2 (version=%r sha256=%s); the "
            "gate stays off",
            version,
            digest,
        )
        return False
    cls._run_memory_extraction = archive_scope(original)
    _state["phase2_applied"] = True
    logger.warning(
        "ov-pref-gate-patch: applied to Phase 2; one decision per archive (pid %d)",
        os.getpid(),
    )
    return True
