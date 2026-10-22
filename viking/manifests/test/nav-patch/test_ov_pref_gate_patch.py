"""Typed-text detection, gating modes, pass-through, and guard checks for ov_pref_gate_patch."""

import asyncio
import io
import json
import os
import sys
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ov_pref_gate_patch as gate

ALL_TYPES = ["entities", "events", "identity", "preferences", "profile", "soul"]


def _text(text):
    return types.SimpleNamespace(type="text", text=text)


def _tool():
    return types.SimpleNamespace(type="tool", tool_name="Bash", tool_output="ok")


def _msg(role, *parts):
    return types.SimpleNamespace(role=role, parts=list(parts))


SUBAGENT_ARCHIVE = [
    _msg("assistant", _text("Implement compendium BUG-1260 fix part 2 …")),
    _msg("user", _tool()),
    _msg("assistant", _text("Handled the relay directly.")),
]
TYPED_ARCHIVE = [
    _msg("user", _text("merge 1987")),
    _msg("assistant", _text("Merged.")),
]


async def _original(
    self,
    messages,
    user=None,
    session_id=None,
    ctx=None,
    strict_extract_errors=False,
    latest_archive_overview="",
    archive_uri=None,
    allowed_memory_types=None,
    allow_self_memory=True,
    peer_memory_enabled=True,
    allowed_peer_ids=None,
    event_search_tags=None,
):
    self.calls.append(
        {
            "messages": messages,
            "archive_uri": archive_uri,
            "allowed_memory_types": allowed_memory_types,
            "allow_self_memory": allow_self_memory,
        }
    )
    if self.error:
        raise self.error
    return self.result


class _Compressor:
    def __init__(self, result=None, error=None):
        self.calls = []
        self.result = result if result is not None else types.SimpleNamespace()
        self.error = error


def _result(adds=(), updates=(), skipped=()):
    return types.SimpleNamespace(
        memory_diff={
            "operations": {
                "adds": [{"uri": u, "memory_type": t, "after": ""} for t, u in adds],
                "updates": [{"uri": u, "memory_type": t} for t, u in updates],
                "deletes": [],
            }
        },
        skipped_operations=[{"memory_type": t, "reason_code": "x"} for t in skipped],
    )


def _run(wrapped, *args, compressor=None, **kwargs):
    compressor = compressor or _Compressor()
    stderr = io.StringIO()
    with patch.object(gate.sys, "stderr", stderr):
        result = asyncio.run(wrapped(compressor, *args, **kwargs))
    records = [
        json.loads(line[len(gate.PREFIX) :])
        for line in stderr.getvalue().splitlines()
        if line.startswith(gate.PREFIX)
    ]
    return result, compressor.calls, records


class TypedTextTests(unittest.TestCase):
    def test_subagent_archive_has_none(self):
        self.assertFalse(gate.has_typed_user_text(SUBAGENT_ARCHIVE))

    def test_tool_only_and_blank_user_turns_do_not_count(self):
        self.assertFalse(gate.has_typed_user_text([_msg("user", _tool())]))
        self.assertFalse(gate.has_typed_user_text([_msg("user", _text("  \n"))]))
        self.assertFalse(gate.has_typed_user_text([_msg("user")]))
        self.assertFalse(gate.has_typed_user_text([]))
        self.assertFalse(gate.has_typed_user_text(None))

    def test_typed_user_text_counts(self):
        self.assertTrue(gate.has_typed_user_text(TYPED_ARCHIVE))
        self.assertTrue(
            gate.has_typed_user_text([_msg("user", _tool(), _text("why?"))])
        )

    def test_role_counts(self):
        self.assertEqual(
            gate.role_counts(SUBAGENT_ARCHIVE + TYPED_ARCHIVE),
            {"assistant": 3, "user": 2, "user_text": 1},
        )


class GatingTests(unittest.TestCase):
    def setUp(self):
        self.wrapped = gate.gating(_original, lambda: ALL_TYPES)

    def test_enforce_drops_preferences_from_an_explicit_policy(self):
        extracted = _result(
            adds=[("events", "viking://e/1.md")],
            updates=[("profile", "viking://p/profile.md")],
            skipped=["preferences", "events"],
        )
        with patch.dict(os.environ, {"OV_PREF_GATE": "1"}):
            result, calls, records = _run(
                self.wrapped,
                messages=SUBAGENT_ARCHIVE,
                session_id="cc-35c37c51__subagent-ab4c",
                archive_uri="viking://s/history/archive_005",
                allowed_memory_types={"events", "preferences", "profile"},
                allow_self_memory=False,
                compressor=_Compressor(result=extracted),
            )
        self.assertIs(result, extracted)
        self.assertEqual(calls[0]["allowed_memory_types"], {"events", "profile"})
        self.assertFalse(calls[0]["allow_self_memory"])
        self.assertIs(calls[0]["messages"], SUBAGENT_ARCHIVE)
        self.assertEqual(
            records,
            [
                {
                    "action": "skipped",
                    "archive_uri": "viking://s/history/archive_005",
                    "diff": True,
                    "messages": 3,
                    "roles": {"assistant": 2, "user": 1, "user_text": 0},
                    "session_id": "cc-35c37c51__subagent-ab4c",
                    "skipped_preferences": 1,
                    "writes": {
                        "preferences": [],
                        "profile": ["viking://p/profile.md"],
                    },
                }
            ],
        )

    def test_shadow_records_the_preference_writes_it_let_through(self):
        extracted = _result(adds=[("preferences", "viking://pref/relay.md")])
        _, _, records = _run(
            self.wrapped,
            messages=SUBAGENT_ARCHIVE,
            compressor=_Compressor(result=extracted),
        )
        self.assertEqual(
            records[0]["writes"],
            {"preferences": ["viking://pref/relay.md"], "profile": []},
        )

    def test_result_without_a_diff_still_logs(self):
        with patch.dict(os.environ, {"OV_PREF_GATE": "1"}):
            _, _, records = _run(self.wrapped, messages=SUBAGENT_ARCHIVE)
        self.assertFalse(records[0]["diff"])
        self.assertEqual(records[0]["writes"], {"preferences": [], "profile": []})

    def test_failed_extraction_logs_and_reraises(self):
        compressor = _Compressor(error=TimeoutError("vlm"))
        with patch.dict(os.environ, {"OV_PREF_GATE": "1"}):
            with self.assertRaises(TimeoutError):
                _run(self.wrapped, messages=SUBAGENT_ARCHIVE, compressor=compressor)
        self.assertNotIn("preferences", compressor.calls[0]["allowed_memory_types"])

    def test_enforce_expands_a_none_policy(self):
        with patch.dict(os.environ, {"OV_PREF_GATE": "1"}):
            _, calls, _ = _run(self.wrapped, SUBAGENT_ARCHIVE)
        self.assertEqual(
            calls[0]["allowed_memory_types"], set(ALL_TYPES) - {"preferences"}
        )

    def test_shadow_logs_and_extracts_unchanged(self):
        for env in ({}, {"OV_PREF_GATE": "shadow"}, {"OV_PREF_GATE": "yes"}):
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True):
                _, calls, records = _run(self.wrapped, messages=SUBAGENT_ARCHIVE)
                self.assertIsNone(calls[0]["allowed_memory_types"])
                self.assertEqual([r["action"] for r in records], ["would_skip"])

    def test_off_passes_through_silently(self):
        with patch.dict(os.environ, {"OV_PREF_GATE": "0"}):
            _, calls, records = _run(self.wrapped, messages=SUBAGENT_ARCHIVE)
        self.assertIsNone(calls[0]["allowed_memory_types"])
        self.assertEqual(records, [])

    def test_typed_text_passes_through(self):
        with patch.dict(os.environ, {"OV_PREF_GATE": "1"}):
            _, calls, records = _run(
                self.wrapped,
                messages=TYPED_ARCHIVE,
                allowed_memory_types={"preferences", "events"},
            )
        self.assertEqual(calls[0]["allowed_memory_types"], {"preferences", "events"})
        self.assertEqual(records, [])

    def test_policy_without_preferences_passes_through(self):
        with patch.dict(os.environ, {"OV_PREF_GATE": "1"}):
            _, calls, records = _run(
                self.wrapped, messages=SUBAGENT_ARCHIVE, allowed_memory_types={"events"}
            )
        self.assertEqual(calls[0]["allowed_memory_types"], {"events"})
        self.assertEqual(records, [])

    def test_empty_archive_passes_through(self):
        with patch.dict(os.environ, {"OV_PREF_GATE": "1"}):
            _, calls, records = _run(self.wrapped, messages=[])
        self.assertIsNone(calls[0]["allowed_memory_types"])
        self.assertEqual(records, [])


class GuardTests(unittest.TestCase):
    def _module(self):
        cls = type("SessionCompressorV3", (), {"_extract_user_memories": _original})
        registry = types.SimpleNamespace(
            list_names=lambda include_disabled=False: ALL_TYPES
        )
        return cls, types.SimpleNamespace(
            SessionCompressorV3=cls, create_default_registry=lambda: registry
        )

    def test_guards_and_idempotence(self):
        cls, module = self._module()
        ov = types.SimpleNamespace(__version__="v0.4.99")
        with (
            patch.dict(sys.modules, {"openviking": ov}),
            patch.dict(os.environ, {}, clear=True),
        ):
            self.assertFalse(gate.apply(module))
            ov.__version__ = gate.EXPECTED_VERSION
            self.assertFalse(gate.apply(module))
            digest = gate.hashlib.sha256(
                gate.inspect.getsource(_original).encode()
            ).hexdigest()
            with patch.object(gate, "EXPECTED_SHA256", digest):
                with patch.dict(os.environ, {"OV_PREF_GATE": "0"}):
                    self.assertFalse(gate.apply(module))
                self.assertTrue(gate.apply(module))
                installed = cls._extract_user_memories
                self.assertTrue(gate.apply(module))
                self.assertIs(cls._extract_user_memories, installed)
                self.assertTrue(getattr(installed, "_ov_pref_gate_patch", False))
                with patch.dict(os.environ, {"OV_PREF_GATE": "1"}):
                    _, calls, _ = _run(installed, SUBAGENT_ARCHIVE)
                self.assertNotIn("preferences", calls[0]["allowed_memory_types"])
                self.assertIn("profile", calls[0]["allowed_memory_types"])

    def test_missing_target_is_not_applied(self):
        ov = types.SimpleNamespace(__version__=gate.EXPECTED_VERSION)
        with patch.dict(sys.modules, {"openviking": ov}):
            self.assertFalse(gate.apply(types.SimpleNamespace()))
            cls, _ = self._module()
            self.assertFalse(gate.apply(types.SimpleNamespace(SessionCompressorV3=cls)))


if __name__ == "__main__":
    unittest.main()
