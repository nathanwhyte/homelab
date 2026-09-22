"""Tests for ov_chatlog_patch. Pure (fakes only) — runs anywhere:

    python3 test_ov_chatlog_patch.py

The version/source-hash guard and the rendering against the INSTALLED openviking are
exercised by piping ov_chatlog_patch.py into a throwaway interpreter in the ov-test pod
(see the BUG-1177 PR); this file covers the wrapper semantics and the guard's refusals.
"""

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ov_chatlog_patch as cp  # noqa: E402


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


class GroupTests(unittest.TestCase):
    def wrapped(self, lines):
        return cp.wrap_group(lambda self, group: list(lines))(None, [])

    def test_empty_content_lines_are_dropped(self):
        out = self.wrapped(
            [
                f"**{PEER}**: merge them",
                "**assistant**: ",
                "**assistant**:",
                "**assistant**:   \n  ",
                "**assistant**: Both PRs are merged.",
            ]
        )
        self.assertEqual(
            out, [f"**{PEER}**: merge them", "**assistant**: Both PRs are merged."]
        )

    def test_multiline_content_is_kept(self):
        line = "**assistant**: first\nsecond"
        self.assertEqual(self.wrapped([line]), [line])

    def test_separator_and_non_label_lines_pass_through(self):
        self.assertEqual(self.wrapped(["...", "plain"]), ["...", "plain"])

    def test_wrapper_is_marked(self):
        self.assertTrue(cp.wrap_group(lambda s, g: [])._ov_chatlog_patch)


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

        module = types.ModuleType("openviking.session.memory.memory_updater")
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
        cls._format_contiguous_group = cp.wrap_group(cls._format_contiguous_group)
        self.assertTrue(cp.apply(module))

    def test_targets_name_the_renderer_module(self):
        self.assertIn("openviking.session.memory.memory_updater", cp.TARGETS)


if __name__ == "__main__":
    unittest.main()
