"""ChatLog speaker labels follow the turn's role (BUG-1177).

OpenViking v0.5.1.1 renders the ``ChatLog`` of an extracted memory (the ``events`` template,
``prompts/templates/memory/events.yaml``) through ``MessageRange.pretty_print`` in
``openviking.session.memory.memory_updater``. Each line is ``**{speaker}**: {content}``, and
``MessageRange._speaker_for`` (``memory_updater.py:654``) picks the speaker as
``peer_id or role``. The pilot lane runs recall peer scope ``actor`` with an explicit
compendium peer, so every turn, user and assistant alike, carries the same ``peer_id``: every
ChatLog line gets one label and a user's approval reads exactly like the agent's reply.
``role`` is stored on every message but never consulted.

This patch replaces ``MessageRange._speaker_for``: an assistant turn is labelled
``assistant``; any other turn keeps the stock ``peer_id or role`` (several human peers in one
shared session stay distinct). ``_can_merge_messages`` compares speakers, so a user chunk can
no longer merge into an assistant chunk under the shared label.

The v0.4.20 patch also dropped the empty ``**name**:`` line a tool-only assistant turn
rendered. Upstream #5139 fixed that in v0.5.1.1: ``_format_contiguous_group``'s
``flush_current`` emits a line only when ``content.strip()`` (``memory_updater.py:640-642``),
so that half is gone and ``_format_contiguous_group`` is no longer touched or hashed.

Rendering only: capture, the stored archive, recall and extraction are unchanged.

Guarded: applies only to openviking ``v0.5.1.1`` whose ``_speaker_for`` source hashes to the
expected value; otherwise it logs "NOT applied" and the stock renderer stays. Disable at
runtime with ``OV_CHATLOG_PATCH=0``.
"""

from __future__ import annotations

import hashlib
import inspect
import logging
import os

EXPECTED_VERSION = "v0.5.1.1"
EXPECTED_SPEAKER_SHA256 = (
    "b3117533db57255151072c51d4ffb839172f6533cefe2cc3c295689f8ab00a64"
)
ASSISTANT_LABEL = "assistant"

logger = logging.getLogger("ov_chatlog_patch")


def _source_hash(fn) -> str | None:
    try:
        return hashlib.sha256(inspect.getsource(fn).encode()).hexdigest()
    except (OSError, TypeError):
        return None


def _enabled() -> bool:
    if os.environ.get("OV_CHATLOG_PATCH", "1") == "0":
        logger.warning("ov-chatlog-patch: disabled by OV_CHATLOG_PATCH=0")
        return False
    return True


def speaker_for(message) -> str:
    """Assistant turns are ``assistant``; other turns keep ``peer_id or role``."""
    role = getattr(message, "role", None)
    if role == ASSISTANT_LABEL:
        return ASSISTANT_LABEL
    return getattr(message, "peer_id", None) or role


speaker_for._ov_chatlog_patch = True


def _speaker_fn(cls):
    """The function behind ``cls._speaker_for`` (a staticmethod), or None."""
    raw = cls.__dict__.get("_speaker_for") if cls is not None else None
    return raw.__func__ if isinstance(raw, staticmethod) else raw


def apply(module) -> bool:
    """Patch ``module.MessageRange`` in place; True if applied."""
    if not _enabled():
        return False
    import openviking

    version = getattr(openviking, "__version__", None)
    cls = getattr(module, "MessageRange", None)
    speaker_fn = _speaker_fn(cls)
    if getattr(speaker_fn, "_ov_chatlog_patch", False):
        return True
    speaker_digest = _source_hash(speaker_fn) if speaker_fn else None
    if version != EXPECTED_VERSION or speaker_digest != EXPECTED_SPEAKER_SHA256:
        logger.warning(
            "ov-chatlog-patch: NOT applied to MessageRange (version=%r speaker_sha256=%s); "
            "stock ChatLog rendering kept",
            version,
            speaker_digest,
        )
        return False
    cls._speaker_for = staticmethod(speaker_for)
    logger.warning(
        "ov-chatlog-patch: applied to MessageRange ChatLog rendering (pid %d)",
        os.getpid(),
    )
    return True


# module name -> apply function, consumed by sitecustomize.py
TARGETS = {
    "openviking.session.memory.memory_updater": apply,
}
