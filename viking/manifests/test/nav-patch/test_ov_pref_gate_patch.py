"""Typed-text detection, per-archive gating, modes, pass-through, and guards for ov_pref_gate_patch."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "_scripts" / "lib"))
from git_env import scrub_environ  # noqa: E402

scrub_environ()

import asyncio
import importlib.util
import io
import json
import os
import time
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
    """Stand-in for v0.5.1.1 Phase 2: one extraction per batch, each its own task."""
    if self.resumed_done:
        return  # every batch finished on an earlier attempt
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
    def __init__(
        self,
        extract,
        compressor,
        batch_size=2,
        policy=None,
        reraise=False,
        resumed_done=False,
    ):
        self.resumed_done = resumed_done
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
        asyncio.run(run(session, "t1", ARCHIVE, messages, "m0", "m9", None))
    return compressor.calls, _records(stderr)


class _Phase2Applied(unittest.TestCase):
    def setUp(self):
        state = patch.dict(gate._state, {"phase2_applied": True})
        state.start()
        self.addCleanup(state.stop)
        # The image sets both switches; each test sets the ones it means.
        switches = patch.dict(os.environ)
        switches.start()
        self.addCleanup(switches.stop)
        for var in gate.SWITCHES.values():
            os.environ.pop(var, None)


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
            {"assistant": 3, "user": 2, "user_agent_text": 0, "user_text": 1},
        )


SIDE_NOTE = (
    "Here is a note offered by a side agent:\n\n> Heads up · The daily backup plan …\n\n"
    "This is how Claude Code surfaces messages the user sends mid-turn — …"
)
MIDTURN = "The user sent a new message while you were working:\n{}\n\n" + (
    "This is how Claude Code surfaces messages the user sends mid-turn — within the "
    "running turn, often alongside the next tool result. Address the message above."
)
AGENT_TEXTS = {
    "side-agent note": SIDE_NOTE,
    "wrapped side-agent note": MIDTURN.format(SIDE_NOTE),
    "coordinator": "The coordinator sent a message while you were working: round 2 …",
    "cross-session": "Another Claude session sent a message: <cross-session-message>…",
    "task notification": "<task-notification> <task-id>b9j</task-id> …</task-notification>",
    "system reminder": '<system-reminder id="d82e"> Another session …</system-reminder>',
    "skill body": "Base directory for this skill: /Users/noot/.claude/skills/retro\n# Retro",
    "compaction summary": "This session is being continued from a previous conversation …",
    "session marker": "[session cc-005f52c6-599b-4f2e · tmux config:3 · run claude-00]",
    "interrupt": "[Request interrupted by user]",
    "tool loaded": "Tool loaded.",
    "command output": "<local-command-stdout>session running</local-command-stdout>",
    "bang output": "<bash-stdout>(no output)</bash-stdout><bash-stderr></bash-stderr>",
}


class AgentTextTests(unittest.TestCase):
    """IMPR-1479 row 2: agent and harness text delivered as a user message."""

    def test_each_inventoried_shape_is_not_typed(self):
        for name, text in AGENT_TEXTS.items():
            with self.subTest(name):
                self.assertFalse(gate.is_typed_text(text))
                self.assertFalse(gate.has_typed_user_text([_msg("user", _text(text))]))

    def test_what_the_user_wrote_still_counts(self):
        for text in (
            "keep going",
            MIDTURN.format("B2"),
            "<bash-input>chmod 600 key.txt</bash-input>",
            '<system-reminder id="x">ctx</system-reminder>\nmerge it',
            "is the side agent's note right?",
            # Round 1: typed words that quote the wrapper footer stay typed.
            MIDTURN.format(
                "This is how Claude Code surfaces messages the user sends mid-turn"
                " — keep this wording in my profile."
            ),
        ):
            with self.subTest(text):
                self.assertTrue(gate.is_typed_text(text))

    def test_nested_harness_tags_are_still_harness_text(self):
        # Round 1: output that quotes its own tag is judged on the whole part.
        text = "<bash-stdout>example: <bash-stdout>daily</bash-stdout></bash-stdout>"
        self.assertFalse(gate.is_typed_text(text))

    def test_the_shape_decides_a_mixed_part(self):
        # Decision A2: whole-part checks only, so the part's shape decides it whatever
        # it holds. Harness text followed by other text is typed ...
        text = "<system-reminder>context</system-reminder>\n" + MIDTURN.format(
            SIDE_NOTE
        )
        self.assertTrue(gate.is_typed_text(text))
        # ... and a part bookended by harness tags is not, even with typed words inside.
        text = "<system-reminder>a</system-reminder>\nkeep going\n<system-reminder>b</system-reminder>"
        self.assertFalse(gate.is_typed_text(text))

    def test_long_parts_are_judged_in_linear_time(self):
        # Round 1: 64k spaces inside the wrapper took 14 s with the regex rule.
        texts = [
            MIDTURN.format("x" + " " * 200_000 + "y"),
            "<system-reminder>" * 50_000,
            "<bash-stdout>" + " " * 200_000,
            "<command-name>" + "<command-args>" * 50_000 + "</command-args>",
        ]
        start = time.perf_counter()
        for text in texts:
            gate.is_typed_text(text)
        self.assertLess(time.perf_counter() - start, 0.5)

    def test_a_bare_command_marker_is_not_typed(self):
        # Decision C2: the three shapes recent archives hold. Only argument text counts.
        for text in (
            "<command-message>claim-check</command-message>\n<command-name>/claim-check</command-name>",
            "<command-name>/clear</command-name>\n            <command-message>clear</command-message>\n"
            "            <command-args></command-args>",
            "<command-message>retro</command-message> <command-name>/retro</command-name>"
            " <command-args>  \n </command-args>",
        ):
            with self.subTest(text):
                self.assertFalse(gate.is_typed_text(text))
                self.assertFalse(gate.has_typed_user_text([_msg("user", _text(text))]))

    def test_command_arguments_are_typed(self):
        for text in (
            "<command-message>research-codebase</command-message>\n"
            "<command-name>/research-codebase</command-name>\n<command-args>P2 and P3</command-args>",
            "<command-name>/claim-check</command-name><command-args>the plan</command-args>"
            "<command-message>claim-check</command-message>",
            # Malformed args (no close tag) count as typed rather than guess.
            "<command-name>/x</command-name><command-args>half</command-name>",
            # Typed text around a marker makes the part typed (A2's mixed-part rule).
            "<command-name>/clear</command-name>\nthen redo the plan",
            "do this first <command-name>/clear</command-name>",
        ):
            with self.subTest(text):
                self.assertTrue(gate.is_typed_text(text))

    def test_replay_of_a_command_marker_archive_is_untyped(self):
        # The shape of cc-db6c5c37 archive_004: a bare /claim-check, then harness text.
        archive = [
            _msg(
                "user",
                _text(
                    "<command-message>claim-check</command-message>\n"
                    "<command-name>/claim-check</command-name>"
                ),
            ),
            _msg(
                "user",
                _text(
                    "Base directory for this skill: /Users/noot/.claude/skills/claim-check"
                ),
            ),
            _msg("assistant", _text("Claims table follows.")),
        ]
        self.assertFalse(gate.has_typed_user_text(archive))
        self.assertEqual(gate.role_counts(archive)["user_text"], 0)
        self.assertEqual(gate.role_counts(archive)["user_agent_text"], 2)

    def test_replay_of_a_side_note_archive_counts_one_typed_message(self):
        # The shape of cc-cc3d043d archive_003: a forwarded note, then "keep going".
        archive = [
            _msg("user", _text(SIDE_NOTE)),
            _msg("assistant", _text("Noted; the backup plan stands.")),
            _msg("user", _text("keep going")),
            _msg("assistant", _text("Continuing.")),
        ]
        self.assertEqual(gate.role_counts(archive)["user_text"], 1)
        self.assertEqual(gate.role_counts(archive)["user_agent_text"], 1)
        self.assertTrue(gate.has_typed_user_text(archive))
        self.assertFalse(gate.has_typed_user_text(archive[:2]))

    def test_an_archive_of_only_agent_text_is_gated(self):
        messages = [_msg("user", _text(t)) for t in AGENT_TEXTS.values()]
        messages.append(_msg("assistant", _text("Done.")))
        with patch.dict(os.environ, {"OV_PREF_GATE": "1", "OV_PROFILE_GATE": "1"}):
            gate_ = gate.Gate(ARCHIVE, "s", messages, gate.read_modes())
        self.assertFalse(gate_.typed)
        self.assertEqual(gate_.roles["user_agent_text"], len(AGENT_TEXTS))


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
                    "profile_action": "would_skip",
                    "roles": {
                        "assistant": 4,
                        "user": 2,
                        "user_agent_text": 0,
                        "user_text": 0,
                    },
                    "session_id": "cc-35c37c51__subagent-ab4c",
                    "writes": {
                        "identity": [],
                        "preferences": [],
                        "profile": ["viking://p/profile.md", "viking://p/2.md"],
                        "soul": [],
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
        with patch.dict(os.environ, {"OV_PREF_GATE": "0", "OV_PROFILE_GATE": "0"}):
            calls, records = _phase(SUBAGENT_ARCHIVE)
        self.assertTrue(all(c["allowed_memory_types"] is None for c in calls))
        self.assertEqual(records, [])

    def test_profile_enforce_removes_both_gated_types(self):
        compressor = _Compressor(
            results=[_result(updates=[("events", "viking://e/1.md")])]
        )
        with patch.dict(os.environ, {"OV_PREF_GATE": "1", "OV_PROFILE_GATE": "1"}):
            calls, records = _phase(SUBAGENT_ARCHIVE, compressor=compressor)
        for call in calls:
            self.assertEqual(
                call["allowed_memory_types"],
                set(ALL_TYPES) - {"preferences", "profile"},
            )
        self.assertEqual(
            [(r["action"], r["profile_action"]) for r in records],
            [("skipped", "skipped")],
        )

    def test_each_switch_acts_alone(self):
        cases = [
            # (OV_PREF_GATE, OV_PROFILE_GATE, removed, action, profile_action)
            ("1", "shadow", {"preferences"}, "skipped", "would_skip"),
            ("shadow", "1", {"profile"}, "would_skip", "skipped"),
            ("0", "1", {"profile"}, "off", "skipped"),
            ("1", "0", {"preferences"}, "skipped", "off"),
        ]
        for pref, prof, removed, action, profile_action in cases:
            env = {"OV_PREF_GATE": pref, "OV_PROFILE_GATE": prof}
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True):
                calls, records = _phase(SUBAGENT_ARCHIVE)
                self.assertEqual(
                    calls[0]["allowed_memory_types"], set(ALL_TYPES) - removed
                )
                self.assertEqual(
                    [(r["action"], r["profile_action"]) for r in records],
                    [(action, profile_action)],
                )

    def test_profile_shadow_alone_logs_without_changing_extraction(self):
        compressor = _Compressor(
            results=[_result(updates=[("profile", "viking://p/profile.md")])]
        )
        env = {"OV_PREF_GATE": "0", "OV_PROFILE_GATE": "shadow"}
        with patch.dict(os.environ, env, clear=True):
            calls, records = _phase(SUBAGENT_ARCHIVE, compressor=compressor)
        self.assertTrue(all(c["allowed_memory_types"] is None for c in calls))
        self.assertEqual(
            [
                (r["action"], r["profile_action"], r["writes"]["profile"])
                for r in records
            ],
            [("off", "would_skip", ["viking://p/profile.md"])],
        )

    def test_unknown_profile_switch_value_means_shadow(self):
        env = {"OV_PREF_GATE": "1", "OV_PROFILE_GATE": "yes"}
        with patch.dict(os.environ, env, clear=True):
            calls, records = _phase(SUBAGENT_ARCHIVE)
        self.assertEqual(
            calls[0]["allowed_memory_types"], set(ALL_TYPES) - {"preferences"}
        )
        self.assertEqual(records[0]["profile_action"], "would_skip")

    def test_typed_archive_passes_through_with_both_switches_on(self):
        with patch.dict(os.environ, {"OV_PREF_GATE": "1", "OV_PROFILE_GATE": "1"}):
            calls, records = _phase(TYPED_ARCHIVE + SUBAGENT_ARCHIVE)
        self.assertTrue(all(c["allowed_memory_types"] is None for c in calls))
        self.assertEqual(records, [])

    def test_identity_and_soul_writes_are_logged_not_gated(self):
        compressor = _Compressor(
            results=[
                _result(
                    adds=[("identity", "viking://p/identity.md")],
                    updates=[("soul", "viking://u/soul.md")],
                )
            ]
        )
        with patch.dict(os.environ, {"OV_PREF_GATE": "1", "OV_PROFILE_GATE": "1"}):
            calls, records = _phase(SUBAGENT_ARCHIVE, compressor=compressor)
        self.assertTrue({"identity", "soul"} <= calls[0]["allowed_memory_types"])
        self.assertEqual(records[0]["writes"]["identity"], ["viking://p/identity.md"])
        self.assertEqual(records[0]["writes"]["soul"], ["viking://u/soul.md"])

    def test_resumed_run_with_nothing_left_to_extract_logs_nothing(self):
        with patch.dict(os.environ, {"OV_PREF_GATE": "1"}):
            calls, records = _phase(SUBAGENT_ARCHIVE, resumed_done=True)
        self.assertEqual(calls, [])
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

    def test_policy_with_profile_but_not_preferences_loses_profile(self):
        with patch.dict(os.environ, {"OV_PREF_GATE": "1", "OV_PROFILE_GATE": "1"}):
            calls, records = _phase(SUBAGENT_ARCHIVE, policy={"events", "profile"})
        self.assertEqual(calls[0]["allowed_memory_types"], {"events"})
        self.assertEqual(
            [(r["action"], r["profile_action"]) for r in records],
            [("not_offered", "skipped")],
        )

    def test_shipped_config_never_reports_an_unoffered_preference_as_skipped(self):
        # Prod's settings with a policy that never held preferences: extraction is
        # unchanged and the line must not add to a Loki count of "action": "skipped".
        env = {"OV_PREF_GATE": "1", "OV_PROFILE_GATE": "shadow"}
        with patch.dict(os.environ, env, clear=True):
            calls, records = _phase(SUBAGENT_ARCHIVE, policy={"events", "profile"})
        self.assertEqual(calls[0]["allowed_memory_types"], {"events", "profile"})
        self.assertEqual(
            [(r["action"], r["profile_action"]) for r in records],
            [("not_offered", "would_skip")],
        )

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
                    session, "t1", ARCHIVE, SUBAGENT_ARCHIVE, "m0", "m9", None
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
        self.assertEqual(
            records[0]["writes"],
            {"identity": [], "preferences": [], "profile": [], "soul": []},
        )

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
            SessionCompressorV3=cls, get_default_registry=lambda: registry
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
                with patch.dict(
                    os.environ, {"OV_PREF_GATE": "0", "OV_PROFILE_GATE": "0"}
                ):
                    self.assertFalse(gate.apply(module))
                    self.assertFalse(gate.apply_session(session_module))
                # One switch on is enough to install the patch (IMPR-1479).
                with patch.dict(
                    os.environ, {"OV_PREF_GATE": "0", "OV_PROFILE_GATE": "shadow"}
                ):
                    self.assertTrue(gate.apply(module))
                    self.assertTrue(gate.apply_session(session_module))
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
                        phase2(session, "t", ARCHIVE, SUBAGENT_ARCHIVE, "a", "b", None)
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


# Skip only when the package is absent; a broken install fails (IMPR-1413).
if importlib.util.find_spec("openviking") is None:  # not in the openviking image
    installed = None
else:
    from openviking.session import compressor_v3 as installed


@unittest.skipIf(installed is None, "needs the openviking v0.5.1.1 image")
class Installed(unittest.TestCase):
    """Both halves applied by sitecustomize to the real v0.5.1.1 targets on import."""

    def test_compressor_half_applied_on_import(self):
        method = installed.SessionCompressorV3._extract_user_memories
        self.assertTrue(getattr(method, "_ov_pref_gate_patch", False))
        self.assertEqual(gate._source_hash(method.__wrapped__), gate.EXPECTED_SHA256)

    def test_phase2_half_applied_on_import(self):
        from openviking.session import session

        method = session.Session._run_memory_extraction
        self.assertTrue(getattr(method, "_ov_pref_gate_patch", False))
        self.assertEqual(
            gate._source_hash(method.__wrapped__), gate.EXPECTED_PHASE2_SHA256
        )
        self.assertTrue(gate._state["phase2_applied"])

    def test_wrappers_bind_the_real_signatures(self):
        from openviking.session import session

        extract = gate.inspect.signature(
            installed.SessionCompressorV3._extract_user_memories
        )
        self.assertTrue(
            {"messages", "session_id", "archive_uri", "allowed_memory_types"}
            <= set(extract.parameters)
        )
        phase2 = gate.inspect.signature(session.Session._run_memory_extraction)
        self.assertTrue({"archive_uri", "messages"} <= set(phase2.parameters))

    def test_the_bundled_registry_offers_preferences_to_remove(self):
        # get_default_registry() needs a server config; load its bundled templates alone.
        from openviking.session.memory import memory_type_registry as mtr

        self.assertIs(installed.get_default_registry, mtr.get_default_registry)
        registry = mtr.MemoryTypeRegistry(load_schemas=False)
        registry.load_from_directory(
            str(mtr.PromptManager._get_bundled_templates_dir() / "memory")
        )
        names = registry.list_names(include_disabled=False)
        self.assertIn(gate.GATED_TYPE, names)
        self.assertIn("profile", names)


if __name__ == "__main__":
    unittest.main()
