"""Tests for ov_chatlog_patch. The fake-only cases run anywhere:

    python3 test_ov_chatlog_patch.py

``RealRendererTests`` need the installed openviking (run inside the v0.5.1.1 image): they
load a fresh, unpatched copy of ``memory_updater`` and check the guard applies to the real
``MessageRange`` and that the patched renderer labels turns by role. They also pin the
upstream #5139 behaviour this patch no longer carries: stock v0.5.1.1 already drops the
empty line of a tool-only turn.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "_scripts" / "lib"))
from git_env import scrub_environ  # noqa: E402

scrub_environ()

import importlib.util
import os
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ov_chatlog_patch as cp  # noqa: E402

TARGET = "openviking.session.memory.memory_updater"
HAVE_OPENVIKING = importlib.util.find_spec("openviking") is not None


class Msg:
    def __init__(self, role, peer_id=None):
        self.role = role
        self.peer_id = peer_id


PEER = "github.com-nathanwhyte-compendium"


class SpeakerTests(unittest.TestCase):
    def test_assistant_turn_is_labelled_assistant_even_with_a_peer(self):
        self.assertEqual(cp.speaker_for(Msg("assistant", PEER)), "assistant")

    def test_user_turn_keeps_its_peer(self):
        self.assertEqual(cp.speaker_for(Msg("user", PEER)), PEER)

    def test_user_turn_without_peer_falls_back_to_role(self):
        self.assertEqual(cp.speaker_for(Msg("user")), "user")

    def test_other_roles_keep_stock_behaviour(self):
        self.assertEqual(cp.speaker_for(Msg("system", PEER)), PEER)
        self.assertEqual(cp.speaker_for(Msg("system")), "system")


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.saved = sys.modules.get("openviking")

    def tearDown(self):
        if self.saved is None:
            sys.modules.pop("openviking", None)
        else:
            sys.modules["openviking"] = self.saved
        os.environ.pop("OV_CHATLOG_PATCH", None)

    def fake(self, version):
        ov = types.ModuleType("openviking")
        ov.__version__ = version
        sys.modules["openviking"] = ov

        class MessageRange:
            @staticmethod
            def _speaker_for(message):
                return getattr(message, "peer_id", None) or message.role

            def _format_contiguous_group(self, msg_group):
                return []

        module = types.ModuleType(TARGET)
        module.MessageRange = MessageRange
        return module, MessageRange

    def test_refuses_wrong_version(self):
        module, cls = self.fake("v0.4.99")
        orig = cls.__dict__["_speaker_for"]
        self.assertFalse(cp.apply(module))
        self.assertIs(cls.__dict__["_speaker_for"], orig)

    def test_refuses_source_drift_on_the_right_version(self):
        module, cls = self.fake(cp.EXPECTED_VERSION)
        orig = cls.__dict__["_speaker_for"]
        self.assertFalse(cp.apply(module))
        self.assertIs(cls.__dict__["_speaker_for"], orig)

    def test_kill_switch(self):
        module, cls = self.fake(cp.EXPECTED_VERSION)
        os.environ["OV_CHATLOG_PATCH"] = "0"
        self.assertFalse(cp.apply(module))

    def test_already_applied_is_idempotent(self):
        module, cls = self.fake(cp.EXPECTED_VERSION)
        cls._speaker_for = staticmethod(cp.speaker_for)
        self.assertTrue(cp.apply(module))
        self.assertIs(cls.__dict__["_speaker_for"].__func__, cp.speaker_for)

    def test_group_renderer_is_left_alone(self):
        module, cls = self.fake(cp.EXPECTED_VERSION)
        group = cls.__dict__["_format_contiguous_group"]
        cls._speaker_for = staticmethod(cp.speaker_for)
        cp.apply(module)
        self.assertIs(cls.__dict__["_format_contiguous_group"], group)

    def test_targets_name_the_renderer_module(self):
        self.assertIn(TARGET, cp.TARGETS)


def fresh_target():
    """A new, unpatched copy of the renderer module under a name sitecustomize ignores."""
    name = "_ov_chatlog_stock_copy"
    spec = importlib.util.find_spec(TARGET)
    copy = importlib.util.spec_from_file_location(name, spec.origin)
    module = importlib.util.module_from_spec(copy)
    sys.modules[name] = module  # dataclasses resolve annotations through sys.modules
    try:
        copy.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


@unittest.skipUnless(
    HAVE_OPENVIKING, "needs the installed openviking (run in the image)"
)
class RealRendererTests(unittest.TestCase):
    def setUp(self):
        os.environ.pop("OV_CHATLOG_PATCH", None)
        from openviking.message import Message
        from openviking.message.part import TextPart, ToolPart

        self.module = fresh_target()
        self.Message, self.TextPart, self.ToolPart = Message, TextPart, ToolPart

    def msg(self, i, role, *parts):
        return self.Message(id=f"m{i}", role=role, parts=list(parts), peer_id=PEER)

    def conversation(self):
        return [
            self.msg(1, "user", self.TextPart(text="merge them")),
            self.msg(2, "assistant", self.ToolPart(tool_id="t1", tool_name="Bash")),
            self.msg(3, "assistant", self.TextPart(text="Both PRs are merged.")),
        ]

    def test_guard_matches_the_real_target(self):
        stock = cp._speaker_fn(self.module.MessageRange)
        self.assertEqual(cp._source_hash(stock), cp.EXPECTED_SPEAKER_SHA256)
        self.assertTrue(cp.apply(self.module))
        self.assertIs(
            self.module.MessageRange.__dict__["_speaker_for"].__func__, cp.speaker_for
        )
        self.assertTrue(cp.apply(self.module))  # idempotent

    def test_stock_renderer_already_drops_tool_only_turns(self):
        # upstream #5139: the v0.4.20 empty-line half of this patch is no longer needed
        out = self.module.MessageRange([self.conversation()]).pretty_print()
        self.assertEqual(
            out.splitlines(),
            [f"**{PEER}**: merge them", f"**{PEER}**: Both PRs are merged."],
        )

    def test_patched_renderer_labels_turns_by_role(self):
        self.assertTrue(cp.apply(self.module))
        out = self.module.MessageRange([self.conversation()]).pretty_print()
        self.assertEqual(
            out.splitlines(),
            [f"**{PEER}**: merge them", "**assistant**: Both PRs are merged."],
        )


if __name__ == "__main__":
    unittest.main()
