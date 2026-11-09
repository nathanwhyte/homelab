"""Skip preference and profile extraction for an archive with no typed user text on v0.5.1.1.

IMPR-1404 gated ``preferences``; IMPR-1479 adds ``profile`` under its own switch, so each
type can run in shadow while the other enforces.

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
message has a typed text part, every ``_extract_user_memories`` call
for that archive runs with each enforced gated type (``preferences``, ``profile``) removed
from ``allowed_memory_types``. That one argument is OpenViking's own per-session
``memory_policy.memory_types`` seam: the isolation handler then leaves the schema out of
the prompt, the prefetch and the resolver, so the model is never asked for that type.
Every type that is not gated, or whose switch is not ``1``, extracts as before. An archive
with typed user text passes through untouched, and so does one whose policy offers no
gated type with its switch on. A call outside Phase 2 (the manual ``/extract`` endpoint,
which passes the whole session unbatched) is judged on its own messages.

A text part is typed when it is non-blank (a tool-result-only ``user`` turn does not
count) and is not agent or harness text that Claude Code delivers as a user message
(IMPR-1479 row 2): a forwarded side-agent note, bare or inside the mid-turn wrapper, a
coordinator or cross-session message, a task notification, a system reminder, a skill
body, a compaction summary, a session marker, local command or ``!`` output, an
interrupt marker, or a ``Tool loaded.`` result. Each is recognised on the whole part by
prefix and suffix, never by parsing inside it, so the shape decides the whole part
whatever it holds: harness text with other text before or after it counts as typed, and
a part that opens and closes with harness tags is harness text even with typed words
between them. The wrapper's inner message, a slash command's name and arguments,
and ``!`` command input still count. The record's ``roles`` counts ``user_text``
under this test and ``user_agent_text`` for user messages holding only such text.

Each Phase 2 run of a gated archive that makes at least one user-memory extraction call
writes one stderr line when it finishes. A resumed run whose extraction batches all
finished on an earlier attempt extracts nothing and writes none; the earlier run logged
the archive. The line is ``ov-pref-gate
{"action": …, "session_id": …, "archive_uri": …, "messages": …, "roles": …,
"extractions": …, "writes": …}``, plus ``"errors"`` when an extraction call raised.
``writes`` lists the ``preferences``, ``profile``, ``identity`` and ``soul`` URIs the
archive's extraction results added or updated. ``profile`` was first only watched, because
the bundled ``profile.yaml`` routes preference-like facts to PREFERENCES and with that
schema gone the model may file them in ``profile.md`` instead. IMPR-1401's second gate then
traced profile bullets to such archives (subagent and tool-result-only sessions), so
IMPR-1479 gates ``profile`` too. ``identity`` and ``soul`` are watched, not gated, for the
same reason one step on: with ``profile`` gone the facts may move there, and IMPR-1401's
workbook canary wrote one fact into all three.

Each gated type has its own switch, read once per archive: ``OV_PREF_GATE`` for
``preferences`` and ``OV_PROFILE_GATE`` for ``profile``. ``1`` removes the type, ``0`` is off,
and anything else, unset included, only logs, so a typo never changes what is extracted.
The line reports ``preferences`` as ``"action"`` (kept from IMPR-1404 for the Loki queries
that read it) and ``profile`` as ``"profile_action"``: ``skipped``, ``would_skip`` or
``off``, or ``not_offered`` when no gated call's policy held the type, so a
``"skipped"`` count only counts archives that were offered it. In shadow, ``writes.profile`` lists the profile writes enforce mode would have
prevented. An archive is gated, and logged, when any switch is not ``0``; with both ``0``
the patch is not installed.

When the policy is ``None`` (every type), enforce mode passes the enabled names of the
deployment's default registry (``get_default_registry()``, ``memory_type_registry.py``
L322) minus each type whose switch is ``1``. A type the policy already excludes is never
re-added. v0.5.1.1 resolves an account registry per extraction
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
PROFILE_TYPE = "profile"
# Logged on every gated archive's line; only the types in SWITCHES are ever removed.
WATCHED_TYPES = (GATED_TYPE, PROFILE_TYPE, "identity", "soul")
# Each gated memory type and the switch that controls it (IMPR-1404, IMPR-1479).
SWITCHES = {GATED_TYPE: "OV_PREF_GATE", PROFILE_TYPE: "OV_PROFILE_GATE"}
PREFIX = "ov-pref-gate "
logger = logging.getLogger("ov_pref_gate_patch")

_MODE_WARNED = set()
_state = {"phase2_applied": False}
_ARCHIVE = contextvars.ContextVar("ov_pref_gate_archive", default=None)


def read_mode(var: str = "OV_PREF_GATE") -> str:
    """``1``, ``0`` or ``shadow``; skipping needs an explicit ``1``."""
    raw = os.environ.get(var, "shadow").strip().lower()
    if raw in ("0", "1", "shadow"):
        return raw
    if (var, raw) not in _MODE_WARNED:
        _MODE_WARNED.add((var, raw))
        logger.warning(
            "ov-pref-gate: %s=%r is not 0, 1 or shadow; using shadow", var, raw
        )
    return "shadow"


def read_modes() -> dict:
    """The mode of every gated type, read now."""
    return {memory_type: read_mode(var) for memory_type, var in SWITCHES.items()}


def all_off(modes: dict) -> bool:
    return all(mode == "0" for mode in modes.values())


def _action(mode: str) -> str:
    return {"1": "skipped", "0": "off"}.get(mode, "would_skip")


# What Claude Code writes into a user turn; IMPR-1479 row 2 inventoried it in 486
# archives. Every check is on the whole part, by plain prefix and suffix comparison, so
# the cost is linear and no text inside a part changes the verdict (decision A2).
_MIDTURN_HEAD = "The user sent a new message while you were working:"
_AGENT_OPENINGS = (
    "Here is a note offered by a side agent:",
    "The coordinator sent a message while you were working",
    "Another Claude session sent a message",
    "Base directory for this skill:",
    "This session is being continued from a previous conversation",
    "[session cc-",
    "[Request interrupted by user",
)
_HARNESS_TAGS = (
    "system-reminder",
    "task-notification",
    "local-command-stdout",
    "local-command-stderr",
    "local-command-caveat",
    "bash-stdout",
    "bash-stderr",
    "cross-session-message",
    "interrupted-output",
)
_HARNESS_OPEN = tuple(f"<{tag}{end}" for tag in _HARNESS_TAGS for end in (">", " "))
_HARNESS_CLOSE = tuple(f"</{tag}>" for tag in _HARNESS_TAGS)


def _is_agent_text(body: str) -> bool:
    return (
        body.startswith(_AGENT_OPENINGS)
        or body == "Tool loaded."
        or (body.startswith(_HARNESS_OPEN) and body.endswith(_HARNESS_CLOSE))
    )


def is_typed_text(text) -> bool:
    """Whether a user text part holds text the user wrote, not agent or harness text.

    A part is agent text when, stripped, it opens with an agent opening, is
    ``Tool loaded.``, or opens with a harness tag and closes with one. The mid-turn
    wrapper is judged by what follows its header, so a side-agent note inside it is
    agent text and anything else the user sent is typed. The shape decides the whole
    part whatever lies inside: a harness block followed or preceded by other text is
    typed, and a part bookended by harness tags is not, even with typed words between.
    """
    if not isinstance(text, str):
        return False
    body = text.strip()
    if body.startswith(_MIDTURN_HEAD):
        body = body[len(_MIDTURN_HEAD) :].lstrip()
    return bool(body) and not _is_agent_text(body)


def _texts(message):
    return [
        text
        for part in getattr(message, "parts", None) or []
        if isinstance(text := getattr(part, "text", None), str) and text.strip()
    ]


def _is_typed(message) -> bool:
    return getattr(message, "role", None) == "user" and any(
        map(is_typed_text, _texts(message))
    )


def has_typed_user_text(messages) -> bool:
    return any(_is_typed(message) for message in messages or [])


def role_counts(messages) -> dict:
    """Messages per role, plus ``user_text`` (user messages with typed text) and
    ``user_agent_text`` (user messages whose text is all agent or harness text)."""
    messages = messages or []
    counts = Counter(str(getattr(message, "role", None)) for message in messages)
    counts["user_text"] = sum(map(_is_typed, messages))
    counts["user_agent_text"] = sum(
        1
        for message in messages
        if getattr(message, "role", None) == "user"
        and _texts(message)
        and not _is_typed(message)
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

    def __init__(self, archive_uri, session_id, messages, modes):
        self.archive_uri = archive_uri
        self.session_id = session_id
        self.modes = dict(modes)
        self.typed = has_typed_user_text(messages)
        self.messages = len(messages or [])
        self.roles = role_counts(messages)
        self.extractions = 0
        self.diff = False
        self.errors = []
        self.writes = {memory_type: [] for memory_type in WATCHED_TYPES}
        # The gated types any gated call offered: an action is only reported for these.
        self.offered = set()

    def _offered(self, allowed):
        """The gated types this call offers, with their switch not ``0``."""
        return {
            memory_type
            for memory_type, mode in self.modes.items()
            if mode != "0" and (allowed is None or memory_type in allowed)
        }

    def gates(self, messages, allowed) -> bool:
        return bool(messages) and not self.typed and bool(self._offered(allowed))

    def removed(self, allowed) -> set:
        """The offered types whose switch is ``1``: what this call drops."""
        return {t for t in self._offered(allowed) if self.modes[t] == "1"}

    def action(self, memory_type) -> str:
        mode = self.modes[memory_type]
        if mode != "0" and memory_type not in self.offered:
            return "not_offered"
        return _action(mode)

    def record_result(self, result) -> None:
        with contextlib.suppress(Exception):
            writes = watched_writes(result)
            if writes is not None:
                self.diff = True
                for memory_type, uris in writes.items():
                    self.writes[memory_type].extend(uris)

    def record(self) -> dict:
        record = {
            "action": self.action(GATED_TYPE),
            "archive_uri": self.archive_uri,
            "diff": self.diff,
            "extractions": self.extractions,
            "messages": self.messages,
            "profile_action": self.action(PROFILE_TYPE),
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
        if archive is None and all_off(read_modes()):
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
                archive_uri, bound.arguments.get("session_id"), messages, read_modes()
            )
            lone = True
        else:
            # Phase 2 batches cannot be told from a lone call: leave extraction stock.
            return await original(self, *args, **kwargs)
        if not gate.gates(messages, allowed):
            return await original(self, *args, **kwargs)
        gate.offered |= gate._offered(allowed)
        removed = gate.removed(allowed)
        if removed:
            types = set(all_types()) if allowed is None else set(allowed)
            bound.arguments["allowed_memory_types"] = types - removed
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
        modes = read_modes()
        try:
            bound = signature.bind(self, *args, **kwargs)
        except TypeError:
            return await original(self, *args, **kwargs)
        gate = Gate(
            bound.arguments.get("archive_uri"),
            getattr(self, "session_id", None),
            bound.arguments.get("messages"),
            modes,
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
    if all_off(read_modes()):
        logger.warning(
            "ov-pref-gate-patch: disabled by OV_PREF_GATE=0 and OV_PROFILE_GATE=0"
        )
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
    modes = read_modes()
    logger.warning(
        "ov-pref-gate-patch: applied; for archives with no typed user text: %s (pid %d)",
        ", ".join(f"{t} {_action(m)}" for t, m in modes.items()),
        os.getpid(),
    )
    return True


def apply_session(module):
    """Install the per-archive half on ``Session._run_memory_extraction``."""
    if all_off(read_modes()):
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
