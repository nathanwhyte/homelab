"""ChatLog speaker labels follow the turn's role, and tool-only turns drop out (BUG-1177).

OpenViking v0.4.20 renders the ``ChatLog`` of an extracted memory (the ``events`` template,
``prompts/templates/memory/events.yaml``) through ``MessageRange.pretty_print`` in
``openviking.session.memory.memory_updater``. Each line is ``**{speaker}**: {content}``, and
``MessageRange._speaker_for`` picks the speaker as ``peer_id or role``. The pilot lane runs
recall peer scope ``actor`` with an explicit compendium peer, so every turn, user and
assistant alike, carries the same ``peer_id``: every ChatLog line gets one label and a user's
approval reads exactly like the agent's reply. ``role`` is stored on every message but never
consulted. Separately, ``_format_contiguous_group`` emits a line for every message group
without an empty-content check, and ``_message_content`` reads only ``TextPart`` — so an
assistant turn that was only tool calls renders as an empty ``**name**:`` line.

This patch changes two things in ``MessageRange``:

  * ``_speaker_for`` — an assistant turn is labelled ``assistant``; any other turn keeps the
    stock ``peer_id or role`` (several human peers in one shared session stay distinct).
    ``_can_merge_messages`` compares speakers, so a user chunk can no longer merge into an
    assistant chunk under the shared label.
  * ``_format_contiguous_group`` — lines whose content is empty after the speaker label are
    dropped. Only the rendered ChatLog changes; stored messages and tool parts are untouched.

Rendering only: capture, the stored archive, recall and extraction are unchanged.

Guarded: applies only to openviking ``v0.4.20`` whose ``_speaker_for`` and
``_format_contiguous_group`` sources hash to the expected values; otherwise it logs
"NOT applied" and the stock renderer stays. Disable at runtime with ``OV_CHATLOG_PATCH=0``.
"""

from __future__ import annotations

import functools
import hashlib
import inspect
import logging
import os
import re

EXPECTED_VERSION = "v0.4.20"
EXPECTED_SPEAKER_SHA256 = (
    "b3117533db57255151072c51d4ffb839172f6533cefe2cc3c295689f8ab00a64"
)
EXPECTED_GROUP_SHA256 = (
    "aa7c32112426ca961768e50942b51f4841a9b238cec8a641493fa74e4b578f5d"
)
ASSISTANT_LABEL = "assistant"

logger = logging.getLogger("ov_chatlog_patch")
_EMPTY_LINE = re.compile(r"^\*\*.*?\*\*:\s*$", re.DOTALL)


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


def wrap_group(orig):
    """Wrap ``_format_contiguous_group``: drop lines with nothing after the label."""

    @functools.wraps(orig)
    def format_contiguous_group(self, msg_group):
        lines = orig(self, msg_group)
        return [line for line in lines if not _EMPTY_LINE.match(line)]

    format_contiguous_group._ov_chatlog_patch = True
    return format_contiguous_group


def apply(module) -> bool:
    """Patch ``module.MessageRange`` in place; True if applied."""
    if not _enabled():
        return False
    import openviking

    version = getattr(openviking, "__version__", None)
    cls = getattr(module, "MessageRange", None)
    group = getattr(cls, "_format_contiguous_group", None)
    if getattr(group, "_ov_chatlog_patch", False):
        return True
    raw_speaker = cls.__dict__.get("_speaker_for") if cls is not None else None
    speaker_fn = (
        raw_speaker.__func__ if isinstance(raw_speaker, staticmethod) else raw_speaker
    )
    speaker_digest = _source_hash(speaker_fn) if speaker_fn else None
    group_digest = _source_hash(group) if group else None
    if (
        version != EXPECTED_VERSION
        or speaker_digest != EXPECTED_SPEAKER_SHA256
        or group_digest != EXPECTED_GROUP_SHA256
    ):
        logger.warning(
            "ov-chatlog-patch: NOT applied to MessageRange (version=%r speaker_sha256=%s "
            "group_sha256=%s); stock ChatLog rendering kept",
            version,
            speaker_digest,
            group_digest,
        )
        return False
    cls._speaker_for = staticmethod(speaker_for)
    cls._format_contiguous_group = wrap_group(group)
    logger.warning(
        "ov-chatlog-patch: applied to MessageRange ChatLog rendering (pid %d)",
        os.getpid(),
    )
    return True


# module name -> apply function, consumed by sitecustomize.py
TARGETS = {
    "openviking.session.memory.memory_updater": apply,
}
