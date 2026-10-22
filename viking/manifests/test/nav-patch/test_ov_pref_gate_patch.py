"""Typed-text detection, per-archive gating, modes, pass-through, and guards for ov_pref_gate_patch."""

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
ARCHIVE = "viking://s/history/archive_005"


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
    if self.errors:
        raise self.errors.pop(0)
    return self.results.pop(0) if self.results else types.SimpleNamespace()


async def _phase2(
    self,
    task_id,
    archive_uri,
    messages,
    usage_records,
    first_message_id,
    last_message_id,
    memory_policy,
    agent_evolution_enabled=True,
    agent_memory_skip_reason=None,
    user_config_error=None,
    record_auto_commit_success=False,
    event_search_tags=None,
    auto_commit_policy=None,
):
    """Stand-in for Phase 2: one extraction per batch, each as its own task."""
    for start in range(0, len(messages), self.batch_size):
        batch = messages[start : start + self.batch_size]
        try:
            await asyncio.create_task(
                self.extract(
                    self.compressor,
                    messages=batch,
                    session_id=self.session_id,
                    archive_uri=archive_uri,
                    allowed_memory_types=self.policy,
                )
            )
        except Exception:
            if self.reraise:
                raise


class _Compressor:
    def __init__(self, results=(), errors=()):
        self.calls = []
        self.results = list(results)
        self.errors = list(errors)


class _Session:
    def __init__(self, extract, compressor, batch_size=2, policy=None, reraise=False):
        self.session_id = "cc-35c37c51__subagent-ab4c"
        self.extract = extract
        self.compressor = compressor
        self.batch_size = batch_size
        self.policy = policy
        self.reraise = reraise


def _result(adds=(), updates=()):
    return types.SimpleNamespace(
        memory_diff={
            "operations": {
                "adds": [{"uri": u, "memory_type": t, "after": ""} for t, u in adds],
                "updates": [{"uri": u, "memory_type": t} for t, u in updates],
                "deletes": [],
            }
        },
    )


def _records(stderr):
    return [
        json.loads(line[len(gate.PREFIX) :])
        for line in stderr.getvalue().splitlines()
        if line.startswith(gate.PREFIX)
    ]


def _run(wrapped, *args, compressor=None, **kwargs):
    """A call outside Phase 2."""
    compressor = compressor or _Compressor()
    stderr = io.StringIO()
    with patch.object(gate.sys, "stderr", stderr):
        result = asyncio.run(wrapped(compressor, *args, **kwargs))
    return result, compressor.calls, _records(stderr)


def _phase(messages, compressor=None, **session_kwargs):
    """One archive through Phase 2, batched."""
    compressor = compressor or _Compressor()
    session = _Session(
        gate.gating(_original, lambda: ALL_TYPES), compressor, **session_kwargs
    )
    run = gate.archive_scope(_phase2)
    stderr = io.StringIO()
    with patch.object(gate.sys, "stderr", stderr):
        asyncio.run(run(session, "t1", ARCHIVE, messages, [], "m0", "m9", None))
    return compressor.calls, _records(stderr)


class _Phase2Applied(unittest.TestCase):
    def setUp(self):
        state = patch.dict(gate._state, {"phase2_applied": True})
        state.start()
        self.addCleanup(state.stop)


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


class ArchiveTests(_Phase2Applied):
    def test_typed_text_in_an_early_batch_keeps_preferences_in_every_batch(self):
        messages = TYPED_ARCHIVE + SUBAGENT_ARCHIVE + SUBAGENT_ARCHIVE
        with patch.dict(os.environ, {"OV_PREF_GATE": "1"}):
            calls, records = _phase(messages)
        self.assertEqual(len(calls), 4)
        self.assertTrue(all(c["allowed_memory_types"] is None for c in calls))
        self.assertEqual(records, [])

    def test_gated_archive_skips_every_batch_and_logs_once(self):
        messages = SUBAGENT_ARCHIVE * 2
        compressor = _Compressor(
            results=[
                _result(adds=[("events", "viking://e/1.md")]),
                _result(updates=[("profile", "viking://p/profile.md")]),
                _result(adds=[("profile", "viking://p/2.md")]),
            ]
        )
        with patch.dict(os.environ, {"OV_PREF_GATE": "1"}):
            calls, records = _phase(messages, compressor=compressor)
        self.assertEqual(len(calls), 3)
        for call in calls:
            self.assertEqual(
                call["allowed_memory_types"], set(ALL_TYPES) - {"preferences"}
            )
        self.assertEqual(
            records,
            [
                {
                    "action": "skipped",
                    "archive_uri": ARCHIVE,
                    "diff": True,
                    "extractions": 3,
                    "messages": 6,
                    "roles": {"assistant": 4, "user": 2, "user_text": 0},
                    "session_id": "cc-35c37c51__subagent-ab4c",
                    "writes": {
                        "preferences": [],
                        "profile": ["viking://p/profile.md", "viking://p/2.md"],
                    },
                }
            ],
        )

    def test_shadow_extracts_unchanged_and_records_preference_writes(self):
        for env in ({}, {"OV_PREF_GATE": "shadow"}, {"OV_PREF_GATE": "yes"}):
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True):
                compressor = _Compressor(
                    results=[_result(adds=[("preferences", "viking://pref/relay.md")])]
                )
                calls, records = _phase(SUBAGENT_ARCHIVE, compressor=compressor)
                self.assertTrue(all(c["allowed_memory_types"] is None for c in calls))
                self.assertEqual([r["action"] for r in records], ["would_skip"])
                self.assertEqual(
                    records[0]["writes"]["preferences"], ["viking://pref/relay.md"]
                )

    def test_off_passes_through_silently(self):
        with patch.dict(os.environ, {"OV_PREF_GATE": "0"}):
            calls, records = _phase(SUBAGENT_ARCHIVE)
        self.assertTrue(all(c["allowed_memory_types"] is None for c in calls))
        self.assertEqual(records, [])

    def test_explicit_policy_loses_only_preferences(self):
        with patch.dict(os.environ, {"OV_PREF_GATE": "1"}):
            calls, _ = _phase(
                SUBAGENT_ARCHIVE, policy={"events", "preferences", "profile"}
            )
        self.assertEqual(calls[0]["allowed_memory_types"], {"events", "profile"})

    def test_policy_without_preferences_passes_through(self):
        with patch.dict(os.environ, {"OV_PREF_GATE": "1"}):
            calls, records = _phase(SUBAGENT_ARCHIVE, policy={"events"})
        self.assertEqual(calls[0]["allowed_memory_types"], {"events"})
        self.assertEqual(records, [])

    def test_failed_batch_is_recorded_and_still_logs_once(self):
        compressor = _Compressor(errors=[TimeoutError("vlm")])
        with patch.dict(os.environ, {"OV_PREF_GATE": "1"}):
            calls, records = _phase(SUBAGENT_ARCHIVE, compressor=compressor)
        self.assertEqual(len(calls), 2)
        self.assertEqual(records[0]["errors"], ["TimeoutError"])
        self.assertEqual(records[0]["extractions"], 2)

    def test_phase2_failure_propagates_and_logs(self):
        compressor = _Compressor(errors=[TimeoutError("vlm")])
        with patch.dict(os.environ, {"OV_PREF_GATE": "1"}):
            with self.assertRaises(TimeoutError):
                _phase(SUBAGENT_ARCHIVE, compressor=compressor, reraise=True)

    def test_call_for_another_archive_is_judged_on_its_own(self):
        async def extract(compressor, **kwargs):
            kwargs["archive_uri"] = "viking://s/manual_extract"
            kwargs["messages"] = TYPED_ARCHIVE
            return await gate.gating(_original, lambda: ALL_TYPES)(compressor, **kwargs)

        compressor = _Compressor()
        session = _Session(extract, compressor)
        with patch.dict(os.environ, {"OV_PREF_GATE": "1"}):
            asyncio.run(
                gate.archive_scope(_phase2)(
                    session, "t1", ARCHIVE, SUBAGENT_ARCHIVE, [], "m0", "m9", None
                )
            )
        self.assertEqual(len(compressor.calls), 2)
        self.assertTrue(
            all(c["allowed_memory_types"] is None for c in compressor.calls)
        )


class LoneCallTests(_Phase2Applied):
    def setUp(self):
        super().setUp()
        self.wrapped = gate.gating(_original, lambda: ALL_TYPES)

    def test_enforce_logs_one_line_per_call(self):
        extracted = _result(updates=[("profile", "viking://p/profile.md")])
        with patch.dict(os.environ, {"OV_PREF_GATE": "1"}):
            result, calls, records = _run(
                self.wrapped,
                messages=SUBAGENT_ARCHIVE,
                session_id="s1",
                archive_uri="viking://s/manual_extract",
                allow_self_memory=False,
                compressor=_Compressor(results=[extracted]),
            )
        self.assertIs(result, extracted)
        self.assertEqual(
            calls[0]["allowed_memory_types"], set(ALL_TYPES) - {"preferences"}
        )
        self.assertFalse(calls[0]["allow_self_memory"])
        self.assertIs(calls[0]["messages"], SUBAGENT_ARCHIVE)
        self.assertEqual(
            [(r["action"], r["extractions"], r["writes"]["profile"]) for r in records],
            [("skipped", 1, ["viking://p/profile.md"])],
        )

    def test_result_without_a_diff_still_logs(self):
        with patch.dict(os.environ, {"OV_PREF_GATE": "1"}):
            _, _, records = _run(self.wrapped, SUBAGENT_ARCHIVE)
        self.assertFalse(records[0]["diff"])
        self.assertEqual(records[0]["writes"], {"preferences": [], "profile": []})

    def test_failed_extraction_logs_and_reraises(self):
        compressor = _Compressor(errors=[TimeoutError("vlm")])
        with patch.dict(os.environ, {"OV_PREF_GATE": "1"}):
            with self.assertRaises(TimeoutError):
                _run(self.wrapped, messages=SUBAGENT_ARCHIVE, compressor=compressor)
        self.assertNotIn("preferences", compressor.calls[0]["allowed_memory_types"])

    def test_typed_and_empty_calls_pass_through(self):
        with patch.dict(os.environ, {"OV_PREF_GATE": "1"}):
            for messages in (TYPED_ARCHIVE, []):
                _, calls, records = _run(self.wrapped, messages=messages)
                self.assertIsNone(calls[0]["allowed_memory_types"])
                self.assertEqual(records, [])

    def test_without_the_phase2_half_nothing_is_gated(self):
        with (
            patch.dict(gate._state, {"phase2_applied": False}),
            patch.dict(os.environ, {"OV_PREF_GATE": "1"}),
        ):
            _, calls, records = _run(self.wrapped, messages=SUBAGENT_ARCHIVE)
        self.assertIsNone(calls[0]["allowed_memory_types"])
        self.assertEqual(records, [])


class GuardTests(unittest.TestCase):
    def setUp(self):
        state = patch.dict(gate._state, {"phase2_applied": False})
        state.start()
        self.addCleanup(state.stop)

    def _compressor_module(self):
        cls = type("SessionCompressorV3", (), {"_extract_user_memories": _original})
        registry = types.SimpleNamespace(
            list_names=lambda include_disabled=False: ALL_TYPES
        )
        return cls, types.SimpleNamespace(
            SessionCompressorV3=cls, create_default_registry=lambda: registry
        )

    @staticmethod
    def _digest(fn):
        return gate.hashlib.sha256(gate.inspect.getsource(fn).encode()).hexdigest()

    def test_guards_and_idempotence(self):
        cls, module = self._compressor_module()
        session_cls = type("Session", (), {"_run_memory_extraction": _phase2})
        session_module = types.SimpleNamespace(Session=session_cls)
        ov = types.SimpleNamespace(__version__="v0.4.99")
        with (
            patch.dict(sys.modules, {"openviking": ov}),
            patch.dict(os.environ, {}, clear=True),
        ):
            self.assertFalse(gate.apply(module))
            self.assertFalse(gate.apply_session(session_module))
            ov.__version__ = gate.EXPECTED_VERSION
            self.assertFalse(gate.apply(module))
            self.assertFalse(gate.apply_session(session_module))
            self.assertFalse(gate._state["phase2_applied"])
            with (
                patch.object(gate, "EXPECTED_SHA256", self._digest(_original)),
                patch.object(gate, "EXPECTED_PHASE2_SHA256", self._digest(_phase2)),
            ):
                with patch.dict(os.environ, {"OV_PREF_GATE": "0"}):
                    self.assertFalse(gate.apply(module))
                    self.assertFalse(gate.apply_session(session_module))
                self.assertTrue(gate.apply(module))
                self.assertTrue(gate.apply_session(session_module))
                self.assertTrue(gate._state["phase2_applied"])
                installed = cls._extract_user_memories
                phase2 = session_cls._run_memory_extraction
                self.assertTrue(gate.apply(module))
                self.assertTrue(gate.apply_session(session_module))
                self.assertIs(cls._extract_user_memories, installed)
                self.assertIs(session_cls._run_memory_extraction, phase2)
                session = _Session(installed, _Compressor())
                with patch.dict(os.environ, {"OV_PREF_GATE": "1"}):
                    asyncio.run(
                        phase2(
                            session, "t", ARCHIVE, SUBAGENT_ARCHIVE, [], "a", "b", None
                        )
                    )
                call = session.compressor.calls[0]
                self.assertNotIn("preferences", call["allowed_memory_types"])
                self.assertIn("profile", call["allowed_memory_types"])

    def test_missing_target_is_not_applied(self):
        ov = types.SimpleNamespace(__version__=gate.EXPECTED_VERSION)
        with patch.dict(sys.modules, {"openviking": ov}):
            self.assertFalse(gate.apply(types.SimpleNamespace()))
            cls, _ = self._compressor_module()
            self.assertFalse(gate.apply(types.SimpleNamespace(SessionCompressorV3=cls)))
            self.assertFalse(gate.apply_session(types.SimpleNamespace()))


if __name__ == "__main__":
    unittest.main()
