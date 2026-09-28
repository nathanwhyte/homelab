"""Tests for ov-recall-shadow-report.py. Pure (stdlib fixtures only) — runs anywhere:

    python3 test-ov-recall-shadow-report.py

``testdata/ov-recall-shadow-sample.txt`` (``.txt``, not ``.log``: the repo ignores ``*.log``) is a recorded sample log fixture: two records
in the real Phase 1 shape (no ``archive`` field — what a production log has today), one
``{"skipped": "prechunked"}`` line, unrelated pod-log noise, and four forward-looking
records carrying an ``archive`` field (not emitted by Phase 1 today; see the module
docstring) that exercise the ledger-join machinery: a parent session, its subagent, a
lab-user session on a different machine's ledger, and a session that matches no ledger.
``testdata/ledger-{pop,workbook}.jsonl`` are ledger fixtures in the real corrected row
shape (``start`` + ``session`` events joined by ``launch_id``).
"""

import collections
import importlib.util
import io
import json
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_MODULE_PATH = os.path.join(_HERE, "ov-recall-shadow-report.py")
_TESTDATA = os.path.join(_HERE, "testdata")

_SPEC = importlib.util.spec_from_file_location("ov_recall_shadow_report", _MODULE_PATH)
report = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(report)


class ParseShadowLinesTests(unittest.TestCase):
    def test_unrelated_lines_are_skipped(self):
        lines = [
            "2026-09-28T00:00:00Z stdout F INFO:openviking:session committed\n",
            'not json at all ov-recall-shadow {"broken"\n',
        ]
        self.assertEqual(report.parse_shadow_lines(lines), [])

    def test_a_shadow_line_is_parsed_regardless_of_its_log_prefix(self):
        lines = [
            '2026-09-28T00:00:00.1Z stderr F WARNING:x: ov-recall-shadow {"verdict": "strip"}\n'
        ]
        self.assertEqual(report.parse_shadow_lines(lines), [{"verdict": "strip"}])

    def test_a_skip_line_is_excluded(self):
        lines = ['x ov-recall-shadow {"skipped": "prechunked"}\n']
        self.assertEqual(report.parse_shadow_lines(lines), [])


class RichWrappedRecordTests(unittest.TestCase):
    """The prod server logs through a rich handler at 80 columns: the header line carries
    ``WARNING  ov-recall-shadow`` and the file:line column, and the JSON is folded into a
    narrow message column on the lines after it. ``testdata/ov-recall-shadow-rich-a2.txt``
    is the four real records from the 2026-09-28 A2 smoke test, pulled from Loki with
    neighbouring noise; before this fix the parser returned none of them."""

    def setUp(self):
        with open(os.path.join(_TESTDATA, "ov-recall-shadow-rich-a2.txt")) as fh:
            self.records = report.parse_shadow_lines(fh.readlines())

    def test_every_wrapped_record_is_reassembled(self):
        self.assertEqual(len(self.records), 4)
        for rec in self.records:
            self.assertIn(rec["verdict"], ("strip", "keep-mixed", "keep-ambiguous"))
            self.assertIn("first_message_id", rec)
            self.assertIsInstance(rec["message_count"], int)

    def test_folded_keys_rejoin_without_a_space(self):
        # rich folds `assistant_chars_after` mid-word across two lines
        self.assertTrue(all("assistant_chars_after" in rec for rec in self.records))

    def test_a_header_without_a_timestamp_starts_its_own_record(self):
        # the second record of a same-second pair has a blank time column (the header
        # starts at column 20, not 29), so it must end the first record, not extend it
        with open(os.path.join(_TESTDATA, "ov-recall-shadow-rich-a2.txt")) as fh:
            lines = fh.readlines()
        first_pair = lines[: next(i for i, ln in enumerate(lines) if "15:21:13" in ln)]
        self.assertEqual(len(report.parse_shadow_lines(first_pair)), 2)

    def test_a_record_cut_short_is_skipped(self):
        lines = [
            "[09/28/26 15:20:54] WARNING  ov-recall-shadow       ov_memory_guard_patch.py:969\n",
            '                             {"verdict":                                        \n',
            "[09/28/26 15:20:55] INFO     unrelated                              server.py:1\n",
        ]
        self.assertEqual(report.parse_shadow_lines(lines), [])

    def test_the_payload_may_start_on_the_header_line(self):
        lines = [
            '[09/28/26 15:20:54] WARNING  ov-recall-shadow {"ver ov_memory_guard_patch.py:969\n',
            '                             dict": "strip"}                                    \n',
        ]
        self.assertEqual(report.parse_shadow_lines(lines), [{"verdict": "strip"}])

    def test_single_line_records_still_parse_next_to_wrapped_ones(self):
        with open(os.path.join(_TESTDATA, "ov-recall-shadow-rich-a2.txt")) as fh:
            lines = fh.readlines()
        lines.append(
            '2026-09-28T00:00:00Z WARNING:x: ov-recall-shadow {"verdict": "strip"}\n'
        )
        self.assertEqual(len(report.parse_shadow_lines(lines)), 5)


class BareSessionUuidTests(unittest.TestCase):
    def test_a_parent_session_archive(self):
        self.assertEqual(
            report.bare_session_uuid(
                "viking://user/noot-pilot/sessions/cc-a5b818a0-c729-4063-abdd-efa1eceb5522/history/archive_003"
            ),
            "a5b818a0-c729-4063-abdd-efa1eceb5522",
        )

    def test_a_subagent_archive_strips_to_the_parent_uuid(self):
        self.assertEqual(
            report.bare_session_uuid(
                "viking://user/noot-pilot/sessions/cc-a5b818a0-c729-4063-abdd-efa1eceb5522__subagent-77aa/history/archive_001"
            ),
            "a5b818a0-c729-4063-abdd-efa1eceb5522",
        )

    def test_none_and_no_sessions_segment(self):
        self.assertIsNone(report.bare_session_uuid(None))
        self.assertIsNone(
            report.bare_session_uuid("viking://resources/compendium/x.md")
        )


class LabOrPilotTests(unittest.TestCase):
    def test_lab_user(self):
        self.assertEqual(
            report.lab_or_pilot(
                "viking://user/noot-pilot-lab/sessions/cc-x/history/archive_001"
            ),
            "lab",
        )

    def test_pilot_user(self):
        self.assertEqual(
            report.lab_or_pilot(
                "viking://user/noot-pilot/sessions/cc-x/history/archive_001"
            ),
            "pilot",
        )

    def test_other_user_and_missing(self):
        self.assertEqual(
            report.lab_or_pilot("viking://user/someone-else/sessions/cc-x"), "unknown"
        )
        self.assertEqual(report.lab_or_pilot(None), "unknown")
        self.assertEqual(report.lab_or_pilot(""), "unknown")


class IndexLedgerTests(unittest.TestCase):
    """Codex #173 finding 4: sessions[uuid] is the full launch history (a list), not
    just the earliest launch -- a resumed session needs every launch's window."""

    def test_start_and_session_rows_join_by_launch_id(self):
        rows = report.parse_ledger_rows(os.path.join(_TESTDATA, "ledger-pop.jsonl"))
        starts, sessions = report.index_ledger(rows)
        launches = sessions["a5b818a0-c729-4063-abdd-efa1eceb5522"]
        self.assertEqual(len(launches), 1)
        entry = launches[0]
        self.assertEqual(entry["launch_id"], "20260927T184306Z-25126")
        self.assertEqual(entry["ts"], "2026-09-27T18:43:07Z")
        self.assertIsNone(entry["end_ts"])
        self.assertEqual(starts[entry["launch_id"]]["backend"], "anthropic")
        self.assertEqual(starts[entry["launch_id"]]["user"], "noot-pilot")

    def test_a_session_row_with_no_start_row_is_indexed_but_unresolvable(self):
        rows = [
            {
                "event": "session",
                "launch_id": "L",
                "session_id": "u1",
                "ts": "2026-09-27T00:00:00Z",
            }
        ]
        starts, sessions = report.index_ledger(rows)
        self.assertEqual(
            sessions["u1"],
            [{"launch_id": "L", "ts": "2026-09-27T00:00:00Z", "end_ts": None}],
        )
        self.assertNotIn("L", starts)

    def test_the_earliest_ts_wins_within_one_launch_across_resume_and_compact_rows(
        self,
    ):
        rows = [
            {
                "event": "session",
                "launch_id": "L",
                "session_id": "u1",
                "ts": "2026-09-27T10:00:00Z",
                "source": "resume",
            },
            {
                "event": "session",
                "launch_id": "L",
                "session_id": "u1",
                "ts": "2026-09-27T08:00:00Z",
                "source": "startup",
            },
        ]
        _, sessions = report.index_ledger(rows)
        self.assertEqual(len(sessions["u1"]), 1, "one launch, not two")
        self.assertEqual(sessions["u1"][0]["ts"], "2026-09-27T08:00:00Z")

    def test_a_resumed_session_keeps_every_launch_not_just_the_first(self):
        rows = report.parse_ledger_rows(os.path.join(_TESTDATA, "ledger-resume.jsonl"))
        starts, sessions = report.index_ledger(rows)
        launches = sessions["bbbbbbbb-1111-2222-3333-444444444444"]
        self.assertEqual(len(launches), 2)
        # sorted by ts: the ollama launch first, the anthropic resume second
        self.assertEqual(launches[0]["launch_id"], "20260910T090000Z-1")
        self.assertEqual(launches[1]["launch_id"], "20260915T090000Z-2")
        self.assertEqual(
            starts[launches[0]["launch_id"]]["backend"], "ollama:qwen3-coder"
        )
        self.assertEqual(starts[launches[1]["launch_id"]]["backend"], "anthropic")

    def test_a_real_end_row_closes_every_session_of_its_launch(self):
        # The launcher writes one end row per launch, keyed by launch_id only, with
        # no session_id: {event, launch_id, ts, mode, exit} (ov-pilot.sh, end line).
        rows = [
            {
                "event": "session",
                "launch_id": "L",
                "session_id": "u1",
                "ts": "2026-09-27T08:00:00Z",
            },
            {
                "event": "session",
                "launch_id": "L",
                "session_id": "u2",
                "ts": "2026-09-27T08:30:00Z",
                "source": "clear",
            },
            {
                "event": "end",
                "launch_id": "L",
                "ts": "2026-09-27T09:00:00Z",
                "mode": "recall",
                "exit": 0,
            },
        ]
        _, sessions = report.index_ledger(rows)
        self.assertEqual(sessions["u1"][0]["end_ts"], "2026-09-27T09:00:00Z")
        self.assertEqual(sessions["u2"][0]["end_ts"], "2026-09-27T09:00:00Z")

    def test_a_launch_without_an_end_row_stays_open(self):
        # A crashed or still-running launch has no end row.
        rows = [
            {
                "event": "session",
                "launch_id": "L",
                "session_id": "u1",
                "ts": "2026-09-27T08:00:00Z",
            }
        ]
        _, sessions = report.index_ledger(rows)
        self.assertIsNone(sessions["u1"][0]["end_ts"])

    def test_a_turn_after_its_only_launch_ended_still_resolves_flagged_outside_window(
        self,
    ):
        # D-4 (Codex #173 review): window matching used to have zero tolerance, so a
        # detached write landing after the launcher's end row (or any clock skew)
        # became "unknown" even though there was only one launch it could possibly be
        # -- nothing to disambiguate against. A single-window session now always
        # resolves; the report flags it "outside-window" instead of discarding it.
        rows = [
            {
                "event": "start",
                "launch_id": "L",
                "ts": "2026-09-27T07:59:59Z",
                "backend": "ollama:deepseek",
                "user": "noot-pilot",
            },
            {
                "event": "session",
                "launch_id": "L",
                "session_id": "aaaaaaaa-0000-0000-0000-000000000001",
                "ts": "2026-09-27T08:00:00Z",
            },
            {"event": "end", "launch_id": "L", "ts": "2026-09-27T09:00:00Z", "exit": 0},
        ]
        ledgers = {"pop": report.index_ledger(rows)}
        archive = (
            "viking://user/noot-pilot/sessions/"
            "cc-aaaaaaaa-0000-0000-0000-000000000001/history/archive_001"
        )
        backend, _machine, _candidates, status = report.backend_for(
            archive, ledgers, "2026-09-27T08:15:00Z"
        )
        self.assertEqual((backend, status), ("ollama:deepseek", "fit"))
        backend, _machine, _candidates, status = report.backend_for(
            archive, ledgers, "2026-09-27T09:30:00Z"
        )
        self.assertEqual((backend, status), ("ollama:deepseek", "outside-window"))

    def test_rows_without_a_launch_id_are_ignored(self):
        starts, sessions = report.index_ledger(
            [{"event": "start", "backend": "anthropic"}]
        )
        self.assertEqual((starts, sessions), ({}, {}))


class BackendForTests(unittest.TestCase):
    def setUp(self):
        self.ledgers = report.load_ledgers(
            [
                f"pop={os.path.join(_TESTDATA, 'ledger-pop.jsonl')}",
                f"workbook={os.path.join(_TESTDATA, 'ledger-workbook.jsonl')}",
            ]
        )

    def test_no_archive_is_unknown(self):
        self.assertEqual(
            report.backend_for(None, self.ledgers), ("unknown", None, None, "unknown")
        )

    def test_a_matching_parent_session_resolves_through_pop(self):
        archive = "viking://user/noot-pilot/sessions/cc-a5b818a0-c729-4063-abdd-efa1eceb5522/history/archive_003"
        self.assertEqual(
            report.backend_for(archive, self.ledgers),
            ("anthropic", "pop", None, "fit"),
        )

    def test_a_matching_subagent_resolves_through_its_parents_launch(self):
        archive = "viking://user/noot-pilot/sessions/cc-a5b818a0-c729-4063-abdd-efa1eceb5522__subagent-77aa/history/archive_001"
        self.assertEqual(
            report.backend_for(archive, self.ledgers),
            ("anthropic", "pop", None, "fit"),
        )

    def test_a_workbook_session_resolves_through_workbook(self):
        archive = "viking://user/noot-pilot-lab/sessions/cc-11111111-2222-3333-4444-555555555555/history/archive_001"
        self.assertEqual(
            report.backend_for(archive, self.ledgers),
            ("ollama:qwen3-coder", "workbook", None, "fit"),
        )

    def test_an_unmatched_session_is_unknown_not_dropped(self):
        archive = "viking://user/noot-pilot/sessions/cc-99999999-0000-1111-2222-333344445555/history/archive_002"
        self.assertEqual(
            report.backend_for(archive, self.ledgers),
            ("unknown", None, None, "unknown"),
        )

    def test_created_at_min_is_accepted_and_does_not_change_a_single_window_case(self):
        archive = "viking://user/noot-pilot/sessions/cc-a5b818a0-c729-4063-abdd-efa1eceb5522/history/archive_003"
        self.assertEqual(
            report.backend_for(
                archive, self.ledgers, created_at_min="2026-09-27T19:00:00+00:00"
            ),
            ("anthropic", "pop", None, "fit"),
        )


class ResumeFixtureTests(unittest.TestCase):
    """Codex #173 finding 4: an ollama->anthropic resume. backend_for must pick the
    launch window the turn's created_at_min actually falls in, not stick to the
    session's first launch."""

    def setUp(self):
        self.ledgers = report.load_ledgers(
            [f"pop={os.path.join(_TESTDATA, 'ledger-resume.jsonl')}"]
        )
        self.archive = "viking://user/noot-pilot/sessions/cc-bbbbbbbb-1111-2222-3333-444444444444/history/archive_005"

    def test_a_turn_in_the_first_window_gets_the_ollama_backend(self):
        backend, machine, candidates, status = report.backend_for(
            self.archive, self.ledgers, created_at_min="2026-09-12T00:00:00+00:00"
        )
        self.assertEqual(
            (backend, machine, candidates, status),
            ("ollama:qwen3-coder", "pop", None, "fit"),
        )

    def test_a_turn_after_the_resume_gets_the_anthropic_backend(self):
        # Regression: an earlier version kept only the session's first (earliest)
        # launch and reported ollama:qwen3-coder here too.
        backend, machine, candidates, status = report.backend_for(
            self.archive, self.ledgers, created_at_min="2026-09-20T00:00:00+00:00"
        )
        self.assertEqual(
            (backend, machine, candidates, status), ("anthropic", "pop", None, "fit")
        )

    def test_a_turn_before_any_launch_is_unknown(self):
        backend, machine, candidates, status = report.backend_for(
            self.archive, self.ledgers, created_at_min="2026-09-01T00:00:00+00:00"
        )
        self.assertEqual(
            (backend, machine, candidates, status), ("unknown", None, None, "unknown")
        )

    def test_no_created_at_min_with_two_windows_is_ambiguous(self):
        backend, machine, candidates, status = report.backend_for(
            self.archive, self.ledgers
        )
        self.assertEqual(backend, "ambiguous")
        self.assertIsNone(machine)
        self.assertEqual(status, "ambiguous")
        self.assertEqual(
            candidates,
            [
                {"machine": "pop", "backend": "ollama:qwen3-coder"},
                {"machine": "pop", "backend": "anthropic"},
            ],
        )


class WindowSlackTests(unittest.TestCase):
    """D-4 (Codex #173 review): window matching had zero tolerance, but created_at is
    the server's receive time, not the plugin's capture time -- a detached write, a
    replayed pending queue, or ordinary cluster/machine clock skew can land a turn just
    outside its true window. --window-slack-seconds makes the tolerance configurable
    (symmetric on both bounds); a single-launch session no longer needs it at all
    (see IndexLedgerTests.test_a_turn_after_its_only_launch_ended..., which covers
    that case)."""

    def setUp(self):
        rows = [
            {
                "event": "start",
                "launch_id": "A",
                "ts": "2026-09-27T09:00:00Z",
                "backend": "ollama:deepseek",
                "user": "noot-pilot",
            },
            {
                "event": "session",
                "launch_id": "A",
                "session_id": "cccccccc-1111-2222-3333-444444444444",
                "ts": "2026-09-27T09:00:01Z",
            },
            {"event": "end", "launch_id": "A", "ts": "2026-09-27T09:30:00Z", "exit": 0},
            {
                "event": "start",
                "launch_id": "B",
                "ts": "2026-09-27T10:00:00Z",
                "backend": "anthropic",
                "user": "noot-pilot",
            },
            {
                "event": "session",
                "launch_id": "B",
                "session_id": "cccccccc-1111-2222-3333-444444444444",
                "ts": "2026-09-27T10:00:01Z",
                "source": "resume",
            },
        ]
        self.ledgers = {"pop": report.index_ledger(rows)}
        self.archive = "viking://user/noot-pilot/sessions/cc-cccccccc-1111-2222-3333-444444444444/history/archive_001"

    def test_without_slack_a_turn_in_the_gap_between_two_closed_launches_is_unknown(
        self,
    ):
        backend, _machine, _candidates, status = report.backend_for(
            self.archive, self.ledgers, created_at_min="2026-09-27T09:35:00+00:00"
        )
        self.assertEqual((backend, status), ("unknown", "unknown"))

    def test_with_slack_the_gap_turn_resolves_to_the_nearer_launch(self):
        backend, machine, _candidates, status = report.backend_for(
            self.archive,
            self.ledgers,
            created_at_min="2026-09-27T09:35:00+00:00",
            window_slack_seconds=600,
        )
        self.assertEqual((backend, machine, status), ("ollama:deepseek", "pop", "fit"))

    def test_slack_can_make_a_near_boundary_turn_ambiguous_between_two_launches(self):
        # Symmetric slack is a real tradeoff, not a free extension: padding both a
        # window's start and its neighbour's end can make a turn that used to resolve
        # singly fit both. This is expected -- ambiguous is still safer than a forced
        # attribution.
        ledgers = report.load_ledgers(
            [f"pop={os.path.join(_TESTDATA, 'ledger-resume.jsonl')}"]
        )
        archive = "viking://user/noot-pilot/sessions/cc-bbbbbbbb-1111-2222-3333-444444444444/history/archive_005"
        _backend, _machine, _candidates, status = report.backend_for(
            archive,
            ledgers,
            created_at_min="2026-09-15T09:00:00+00:00",
            window_slack_seconds=120,
        )
        self.assertEqual(status, "ambiguous")

    def test_window_slack_seconds_is_a_cli_option(self):
        args = report.parse_args(
            [
                "--log",
                os.path.join(_TESTDATA, "ov-recall-shadow-sample.txt"),
                "--window-slack-seconds",
                "45",
            ]
        )
        self.assertEqual(args.window_slack_seconds, 45)

    def test_window_slack_seconds_defaults_to_zero(self):
        args = report.parse_args(
            ["--log", os.path.join(_TESTDATA, "ov-recall-shadow-sample.txt")]
        )
        self.assertEqual(args.window_slack_seconds, 0)

    def test_render_markdown_flags_an_outside_window_attribution(self):
        rows = [
            {
                "event": "start",
                "launch_id": "L",
                "ts": "2026-09-27T07:59:59Z",
                "backend": "ollama:deepseek",
                "user": "noot-pilot",
            },
            {
                "event": "session",
                "launch_id": "L",
                "session_id": "aaaaaaaa-0000-0000-0000-000000000001",
                "ts": "2026-09-27T08:00:00Z",
            },
            {"event": "end", "launch_id": "L", "ts": "2026-09-27T09:00:00Z", "exit": 0},
        ]
        ledgers = {"pop": report.index_ledger(rows)}
        records = [
            {
                "verdict": "strip",
                "archive": (
                    "viking://user/noot-pilot/sessions/"
                    "cc-aaaaaaaa-0000-0000-0000-000000000001/history/archive_001"
                ),
                "first_message_id": "u0",
                "last_message_id": "a1",
                "created_at_min": "2026-09-27T09:30:00.000000+00:00",
            }
        ]
        rep = report.build_report(records, ledgers)
        self.assertEqual(rep["rows"][0]["window_status"], "outside-window")
        text = report.render_markdown(rep)
        self.assertIn("ollama:deepseek (outside-window)", text)


class LaunchWindowsTests(unittest.TestCase):
    def test_two_launches_bound_each_other(self):
        launches = [
            {"launch_id": "L1", "ts": "2026-09-10T09:00:00Z", "end_ts": None},
            {"launch_id": "L2", "ts": "2026-09-15T09:00:00Z", "end_ts": None},
        ]
        windows = report._launch_windows(launches)
        self.assertEqual(
            windows,
            [
                ("2026-09-10T09:00:00Z", "2026-09-15T09:00:00Z", "L1"),
                ("2026-09-15T09:00:00Z", None, "L2"),
            ],
        )

    def test_a_single_launch_is_open_ended(self):
        launches = [{"launch_id": "L1", "ts": "2026-09-10T09:00:00Z", "end_ts": None}]
        self.assertEqual(
            report._launch_windows(launches), [("2026-09-10T09:00:00Z", None, "L1")]
        )

    def test_an_end_ts_before_the_next_launch_closes_the_window_early(self):
        launches = [
            {
                "launch_id": "L1",
                "ts": "2026-09-10T09:00:00Z",
                "end_ts": "2026-09-10T10:00:00Z",
            },
            {"launch_id": "L2", "ts": "2026-09-15T09:00:00Z", "end_ts": None},
        ]
        windows = report._launch_windows(launches)
        self.assertEqual(
            windows[0], ("2026-09-10T09:00:00Z", "2026-09-10T10:00:00Z", "L1")
        )

    def test_an_end_ts_after_the_next_launch_is_ignored_in_favour_of_the_next_launch(
        self,
    ):
        launches = [
            {
                "launch_id": "L1",
                "ts": "2026-09-10T09:00:00Z",
                "end_ts": "2026-09-20T00:00:00Z",
            },
            {"launch_id": "L2", "ts": "2026-09-15T09:00:00Z", "end_ts": None},
        ]
        windows = report._launch_windows(launches)
        self.assertEqual(
            windows[0], ("2026-09-10T09:00:00Z", "2026-09-15T09:00:00Z", "L1")
        )


class CandidateUsersTests(unittest.TestCase):
    """Codex #173 finding 3: the recorded start.user is authoritative -- not a guess
    from the backend. Every existing ollama launch's start row actually records
    "noot-pilot" (ov-pilot.sh:224 hardcodes --arg user noot-pilot regardless of
    --ollama), so the old "ollama backend -> noot-pilot-lab" assumption missed them
    all; noot-pilot-lab is only ever a fallback now."""

    def test_the_recorded_user_is_tried_first_regardless_of_backend(self):
        # This is the real, hardcoded shape: every launch (ollama included) records
        # start.user = "noot-pilot".
        self.assertEqual(
            report._candidate_users("noot-pilot"), ["noot-pilot", "noot-pilot-lab"]
        )

    def test_a_missing_user_falls_back_to_noot_pilot_first(self):
        self.assertEqual(
            report._candidate_users(None), ["noot-pilot", "noot-pilot-lab"]
        )

    def test_noot_pilot_lab_recorded_directly_is_not_duplicated(self):
        self.assertEqual(report._candidate_users("noot-pilot-lab"), ["noot-pilot-lab"])


class SampleFixtureReportTests(unittest.TestCase):
    """The automated criterion: parses a recorded sample log fixture into the expected table."""

    def setUp(self):
        with open(os.path.join(_TESTDATA, "ov-recall-shadow-sample.txt")) as fh:
            self.records = report.parse_shadow_lines(fh.readlines())
        self.ledgers = report.load_ledgers(
            [
                f"pop={os.path.join(_TESTDATA, 'ledger-pop.jsonl')}",
                f"workbook={os.path.join(_TESTDATA, 'ledger-workbook.jsonl')}",
            ]
        )

    def test_noise_and_skip_lines_do_not_become_records(self):
        # 2 real-shape + 4 archive-bearing records; 1 skip line and 2 noise lines excluded.
        self.assertEqual(len(self.records), 6)

    def test_verdict_counts(self):
        rep = report.build_report(self.records, self.ledgers)
        self.assertEqual(
            dict(rep["by_verdict"]), {"strip": 4, "keep-ambiguous": 1, "keep-mixed": 1}
        )

    def test_backend_summary_counts(self):
        # Session identity, not archive identity (Codex #173 finding 5): rec3 (pop
        # parent) and rec4 (its subagent) are the SAME session, so anthropic's
        # session count is 1, not 2. rec1/rec2 have no archive at all and count as
        # unresolved_turns, not pseudo-sessions; rec6's archive is unmatched by any
        # ledger but is still a real resolved session, so unknown's session count is
        # 1 (rec6), with 2 separate unresolved_turns (rec1, rec2).
        rep = report.build_report(self.records, self.ledgers)
        by_backend = rep["by_backend"]
        self.assertEqual(
            {
                k: (len(v["sessions"]), v["turns"], v["strip"], v["unresolved_turns"])
                for k, v in by_backend.items()
            },
            {
                "unknown": (1, 3, 2, 2),
                "anthropic": (1, 2, 1, 0),
                "ollama:qwen3-coder": (1, 1, 1, 0),
            },
        )

    def test_a_matched_row_carries_machine_and_lab_or_pilot(self):
        rep = report.build_report(self.records, self.ledgers)
        matches = [r for r in rep["rows"] if r["machine"] == "workbook"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["lab_or_pilot"], "lab")
        pop_matches = [r for r in rep["rows"] if r["machine"] == "pop"]
        self.assertEqual(len(pop_matches), 2)
        self.assertTrue(all(r["lab_or_pilot"] == "pilot" for r in pop_matches))

    def test_render_markdown_produces_the_expected_table(self):
        rep = report.build_report(self.records, self.ledgers)
        text = report.render_markdown(rep)
        self.assertIn("## Turns by verdict", text)
        self.assertIn("| strip | 4 |", text)
        self.assertIn("| keep-ambiguous | 1 |", text)
        self.assertIn("| keep-mixed | 1 |", text)
        self.assertIn("## Per-backend summary", text)
        self.assertIn("| anthropic | 1 | 2 | 1 | 0 |", text)
        self.assertIn("| ollama:qwen3-coder | 1 | 1 | 1 | 0 |", text)
        self.assertIn("| unknown | 1 | 3 | 2 | 2 |", text)


class MainCliTests(unittest.TestCase):
    def test_main_writes_the_table_to_stdout(self):
        argv = [
            "--log",
            os.path.join(_TESTDATA, "ov-recall-shadow-sample.txt"),
            "--ledger",
            f"pop={os.path.join(_TESTDATA, 'ledger-pop.jsonl')}",
            "--ledger",
            f"workbook={os.path.join(_TESTDATA, 'ledger-workbook.jsonl')}",
        ]
        captured = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            rc = report.main(argv)
        finally:
            sys.stdout = old_stdout
        self.assertEqual(rc, 0)
        self.assertIn("## Turns by verdict", captured.getvalue())

    def test_bad_ledger_spec_is_rejected(self):
        with self.assertRaises(ValueError):
            report.load_ledgers(["not-a-valid-spec"])


_ARCHIVE_TREE = os.path.join(_TESTDATA, "archive-tree")


class ParseTsTests(unittest.TestCase):
    def test_z_suffix_and_offset_suffix_both_parse(self):
        self.assertIsNotNone(report._parse_ts("2026-09-27T18:43:07Z"))
        self.assertIsNotNone(report._parse_ts("2026-09-27T19:00:00.000000+00:00"))

    def test_none_and_garbage_are_none(self):
        self.assertIsNone(report._parse_ts(None))
        self.assertIsNone(report._parse_ts("not a timestamp"))


class CandidateSessionsTests(unittest.TestCase):
    def setUp(self):
        self.ledgers = report.load_ledgers(
            [
                f"pop={os.path.join(_TESTDATA, 'ledger-pop.jsonl')}",
                f"workbook={os.path.join(_TESTDATA, 'ledger-workbook.jsonl')}",
            ]
        )

    def test_no_created_at_min_yields_no_candidates(self):
        self.assertEqual(report._candidate_sessions(self.ledgers, None, 3600), [])

    def test_a_session_starting_after_the_turn_is_excluded(self):
        # pop's session ts is 2026-09-27T18:43:07Z; a turn before that has no eligible
        # candidate from pop's ledger.
        candidates = report._candidate_sessions(
            self.ledgers, "2026-09-27T18:00:00+00:00", 3600
        )
        self.assertNotIn(
            "a5b818a0-c729-4063-abdd-efa1eceb5522", [c[1] for c in candidates]
        )

    def test_closest_preceding_session_sorts_first(self):
        candidates = report._candidate_sessions(
            self.ledgers, "2026-09-27T19:10:00+00:00", 365 * 24 * 3600
        )
        # workbook's session (2026-09-20) is also eligible with a generous slack, but
        # pop's (2026-09-27T18:43:07Z) is closer to the turn and must sort first.
        self.assertEqual(candidates[0][1], "a5b818a0-c729-4063-abdd-efa1eceb5522")

    def test_slack_window_excludes_a_too_distant_session(self):
        candidates = report._candidate_sessions(
            self.ledgers, "2026-09-27T19:10:00+00:00", 3600
        )
        uuids = [c[1] for c in candidates]
        self.assertIn("a5b818a0-c729-4063-abdd-efa1eceb5522", uuids)
        self.assertNotIn("11111111-2222-3333-4444-555555555555", uuids)

    def test_each_candidate_carries_its_launch_recorded_user(self):
        candidates = report._candidate_sessions(
            self.ledgers, "2026-09-27T19:10:00+00:00", 365 * 24 * 3600
        )
        by_uuid = {uuid: user for _ts, uuid, user in candidates}
        # Both testdata ledgers record the real, hardcoded start.user value.
        self.assertEqual(by_uuid["a5b818a0-c729-4063-abdd-efa1eceb5522"], "noot-pilot")
        self.assertEqual(by_uuid["11111111-2222-3333-4444-555555555555"], "noot-pilot")


class LocalTreeReaderTests(unittest.TestCase):
    def setUp(self):
        self.reader = report.LocalTreeReader(_ARCHIVE_TREE)

    def test_list_sessions(self):
        # D-3 fixtures (ArchiveStatusTests / ResolveArchiveTerminalStateTests) and D-7
        # fixtures (ReclassifyRecordTests) added more session dirs under the shared
        # noot-pilot testdata tree.
        self.assertEqual(
            sorted(self.reader.list_sessions("noot-pilot")),
            [
                "cc-11112222-3333-4444-5555-666677778888",
                "cc-99990000-1111-2222-3333-444455556666",
                "cc-a5b818a0-c729-4063-abdd-efa1eceb5522",
                "cc-a5b818a0-c729-4063-abdd-efa1eceb5522__subagent-77aa",
                "cc-bbbbbbbb-1111-2222-3333-444444444444",
                "cc-dddddddd-0000-1111-2222-333344445566",
                "cc-eeeeeeee-0000-1111-2222-333344445577",
                "cc-ffffffff-0000-1111-2222-333344445588",
            ],
        )

    def test_list_sessions_for_an_unknown_user_is_empty(self):
        self.assertEqual(self.reader.list_sessions("nobody"), [])

    def test_list_archives(self):
        self.assertEqual(
            self.reader.list_archives(
                "noot-pilot", "cc-a5b818a0-c729-4063-abdd-efa1eceb5522"
            ),
            ["archive_001"],
        )

    def test_read_messages(self):
        messages = self.reader.read_messages(
            "noot-pilot", "cc-a5b818a0-c729-4063-abdd-efa1eceb5522", "archive_001"
        )
        self.assertEqual([m["id"] for m in messages], ["msg_p1_u0", "msg_p1_a1"])

    def test_read_messages_missing_archive_is_none(self):
        self.assertIsNone(
            self.reader.read_messages("noot-pilot", "cc-missing", "archive_001")
        )

    def test_reads_are_cached(self):
        key = ("noot-pilot", "cc-a5b818a0-c729-4063-abdd-efa1eceb5522", "archive_001")
        first = self.reader.read_messages(*key)
        self.reader._read_cache[key].append({"id": "injected"})
        second = self.reader.read_messages(*key)
        self.assertIs(first, second)


class ArchiveStatusTests(unittest.TestCase):
    """D-3 (Codex #173 review): terminal-state precedence mirrors upstream's own
    ``_archive_terminal_state`` (openviking/session/session.py:3340-3349) -- ``.done``
    checked first (completed), then ``.failed.json`` (failed), else pending."""

    def setUp(self):
        self.reader = report.LocalTreeReader(_ARCHIVE_TREE)

    def test_a_done_marker_is_completed(self):
        self.assertEqual(
            self.reader.archive_status(
                "noot-pilot",
                "cc-dddddddd-0000-1111-2222-333344445566",
                "archive_002",
            ),
            "completed",
        )

    def test_a_failed_marker_is_failed(self):
        self.assertEqual(
            self.reader.archive_status(
                "noot-pilot",
                "cc-dddddddd-0000-1111-2222-333344445566",
                "archive_001",
            ),
            "failed",
        )

    def test_neither_marker_is_pending(self):
        # This fixture archive carries no terminal marker at all (not yet committed).
        self.assertEqual(
            self.reader.archive_status(
                "noot-pilot",
                "cc-ffffffff-0000-1111-2222-333344445588",
                "archive_001",
            ),
            "pending",
        )

    def test_done_takes_precedence_when_both_markers_are_somehow_present(self):
        # Mirrors upstream's own marker order: .done is checked before .failed.json.
        import os as _os

        both_dir = _os.path.join(
            _ARCHIVE_TREE,
            "noot-pilot",
            "sessions",
            "cc-dddddddd-0000-1111-2222-333344445566",
            "history",
            "archive_002",
        )
        self.assertTrue(_os.path.isfile(_os.path.join(both_dir, ".done")))


class ResolveArchiveTerminalStateTests(unittest.TestCase):
    """D-3 (Codex #173 review): a failed archive used to shadow the real one because
    resolve_archive returned on the first `messages.jsonl` match regardless of
    terminal state (BUG-1174's exact shape: subagent archive_001 failed and
    archive_002 held the same message ids). Only completed archives count as a
    match; more than one completed match is reported ambiguous, never first-wins."""

    def setUp(self):
        self.ledgers = report.load_ledgers(
            [f"pop={os.path.join(_TESTDATA, 'ledger-archive-status.jsonl')}"]
        )
        self.reader = report.LocalTreeReader(_ARCHIVE_TREE)

    def test_a_failed_archive_is_skipped_in_favour_of_the_completed_retry(self):
        rec = {
            "first_message_id": "msg_fail_u0",
            "created_at_min": "2026-09-27T20:00:00.000000+00:00",
        }
        self.assertEqual(
            report.resolve_archive(rec, self.ledgers, self.reader),
            "viking://user/noot-pilot/sessions/cc-dddddddd-0000-1111-2222-333344445566/history/archive_002",
        )

    def test_two_completed_matches_are_reported_ambiguous_not_first_wins(self):
        rec = {
            "first_message_id": "msg_ambig_u0",
            "created_at_min": "2026-09-27T21:00:00.000000+00:00",
        }
        # The plain resolve_archive contract ("an archive URI, or None when it cannot
        # be resolved") cannot represent ambiguous-between-two -- it must not silently
        # pick the first one.
        self.assertIsNone(report.resolve_archive(rec, self.ledgers, self.reader))

    def test_the_detailed_resolution_surfaces_ambiguous_matches_and_status(self):
        rec = {
            "first_message_id": "msg_ambig_u0",
            "created_at_min": "2026-09-27T21:00:00.000000+00:00",
        }
        detail = report.resolve_archive_detail(rec, self.ledgers, self.reader)
        self.assertEqual(detail.status, "ambiguous")
        self.assertIsNone(detail.archive)
        self.assertEqual(
            sorted(detail.ambiguous_matches),
            [
                "viking://user/noot-pilot/sessions/cc-eeeeeeee-0000-1111-2222-333344445577/history/archive_001",
                "viking://user/noot-pilot/sessions/cc-eeeeeeee-0000-1111-2222-333344445577/history/archive_002",
            ],
        )

    def test_the_detailed_resolution_reports_a_failed_diagnostic_for_the_skipped_archive(
        self,
    ):
        rec = {
            "first_message_id": "msg_fail_u0",
            "created_at_min": "2026-09-27T20:00:00.000000+00:00",
        }
        detail = report.resolve_archive_detail(rec, self.ledgers, self.reader)
        self.assertEqual(detail.status, "resolved")
        self.assertIn(
            (
                "viking://user/noot-pilot/sessions/cc-dddddddd-0000-1111-2222-333344445566/history/archive_001",
                "failed",
            ),
            detail.diagnostics,
        )

    def test_a_pending_only_match_is_unresolved_with_a_pending_diagnostic(self):
        # This session's archive_001 has messages.jsonl but no .done/.failed.json
        # marker at all (not yet committed) -- still not a completed match.
        rec = {
            "first_message_id": "msg_pending_u0",
            "created_at_min": "2026-09-27T22:00:00.000000+00:00",
        }
        detail = report.resolve_archive_detail(rec, self.ledgers, self.reader)
        self.assertEqual(detail.status, "unresolved")
        self.assertIn(
            (
                "viking://user/noot-pilot/sessions/cc-ffffffff-0000-1111-2222-333344445588/history/archive_001",
                "pending",
            ),
            detail.diagnostics,
        )


class BuildReportArchiveStatusTests(unittest.TestCase):
    """D-3, end to end: the report row surfaces the archive's resolution status."""

    def setUp(self):
        self.ledgers = report.load_ledgers(
            [f"pop={os.path.join(_TESTDATA, 'ledger-archive-status.jsonl')}"]
        )
        self.reader = report.LocalTreeReader(_ARCHIVE_TREE)

    def test_a_resolved_row_reports_resolved_status(self):
        records = [
            {
                "verdict": "strip",
                "first_message_id": "msg_fail_u0",
                "last_message_id": "msg_fail_a1",
                "created_at_min": "2026-09-27T20:00:00.000000+00:00",
            }
        ]
        rep = report.build_report(records, self.ledgers, reader=self.reader)
        row = rep["rows"][0]
        self.assertEqual(row["archive_status"], "resolved")
        self.assertTrue(row["archive"].endswith("archive_002"))

    def test_an_ambiguous_row_reports_ambiguous_status_and_candidates(self):
        records = [
            {
                "verdict": "strip",
                "first_message_id": "msg_ambig_u0",
                "last_message_id": "msg_ambig_a1",
                "created_at_min": "2026-09-27T21:00:00.000000+00:00",
            }
        ]
        rep = report.build_report(records, self.ledgers, reader=self.reader)
        row = rep["rows"][0]
        self.assertEqual(row["archive_status"], "ambiguous")
        self.assertIsNone(row["archive"])
        self.assertEqual(len(row["archive_ambiguous_matches"]), 2)
        text = report.render_markdown(rep)
        self.assertIn("ambiguous", text)


class ResolveArchiveTests(unittest.TestCase):
    def setUp(self):
        self.ledgers = report.load_ledgers(
            [
                f"pop={os.path.join(_TESTDATA, 'ledger-pop.jsonl')}",
                f"workbook={os.path.join(_TESTDATA, 'ledger-workbook.jsonl')}",
            ]
        )
        self.reader = report.LocalTreeReader(_ARCHIVE_TREE)

    def test_resolves_a_parent_session_turn(self):
        rec = {
            "first_message_id": "msg_p1_u0",
            "created_at_min": "2026-09-27T19:00:00.000000+00:00",
        }
        self.assertEqual(
            report.resolve_archive(rec, self.ledgers, self.reader),
            "viking://user/noot-pilot/sessions/cc-a5b818a0-c729-4063-abdd-efa1eceb5522/history/archive_001",
        )

    def test_resolves_a_subagent_turn_through_its_parents_launch(self):
        rec = {
            "first_message_id": "msg_sub_u0",
            "created_at_min": "2026-09-27T19:05:00.000000+00:00",
        }
        self.assertEqual(
            report.resolve_archive(rec, self.ledgers, self.reader),
            "viking://user/noot-pilot/sessions/cc-a5b818a0-c729-4063-abdd-efa1eceb5522__subagent-77aa/history/archive_001",
        )

    def test_resolves_a_lab_session_turn_through_the_workbook_ledger(self):
        rec = {
            "first_message_id": "msg_lab_u0",
            "created_at_min": "2026-09-20T09:30:00.000000+00:00",
        }
        self.assertEqual(
            report.resolve_archive(rec, self.ledgers, self.reader),
            "viking://user/noot-pilot-lab/sessions/cc-11111111-2222-3333-4444-555555555555/history/archive_001",
        )

    def test_an_id_in_no_archive_is_unresolved(self):
        rec = {
            "first_message_id": "msg_nowhere",
            "created_at_min": "2026-09-27T19:10:00.000000+00:00",
        }
        self.assertIsNone(report.resolve_archive(rec, self.ledgers, self.reader))

    def test_no_created_at_min_is_unresolved(self):
        self.assertIsNone(
            report.resolve_archive(
                {"first_message_id": "msg_p1_u0"}, self.ledgers, self.reader
            )
        )

    def test_no_first_message_id_is_unresolved(self):
        rec = {"created_at_min": "2026-09-27T19:00:00.000000+00:00"}
        self.assertIsNone(report.resolve_archive(rec, self.ledgers, self.reader))


class BuildReportWithReaderTests(unittest.TestCase):
    """End to end: records with no explicit `archive` field resolve through the reader."""

    def setUp(self):
        self.ledgers = report.load_ledgers(
            [
                f"pop={os.path.join(_TESTDATA, 'ledger-pop.jsonl')}",
                f"workbook={os.path.join(_TESTDATA, 'ledger-workbook.jsonl')}",
            ]
        )
        self.reader = report.LocalTreeReader(_ARCHIVE_TREE)

    def test_a_record_with_no_archive_field_resolves_backend_via_the_reader(self):
        records = [
            {
                "verdict": "strip",
                "first_message_id": "msg_p1_u0",
                "last_message_id": "msg_p1_a1",
                "created_at_min": "2026-09-27T19:00:00.000000+00:00",
                "created_at_max": "2026-09-27T19:00:05.000000+00:00",
            }
        ]
        rep = report.build_report(records, self.ledgers, reader=self.reader)
        self.assertEqual(rep["rows"][0]["backend"], "anthropic")
        self.assertEqual(rep["rows"][0]["machine"], "pop")
        self.assertEqual(rep["rows"][0]["lab_or_pilot"], "pilot")

    def test_an_unresolvable_record_still_reports_unknown_not_dropped(self):
        records = [
            {
                "verdict": "strip",
                "first_message_id": "msg_nowhere",
                "created_at_min": "2026-09-27T19:10:00.000000+00:00",
            }
        ]
        rep = report.build_report(records, self.ledgers, reader=self.reader)
        self.assertEqual(rep["rows"][0]["backend"], "unknown")
        self.assertEqual(len(rep["rows"]), 1)

    def test_without_a_reader_records_with_no_archive_field_stay_unknown(self):
        records = [
            {
                "verdict": "strip",
                "first_message_id": "msg_p1_u0",
                "created_at_min": "2026-09-27T19:00:00.000000+00:00",
            }
        ]
        rep = report.build_report(records, self.ledgers, reader=None)
        self.assertEqual(rep["rows"][0]["backend"], "unknown")


class BuildReportResumeFixtureTests(unittest.TestCase):
    """End to end (resolve_archive + backend_for's launch windows) against the
    ollama->anthropic resume fixture -- Codex #173 finding 4."""

    def setUp(self):
        self.ledgers = report.load_ledgers(
            [f"pop={os.path.join(_TESTDATA, 'ledger-resume.jsonl')}"]
        )
        self.reader = report.LocalTreeReader(_ARCHIVE_TREE)

    def test_a_turn_before_the_resume_resolves_the_ollama_backend(self):
        records = [
            {
                "verdict": "strip",
                "first_message_id": "msg_resume_l1_u0",
                "last_message_id": "msg_resume_l1_a1",
                "created_at_min": "2026-09-10T12:00:00.000000+00:00",
                "created_at_max": "2026-09-10T12:00:05.000000+00:00",
            }
        ]
        rep = report.build_report(records, self.ledgers, reader=self.reader)
        self.assertEqual(rep["rows"][0]["backend"], "ollama:qwen3-coder")
        self.assertIsNone(rep["rows"][0]["ambiguous_candidates"])

    def test_a_turn_after_the_resume_resolves_the_anthropic_backend(self):
        records = [
            {
                "verdict": "strip",
                "first_message_id": "msg_resume_l2_u0",
                "last_message_id": "msg_resume_l2_a1",
                "created_at_min": "2026-09-15T12:00:00.000000+00:00",
                "created_at_max": "2026-09-15T12:00:05.000000+00:00",
            }
        ]
        rep = report.build_report(records, self.ledgers, reader=self.reader)
        self.assertEqual(rep["rows"][0]["backend"], "anthropic")
        self.assertIsNone(rep["rows"][0]["ambiguous_candidates"])


class MaxCandidatesTests(unittest.TestCase):
    """D-2 (Codex #173 review): the candidate cap silently dropped the true session
    when six other launches started closer to the turn -- and the cap could not be
    raised from the CLI. Default is now unlimited; --max-candidates opts into a
    narrower, faster search and unresolved records report how much of the search
    space was actually covered."""

    def _rows(self, n_fake, real_ts):
        rows = []
        for i in range(n_fake):
            lid = f"fake-{i}"
            ts = f"2026-09-27T18:5{i}:00Z"
            rows.append(
                {
                    "event": "start",
                    "launch_id": lid,
                    "ts": ts,
                    "backend": "anthropic",
                    "user": "noot-pilot",
                }
            )
            rows.append(
                {
                    "event": "session",
                    "launch_id": lid,
                    "session_id": f"fake-session-{i}",
                    "ts": ts,
                }
            )
        rows.append(
            {
                "event": "start",
                "launch_id": "real",
                "ts": real_ts,
                "backend": "anthropic",
                "user": "noot-pilot",
            }
        )
        rows.append(
            {
                "event": "session",
                "launch_id": "real",
                "session_id": "a5b818a0-c729-4063-abdd-efa1eceb5522",
                "ts": real_ts,
            }
        )
        return rows

    def setUp(self):
        self.reader = report.LocalTreeReader(_ARCHIVE_TREE)
        rows = self._rows(6, "2026-09-27T18:43:07Z")
        self.ledgers = {"pop": report.index_ledger(rows)}
        self.rec = {
            "first_message_id": "msg_p1_u0",
            "created_at_min": "2026-09-27T19:00:00.000000+00:00",
        }

    def test_the_default_cap_no_longer_drops_a_seventh_candidate(self):
        self.assertEqual(
            report.resolve_archive(self.rec, self.ledgers, self.reader),
            "viking://user/noot-pilot/sessions/cc-a5b818a0-c729-4063-abdd-efa1eceb5522/history/archive_001",
        )

    def test_an_explicit_max_candidates_can_still_narrow_the_search(self):
        self.assertIsNone(
            report.resolve_archive(
                self.rec, self.ledgers, self.reader, max_candidates=5
            )
        )

    def test_an_unresolved_record_reports_incomplete_candidate_coverage_when_capped(
        self,
    ):
        records = [dict(self.rec, verdict="strip")]
        rep = report.build_report(
            records, self.ledgers, reader=self.reader, max_candidates=5
        )
        row = rep["rows"][0]
        self.assertEqual(row["backend"], "unknown")
        self.assertEqual(row["candidate_coverage"], (5, 7))

    def test_full_coverage_reports_no_gap(self):
        records = [dict(self.rec, verdict="strip")]
        rep = report.build_report(records, self.ledgers, reader=self.reader)
        row = rep["rows"][0]
        self.assertIsNone(row["candidate_coverage"])

    def test_max_candidates_is_a_cli_option(self):
        args = report.parse_args(
            [
                "--log",
                os.path.join(_TESTDATA, "ov-recall-shadow-sample.txt"),
                "--max-candidates",
                "12",
            ]
        )
        self.assertEqual(args.max_candidates, 12)

    def test_max_candidates_defaults_to_unlimited(self):
        args = report.parse_args(
            ["--log", os.path.join(_TESTDATA, "ov-recall-shadow-sample.txt")]
        )
        self.assertIsNone(args.max_candidates)


class DedupCanonicalizationTests(unittest.TestCase):
    """D-1 (Codex #173 review, 2026-09-27): a retried extraction re-logs the identical
    turn. Dedup must key on (archive, first_message_id, last_message_id, message_count)
    after a verified archive join -- not on the raw line, and not on a process-local
    seen-set that would lose the occurrence/conflict diagnostics."""

    def setUp(self):
        self.ledgers = report.load_ledgers(
            [f"pop={os.path.join(_TESTDATA, 'ledger-pop.jsonl')}"]
        )
        self.base = {
            "archive": "viking://user/noot-pilot/sessions/cc-a5b818a0-c729-4063-abdd-efa1eceb5522/history/archive_003",
            "verdict": "strip",
            "turn_start": 0,
            "turn_end": 2,
            "first_message_id": "u0",
            "last_message_id": "a1",
            "message_count": 2,
            "ov_tools": ["mcp__plugin_openviking-memory_openviking__read"],
            "other_tools": [],
        }

    def test_an_exact_repeat_collapses_to_one_turn(self):
        records = [dict(self.base), dict(self.base)]
        rep = report.build_report(records, self.ledgers)
        self.assertEqual(dict(rep["by_verdict"]), {"strip": 1})
        self.assertEqual(len(rep["rows"]), 1)
        self.assertEqual(rep["rows"][0]["occurrences"], 2)

    def test_a_retry_with_shifted_turn_bounds_still_collapses(self):
        # Batch apply can concatenate several requests' lists, shifting turn_start/
        # turn_end so the lines are not byte-identical -- the join key is message ids
        # + message_count, not the raw turn range.
        shifted = dict(self.base)
        shifted["turn_start"] = 4
        shifted["turn_end"] = 6
        records = [dict(self.base), shifted]
        rep = report.build_report(records, self.ledgers)
        self.assertEqual(len(rep["rows"]), 1)
        self.assertEqual(rep["rows"][0]["occurrences"], 2)

    def test_conflicting_verdicts_across_duplicates_are_preserved_not_hidden(self):
        conflicting = dict(self.base)
        conflicting["verdict"] = "keep-ambiguous"
        records = [dict(self.base), conflicting]
        rep = report.build_report(records, self.ledgers)
        self.assertEqual(len(rep["rows"]), 1)
        self.assertEqual(rep["rows"][0]["occurrences"], 2)
        self.assertEqual(
            rep["rows"][0]["duplicate_verdicts"], ["keep-ambiguous", "strip"]
        )

    def test_a_different_message_count_is_not_a_duplicate(self):
        different = dict(self.base)
        different["message_count"] = 3
        records = [dict(self.base), different]
        rep = report.build_report(records, self.ledgers)
        self.assertEqual(len(rep["rows"]), 2)
        self.assertEqual(dict(rep["by_verdict"]), {"strip": 2})

    def test_unresolved_records_are_never_deduplicated_against_each_other(self):
        # No archive to join on -- canonicalization only applies "after a verified
        # archive join" (Codex answer 1); two distinct unresolved turns must not
        # collapse just because they share no identity.
        unresolved = dict(self.base)
        del unresolved["archive"]
        records = [dict(unresolved), dict(unresolved)]
        rep = report.build_report(records, self.ledgers)
        self.assertEqual(len(rep["rows"]), 2)
        self.assertEqual(dict(rep["by_verdict"]), {"strip": 2})


class ParseArchiveUriTests(unittest.TestCase):
    def test_a_well_formed_archive_uri(self):
        self.assertEqual(
            report.parse_archive_uri(
                "viking://user/noot-pilot/sessions/cc-a5b8/history/archive_003"
            ),
            {
                "user": "noot-pilot",
                "session_dir": "cc-a5b8",
                "archive_id": "archive_003",
            },
        )

    def test_none_and_malformed_are_none(self):
        self.assertIsNone(report.parse_archive_uri(None))
        self.assertIsNone(report.parse_archive_uri(""))
        self.assertIsNone(
            report.parse_archive_uri("viking://resources/compendium/x.md")
        )


class ExtractSummaryTests(unittest.TestCase):
    def test_the_line_after_a_summary_heading(self):
        content = "# Summary\nThe widget ships in cobalt blue.\n\n# ChatLog:\n..."
        self.assertEqual(
            report._extract_summary(content), "The widget ships in cobalt blue."
        )

    def test_truncates_a_long_line(self):
        content = "# Summary\n" + ("x" * 300)
        self.assertEqual(len(report._extract_summary(content, max_chars=160)), 160)

    def test_no_summary_heading_falls_back_to_the_first_non_blank_line(self):
        content = "\n\nfirst real line\nsecond line"
        self.assertEqual(report._extract_summary(content), "first real line")

    def test_empty_content_is_none(self):
        self.assertIsNone(report._extract_summary(""))
        self.assertIsNone(report._extract_summary(None))


class DiffEventsTests(unittest.TestCase):
    def test_the_real_nested_shape(self):
        diff = {
            "operations": {
                "adds": [
                    {
                        "uri": "viking://user/u/memories/events/2026/09/27/widget_color_decided.md",
                        "memory_type": "events",
                        "after": "# Summary\nThe widget ships in cobalt blue.\n",
                    }
                ],
                "updates": [],
                "deletes": [],
            }
        }
        events = report._diff_events(diff)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["name"], "widget_color_decided")
        self.assertEqual(events[0]["memory_type"], "events")
        self.assertEqual(events[0]["operation"], "add")
        self.assertEqual(events[0]["abstract"], "The widget ships in cobalt blue.")

    def test_adds_and_updates_are_both_included(self):
        diff = {
            "operations": {
                "adds": [
                    {"uri": "viking://x/a.md", "memory_type": "events", "after": "A"}
                ],
                "updates": [
                    {"uri": "viking://x/b.md", "memory_type": "entities", "after": "B"}
                ],
                "deletes": [],
            }
        }
        events = report._diff_events(diff)
        names = {e["name"] for e in events}
        self.assertEqual(names, {"a", "b"})
        by_name = {e["name"]: e["operation"] for e in events}
        self.assertEqual(by_name, {"a": "add", "b": "update"})

    def test_deletes_are_included_and_typed(self):
        # D-6 (Codex #173 review): _diff_events silently dropped deletes -- a
        # memory_type x add/update/delete operation was only two-thirds typed.
        diff = {
            "operations": {
                "adds": [],
                "updates": [],
                "deletes": [
                    {
                        "uri": "viking://x/c.md",
                        "memory_type": "preferences",
                        "before": "# Summary\nStale preference removed.\n",
                    }
                ],
            }
        }
        events = report._diff_events(diff)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["name"], "c")
        self.assertEqual(events[0]["memory_type"], "preferences")
        self.assertEqual(events[0]["operation"], "delete")
        # A delete has no "after" -- the abstract falls back to "before".
        self.assertEqual(events[0]["abstract"], "Stale preference removed.")

    def test_none_and_non_dict_and_empty_yield_no_events(self):
        self.assertEqual(report._diff_events(None), [])
        self.assertEqual(report._diff_events("not a dict"), [])
        self.assertEqual(report._diff_events({}), [])

    def test_a_flat_legacy_shape_is_still_accepted(self):
        diff = {"adds": [{"uri": "viking://x/a.md", "after": "A"}], "updates": []}
        self.assertEqual(len(report._diff_events(diff)), 1)


class EventsForArchiveTests(unittest.TestCase):
    def setUp(self):
        self.reader = report.LocalTreeReader(_ARCHIVE_TREE)

    def test_reads_the_real_fixture_memory_diff(self):
        archive = "viking://user/noot-pilot/sessions/cc-a5b818a0-c729-4063-abdd-efa1eceb5522/history/archive_001"
        events = report._events_for_archive(archive, self.reader)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["name"], "widget_color_decided")
        self.assertEqual(events[0]["abstract"], "The widget ships in cobalt blue.")

    def test_no_archive_or_no_reader_yields_no_events(self):
        self.assertEqual(report._events_for_archive(None, self.reader), [])
        self.assertEqual(
            report._events_for_archive(
                "viking://user/noot-pilot/sessions/cc-a5b818a0-c729-4063-abdd-efa1eceb5522/history/archive_001",
                None,
            ),
            [],
        )

    def test_an_archive_with_no_memory_diff_file_yields_no_events(self):
        # The subagent archive fixture has messages.jsonl but no memory_diff.json.
        archive = "viking://user/noot-pilot/sessions/cc-a5b818a0-c729-4063-abdd-efa1eceb5522__subagent-77aa/history/archive_001"
        self.assertEqual(report._events_for_archive(archive, self.reader), [])


class RenderMarkdownTurnsTableTests(unittest.TestCase):
    """Codex #173 finding 1: a per-turn table (machine, backend, session, archive,
    turn range, verdict, tools, produced events, empty manual-label column), with the
    aggregate summary kept as a separate section."""

    def setUp(self):
        self.ledgers = report.load_ledgers(
            [f"pop={os.path.join(_TESTDATA, 'ledger-pop.jsonl')}"]
        )
        self.reader = report.LocalTreeReader(_ARCHIVE_TREE)

    def test_a_resolved_turn_shows_its_produced_event(self):
        records = [
            {
                "verdict": "strip",
                "turn_start": 0,
                "turn_end": 2,
                "first_message_id": "msg_p1_u0",
                "last_message_id": "msg_p1_a1",
                "created_at_min": "2026-09-27T19:00:00.000000+00:00",
                "created_at_max": "2026-09-27T19:00:05.000000+00:00",
                "ov_tools": ["mcp__plugin_openviking-memory_openviking__read"],
                "other_tools": [],
            }
        ]
        rep = report.build_report(records, self.ledgers, reader=self.reader)
        text = report.render_markdown(rep)
        self.assertIn("## Turns", text)
        self.assertIn("## Aggregate summary", text)
        # the per-turn table appears before the aggregate section
        self.assertLess(text.index("## Turns"), text.index("## Aggregate summary"))
        self.assertIn("widget_color_decided", text)
        self.assertIn("The widget ships in cobalt blue.", text)
        self.assertIn("a5b818a0-c729-4063-abdd-efa1eceb5522", text)
        self.assertIn("anthropic", text)
        self.assertIn("pop", text)
        # the manual-label column header is present and the cell is empty (an em dash)
        self.assertIn("Label (restatement / new-info / mixed)", text)

    def test_an_unresolved_turn_still_gets_a_row(self):
        records = [
            {
                "verdict": "keep-mixed",
                "turn_start": 4,
                "turn_end": 6,
                "first_message_id": "msg_nowhere",
                "last_message_id": "msg_nowhere2",
                "ov_tools": [],
                "other_tools": ["mutating"],
            }
        ]
        rep = report.build_report(records, self.ledgers, reader=self.reader)
        text = report.render_markdown(rep)
        self.assertIn("keep-mixed", text)
        self.assertIn("unknown", text)


class SharedArchiveEventsTests(unittest.TestCase):
    """D-6 (Codex #173 review): an archive's memory_diff is an archive-level diff, not
    attributed to any one turn -- when more than one turn's shadow record resolves to
    the same archive (a batched/segmented extraction, or several turns before the next
    commit), the report must say so plainly rather than implying each turn caused it."""

    def setUp(self):
        self.ledgers = report.load_ledgers(
            [f"pop={os.path.join(_TESTDATA, 'ledger-pop.jsonl')}"]
        )
        self.reader = report.LocalTreeReader(_ARCHIVE_TREE)
        self.archive = "viking://user/noot-pilot/sessions/cc-a5b818a0-c729-4063-abdd-efa1eceb5522/history/archive_001"

    def test_two_turns_resolving_to_the_same_archive_are_flagged_shared(self):
        records = [
            {
                "verdict": "strip",
                "first_message_id": "msg_p1_u0",
                "last_message_id": "msg_p1_a1",
                "message_count": 2,
                "created_at_min": "2026-09-27T19:00:00.000000+00:00",
            },
            {
                "verdict": "strip",
                # Different message ids/count than the row above -- not a dedup
                # collapse, a second, genuinely distinct turn joined to the same
                # archive.
                "first_message_id": "msg_p1_u0",
                "last_message_id": "msg_p1_a1",
                "message_count": 3,
                "created_at_min": "2026-09-27T19:00:00.000000+00:00",
            },
        ]
        rep = report.build_report(records, self.ledgers, reader=self.reader)
        for row in rep["rows"]:
            self.assertTrue(row["events_shared_archive"])
        text = report.render_markdown(rep)
        self.assertIn("not attributed to a single turn", text)

    def test_a_single_turn_archive_is_not_flagged_shared(self):
        records = [
            {
                "verdict": "strip",
                "first_message_id": "msg_p1_u0",
                "last_message_id": "msg_p1_a1",
                "created_at_min": "2026-09-27T19:00:00.000000+00:00",
            }
        ]
        rep = report.build_report(records, self.ledgers, reader=self.reader)
        self.assertFalse(rep["rows"][0]["events_shared_archive"])
        text = report.render_markdown(rep)
        self.assertNotIn("not attributed to a single turn", text)


class ClassifyToolNameTests(unittest.TestCase):
    """C-2/C-4 downstream (Codex #173 review): the canonical classifier lives here
    once; the production shadow classifier (ov_memory_guard_patch.py::classify_tool)
    mirrors it. TodoWrite is explicitly neutral; SendMessage/Write/Edit are explicitly
    mutating; the create/update/delete/send/write/publish verb heuristic applies only
    to mcp__-prefixed names; a failed OpenViking read is its own classification, never
    folded into a clean "strip"-eligible ov_read."""

    def test_an_ov_read_tool_is_ov_read(self):
        self.assertEqual(
            report.classify_tool_name("mcp__plugin_openviking-memory_openviking__read"),
            "ov_read",
        )

    def test_an_errored_ov_read_is_its_own_classification(self):
        self.assertEqual(
            report.classify_tool_name(
                "mcp__plugin_openviking-memory_openviking__read", tool_status="error"
            ),
            "ov_read_error",
        )

    def test_todo_write_is_neutral_read_only(self):
        self.assertEqual(report.classify_tool_name("TodoWrite"), "read_only")

    def test_send_message_write_and_edit_are_explicitly_mutating(self):
        for name in ("SendMessage", "Write", "Edit"):
            self.assertEqual(report.classify_tool_name(name), "mutating")

    def test_the_verb_heuristic_only_applies_to_mcp_prefixed_names(self):
        self.assertEqual(
            report.classify_tool_name("mcp__some_plugin__create_thing"), "mutating"
        )
        # An unprefixed tool that happens to contain a verb-like substring must not
        # be misclassified -- the heuristic is scoped to mcp__ names only.
        self.assertEqual(report.classify_tool_name("Updater"), "unknown")

    def test_unknown_and_empty_name(self):
        self.assertEqual(report.classify_tool_name("SomeRandomTool"), "unknown")
        self.assertEqual(report.classify_tool_name(None), "unknown")
        self.assertEqual(report.classify_tool_name(""), "unknown")

    def test_read_only_names(self):
        for name in ("ToolSearch", "Read", "Glob", "Grep", "LS"):
            self.assertEqual(report.classify_tool_name(name), "read_only")


class DeriveVerdictFromClassificationsTests(unittest.TestCase):
    def test_mutating_wins_keep_mixed(self):
        self.assertEqual(
            report._derive_verdict_from_classifications(["ov_read", "mutating"]),
            "keep-mixed",
        )

    def test_unknown_without_mutating_is_keep_ambiguous(self):
        self.assertEqual(
            report._derive_verdict_from_classifications(["ov_read", "unknown"]),
            "keep-ambiguous",
        )

    def test_an_errored_ov_read_is_never_strip(self):
        self.assertEqual(
            report._derive_verdict_from_classifications(["ov_read_error"]),
            "keep-ambiguous",
        )

    def test_clean_ov_read_only_is_strip(self):
        self.assertEqual(
            report._derive_verdict_from_classifications(["ov_read", "read_only"]),
            "strip",
        )


class ReclassifyRecordTests(unittest.TestCase):
    """D-7 / Codex additional finding 1 (2026-09-27): a fragment can claim
    partial:false and a clean strip verdict while its tail (a later mutating tool
    call) was split into a separate extraction batch and never logged. Reconstruct the
    complete source turn from the archive's raw messages.jsonl and re-derive the
    verdict; only report a reconstructed_verdict when it actually differs."""

    def setUp(self):
        self.reader = report.LocalTreeReader(_ARCHIVE_TREE)
        self.archive = "viking://user/noot-pilot/sessions/cc-11112222-3333-4444-5555-666677778888/history/archive_001"

    def test_a_fragment_missing_its_mutating_tail_is_reclassified(self):
        # The logged record only saw msg_frag_u0..msg_frag_a1 (an ov_read, clean
        # strip); the archive's true turn continues through msg_frag_a2, an
        # untracked Write, before the next user boundary (msg_frag_u3).
        rec = {
            "verdict": "strip",
            "partial": False,
            "first_message_id": "msg_frag_u0",
            "last_message_id": "msg_frag_a1",
        }
        self.assertEqual(
            report.reclassify_record(rec, self.archive, self.reader), "keep-mixed"
        )

    def test_a_fragment_whose_full_turn_was_logged_reclassifies_to_the_same_verdict(
        self,
    ):
        # The caller only surfaces reconstructed_verdict when it differs; the full
        # turn is still re-derived, so C-2/C-4 corrections reach records that covered
        # their whole turn.
        rec = {
            "verdict": "keep-mixed",
            "partial": False,
            "first_message_id": "msg_frag_u0",
            "last_message_id": "msg_frag_a2",
        }
        self.assertEqual(
            report.reclassify_record(rec, self.archive, self.reader), "keep-mixed"
        )

    def test_no_reader_or_no_archive_yields_no_reclassification(self):
        rec = {"first_message_id": "msg_frag_u0", "last_message_id": "msg_frag_a1"}
        self.assertIsNone(report.reclassify_record(rec, None, self.reader))
        self.assertIsNone(report.reclassify_record(rec, self.archive, None))

    def test_an_unresolvable_message_id_yields_no_reclassification(self):
        rec = {"first_message_id": "msg_nowhere", "last_message_id": "msg_nowhere2"}
        self.assertIsNone(report.reclassify_record(rec, self.archive, self.reader))

    def test_forward_compat_other_tool_names_is_preferred_over_reconstruction(self):
        # C-2/C-4 downstream: when the emitter already sends other_tool_names (a new,
        # forward-compatible field), reclassify from it directly -- no archive read
        # needed at all.
        rec = {
            "verdict": "strip",
            "first_message_id": "msg_frag_u0",
            "last_message_id": "msg_frag_a1",
            "ov_tools": ["mcp__plugin_openviking-memory_openviking__read"],
            "other_tool_names": ["Write"],
        }
        self.assertEqual(report.reclassify_record(rec, None, None), "keep-mixed")

    def test_emitter_errored_and_unresolved_fields_are_honored(self):
        # homelab#174's emitter sends errored / unresolved_ov_reads, not ov_read_error
        base = {
            "verdict": "strip",
            "first_message_id": "msg_frag_u0",
            "last_message_id": "msg_frag_a1",
            "ov_tools": ["mcp__plugin_openviking-memory_openviking__read"],
            "other_tool_names": [],
        }
        for extra in ({"errored": True}, {"unresolved_ov_reads": 1}):
            rec = {**base, **extra}
            self.assertEqual(
                report.reclassify_record(rec, None, None), "keep-ambiguous", extra
            )
        self.assertEqual(report.reclassify_record(base, None, None), "strip")

    def test_forward_compat_ov_read_error_flag_is_honored(self):
        rec = {
            "verdict": "strip",
            "first_message_id": "msg_frag_u0",
            "last_message_id": "msg_frag_a1",
            "ov_tools": ["mcp__plugin_openviking-memory_openviking__read"],
            "other_tool_names": [],
            "ov_read_error": True,
        }
        self.assertEqual(report.reclassify_record(rec, None, None), "keep-ambiguous")


class ReclassifyRealArchiveShapeTests(unittest.TestCase):
    """C-4 in real archives: the assistant's tool part stays "running" and the outcome
    is on the user-side result part carrying the same tool_id (checked on pop over 60
    live archives, 2026-09-27). Records from the homelab#173 emitter lack
    other_tool_names, so these are re-derived from the archive."""

    def setUp(self):
        self.reader = report.LocalTreeReader(_ARCHIVE_TREE)
        self.archive = "viking://user/noot-pilot/sessions/cc-99990000-1111-2222-3333-444455556666/history/archive_001"

    def rec(self, first, last, verdict):
        return {"verdict": verdict, "first_message_id": first, "last_message_id": last}

    def test_a_failed_ov_read_found_by_tool_id_is_not_strip(self):
        got = report.reclassify_record(
            self.rec("msg_real_u0", "msg_real_a3", "strip"), self.archive, self.reader
        )
        self.assertEqual(got, "keep-ambiguous")

    def test_todowrite_beside_a_completed_read_is_strip(self):
        # the homelab#173 emitter logged keep-mixed (TodoWrite matched the verb regex)
        got = report.reclassify_record(
            self.rec("msg_real_u4", "msg_real_a7", "keep-mixed"),
            self.archive,
            self.reader,
        )
        self.assertEqual(got, "strip")

    def test_an_ov_read_with_no_outcome_in_the_turn_is_not_strip(self):
        got = report.reclassify_record(
            self.rec("msg_real_u8", "msg_real_a9", "strip"), self.archive, self.reader
        )
        self.assertEqual(got, "keep-ambiguous")


class BuildReportReconstructionTests(unittest.TestCase):
    """D-7, end to end: the row surfaces partial and reconstructed_verdict."""

    def setUp(self):
        self.ledgers = report.load_ledgers(
            [f"pop={os.path.join(_TESTDATA, 'ledger-reconstruction.jsonl')}"]
        )
        self.reader = report.LocalTreeReader(_ARCHIVE_TREE)

    def test_a_reclassified_row_carries_the_reconstructed_verdict(self):
        records = [
            {
                "verdict": "strip",
                "partial": False,
                "first_message_id": "msg_frag_u0",
                "last_message_id": "msg_frag_a1",
                "message_count": 2,
                "created_at_min": "2026-09-27T23:00:00.000000+00:00",
            }
        ]
        rep = report.build_report(records, self.ledgers, reader=self.reader)
        row = rep["rows"][0]
        self.assertEqual(row["partial"], False)
        self.assertEqual(row["reconstructed_verdict"], "keep-mixed")
        text = report.render_markdown(rep)
        self.assertIn("reconstructed", text)

    def test_an_unaffected_row_carries_no_reconstructed_verdict(self):
        # the logged range already includes the Write, so the logged verdict is
        # keep-mixed and reconstruction agrees
        records = [
            {
                "verdict": "keep-mixed",
                "partial": False,
                "first_message_id": "msg_frag_u0",
                "last_message_id": "msg_frag_a2",
                "message_count": 3,
                "created_at_min": "2026-09-27T23:00:00.000000+00:00",
            }
        ]
        rep = report.build_report(records, self.ledgers, reader=self.reader)
        self.assertIsNone(rep["rows"][0]["reconstructed_verdict"])


class _Completed:
    def __init__(self, stdout):
        self.stdout = stdout


class OvCliReaderTests(unittest.TestCase):
    """``subprocess`` is faked; the argv shapes and the ``ov read -o json`` envelope
    match a real read-only invocation (2026-09-28, against real prod session data —
    see the module docstring)."""

    def _fake_run(self, matcher, result):
        calls = []

        def run(argv, capture_output=True, text=True, timeout=None):
            calls.append(argv)
            if matcher(argv):
                return result
            raise AssertionError(f"unexpected ov invocation: {argv}")

        run.calls = calls
        return run

    def test_list_sessions_parses_simple_paths(self):
        # Regression: an earlier draft omitted --user from `ov ls`, which silently
        # listed the CLI's default user's (empty) tree instead of raising -- caught
        # live against real ov-test/prod data, 2026-09-28.
        stdout = (
            "cmd: ov ls viking://user/noot-pilot/sessions -l 256 -n 256 -s\n"
            "viking://user/noot-pilot/sessions/cc-a\n"
            "viking://user/noot-pilot/sessions/cc-b\n"
        )

        def matcher(argv):
            return (
                argv[:2] == ["ov", "ls"] and "--user" in argv and "noot-pilot" in argv
            )

        run = self._fake_run(matcher, _Completed(stdout))
        reader = report.OvCliReader(run=run)
        self.assertEqual(reader.list_sessions("noot-pilot"), ["cc-a", "cc-b"])

    def test_list_sessions_is_cached_per_user(self):
        stdout = "viking://user/noot-pilot/sessions/cc-a\n"
        run = self._fake_run(lambda argv: "--user" in argv, _Completed(stdout))
        reader = report.OvCliReader(run=run)
        reader.list_sessions("noot-pilot")
        reader.list_sessions("noot-pilot")
        self.assertEqual(len(run.calls), 1)
        reader.list_sessions("noot-pilot-lab")
        self.assertEqual(len(run.calls), 2, "a different user must not hit the cache")

    def test_read_messages_parses_the_real_envelope_shape(self):
        # `ov read <uri> --user <user> -o json` -> {"ok": true, "result": "<raw jsonl>"}
        raw_jsonl = '{"id": "msg_a", "role": "user", "parts": []}\n'
        stdout = json.dumps({"ok": True, "result": raw_jsonl})
        run = self._fake_run(
            lambda argv: argv[1] == "read" and "--user" in argv, _Completed(stdout)
        )
        reader = report.OvCliReader(run=run)
        messages = reader.read_messages("noot-pilot", "cc-a", "archive_001")
        self.assertEqual(messages, [{"id": "msg_a", "role": "user", "parts": []}])

    def test_unparseable_output_is_none_not_a_crash(self):
        run = self._fake_run(lambda argv: True, _Completed("not json"))
        reader = report.OvCliReader(run=run)
        self.assertIsNone(reader.read_messages("noot-pilot", "cc-a", "archive_001"))

    def test_archive_status_lists_hidden_markers(self):
        # Regression, caught live 2026-09-28 against prod archive_001 of cc-e3f06e32:
        # `ov ls -s` hides dotfiles, so `.done` was never seen and every real archive
        # read as pending. This fake behaves like the real CLI (dotfiles only with -a),
        # with that archive's real listing: a retried extraction leaves both markers.
        visible = [
            "viking://user/noot-pilot/sessions/cc-a/history/archive_001/memory_diff.json",
            "viking://user/noot-pilot/sessions/cc-a/history/archive_001/messages.jsonl",
        ]
        hidden = [
            "viking://user/noot-pilot/sessions/cc-a/history/archive_001/.done",
            "viking://user/noot-pilot/sessions/cc-a/history/archive_001/.failed.json",
        ]
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            shown = visible + (hidden if "-a" in argv else [])
            return _Completed("cmd: ov ls …\n" + "\n".join(shown) + "\n")

        reader = report.OvCliReader(run=run)
        self.assertEqual(
            reader.archive_status("noot-pilot", "cc-a", "archive_001"), "completed"
        )
        # the session and archive listings keep hidden entries out
        reader.list_archives("noot-pilot", "cc-a")
        self.assertNotIn("-a", calls[-1])

    def test_archive_status_completed_from_a_done_marker(self):
        stdout = (
            "viking://user/noot-pilot/sessions/cc-a/history/archive_001/.done\n"
            "viking://user/noot-pilot/sessions/cc-a/history/archive_001/messages.jsonl\n"
        )
        run = self._fake_run(lambda argv: "--user" in argv, _Completed(stdout))
        reader = report.OvCliReader(run=run)
        self.assertEqual(
            reader.archive_status("noot-pilot", "cc-a", "archive_001"), "completed"
        )

    def test_archive_status_failed_from_a_failed_json_marker(self):
        stdout = (
            "viking://user/noot-pilot/sessions/cc-a/history/archive_001/.failed.json\n"
        )
        run = self._fake_run(lambda argv: "--user" in argv, _Completed(stdout))
        reader = report.OvCliReader(run=run)
        self.assertEqual(
            reader.archive_status("noot-pilot", "cc-a", "archive_001"), "failed"
        )

    def test_archive_status_pending_with_neither_marker(self):
        stdout = "viking://user/noot-pilot/sessions/cc-a/history/archive_001/messages.jsonl\n"
        run = self._fake_run(lambda argv: "--user" in argv, _Completed(stdout))
        reader = report.OvCliReader(run=run)
        self.assertEqual(
            reader.archive_status("noot-pilot", "cc-a", "archive_001"), "pending"
        )


class ModeForTests(unittest.TestCase):
    """D-5 (Codex #173 review): mode is indexed but was never emitted -- mode_for
    mirrors backend_for's own window selection so the two stay consistent for the same
    (archive, created_at_min)."""

    def setUp(self):
        self.ledgers = report.load_ledgers(
            [f"pop={os.path.join(_TESTDATA, 'ledger-pop.jsonl')}"]
        )
        self.archive = "viking://user/noot-pilot/sessions/cc-a5b818a0-c729-4063-abdd-efa1eceb5522/history/archive_003"

    def test_a_single_window_session_resolves_its_mode(self):
        self.assertEqual(report.mode_for(self.archive, self.ledgers), "recall")

    def test_no_archive_is_unknown(self):
        self.assertEqual(report.mode_for(None, self.ledgers), "unknown")

    def test_two_launch_windows_with_no_created_at_min_is_ambiguous(self):
        ledgers = report.load_ledgers(
            [f"pop={os.path.join(_TESTDATA, 'ledger-resume.jsonl')}"]
        )
        archive = "viking://user/noot-pilot/sessions/cc-bbbbbbbb-1111-2222-3333-444444444444/history/archive_005"
        self.assertEqual(report.mode_for(archive, ledgers), "ambiguous")

    def test_two_launch_windows_disambiguated_by_created_at_min(self):
        ledgers = report.load_ledgers(
            [f"pop={os.path.join(_TESTDATA, 'ledger-resume.jsonl')}"]
        )
        archive = "viking://user/noot-pilot/sessions/cc-bbbbbbbb-1111-2222-3333-444444444444/history/archive_005"
        self.assertEqual(
            report.mode_for(
                archive, ledgers, created_at_min="2026-09-12T00:00:00+00:00"
            ),
            "capture",
        )
        self.assertEqual(
            report.mode_for(
                archive, ledgers, created_at_min="2026-09-20T00:00:00+00:00"
            ),
            "recall",
        )


class BuildReportModeTests(unittest.TestCase):
    def test_a_row_carries_its_resolved_mode(self):
        ledgers = report.load_ledgers(
            [f"pop={os.path.join(_TESTDATA, 'ledger-pop.jsonl')}"]
        )
        reader = report.LocalTreeReader(_ARCHIVE_TREE)
        records = [
            {
                "verdict": "strip",
                "first_message_id": "msg_p1_u0",
                "last_message_id": "msg_p1_a1",
                "created_at_min": "2026-09-27T19:00:00.000000+00:00",
            }
        ]
        rep = report.build_report(records, ledgers, reader=reader)
        self.assertEqual(rep["rows"][0]["mode"], "recall")


class FilterRecordsByBlockTests(unittest.TestCase):
    """D-5: --since/--until block scoping."""

    def setUp(self):
        self.records = [
            {"created_at_min": "2026-09-27T00:00:00+00:00", "label": "early"},
            {"created_at_min": "2026-09-27T12:00:00+00:00", "label": "mid"},
            {"created_at_min": "2026-09-27T23:00:00+00:00", "label": "late"},
            {"label": "no-timestamp"},
        ]

    def test_no_bounds_keeps_everything(self):
        self.assertEqual(report.filter_records_by_block(self.records), self.records)

    def test_since_excludes_earlier_records(self):
        kept = report.filter_records_by_block(
            self.records, since="2026-09-27T06:00:00+00:00"
        )
        labels = {r["label"] for r in kept}
        self.assertEqual(labels, {"mid", "late", "no-timestamp"})

    def test_until_excludes_records_at_or_after_the_bound(self):
        kept = report.filter_records_by_block(
            self.records, until="2026-09-27T12:00:00+00:00"
        )
        labels = {r["label"] for r in kept}
        self.assertEqual(labels, {"early", "no-timestamp"})

    def test_a_record_with_no_created_at_min_is_always_kept(self):
        # Nothing to scope it out on -- dropping it would silently hide an
        # unresolvable turn from the block's accounting.
        kept = report.filter_records_by_block(
            self.records,
            since="2026-09-27T00:00:00+00:00",
            until="2026-09-27T00:00:01+00:00",
        )
        labels = {r["label"] for r in kept}
        self.assertIn("no-timestamp", labels)

    def test_since_and_until_narrows_to_the_block(self):
        kept = report.filter_records_by_block(
            self.records,
            since="2026-09-27T06:00:00+00:00",
            until="2026-09-27T18:00:00+00:00",
        )
        labels = {r["label"] for r in kept}
        self.assertEqual(labels, {"mid", "no-timestamp"})

    def test_since_and_until_are_cli_options(self):
        args = report.parse_args(
            [
                "--log",
                os.path.join(_TESTDATA, "ov-recall-shadow-sample.txt"),
                "--since",
                "2026-09-27T00:00:00Z",
                "--until",
                "2026-09-28T00:00:00Z",
            ]
        )
        self.assertEqual(args.since, "2026-09-27T00:00:00Z")
        self.assertEqual(args.until, "2026-09-28T00:00:00Z")


class FilterRowsByLabSessionsTests(unittest.TestCase):
    """D-5: --lab-sessions include|exclude."""

    def setUp(self):
        self.ledgers = report.load_ledgers(
            [
                f"pop={os.path.join(_TESTDATA, 'ledger-pop.jsonl')}",
                f"workbook={os.path.join(_TESTDATA, 'ledger-workbook.jsonl')}",
            ]
        )
        self.reader = report.LocalTreeReader(_ARCHIVE_TREE)
        self.records = [
            {
                "verdict": "strip",
                "first_message_id": "msg_p1_u0",
                "last_message_id": "msg_p1_a1",
                "created_at_min": "2026-09-27T19:00:00.000000+00:00",
            },
            {
                "verdict": "strip",
                "archive": "viking://user/noot-pilot-lab/sessions/cc-11111111-2222-3333-4444-555555555555/history/archive_001",
                "first_message_id": "u0",
                "last_message_id": "a1",
            },
        ]

    def test_include_is_the_default_and_keeps_everything(self):
        rep = report.build_report(self.records, self.ledgers, reader=self.reader)
        self.assertEqual(len(rep["rows"]), 2)
        filtered = report.filter_rows_by_lab_sessions(rep, include_lab=True)
        self.assertEqual(len(filtered["rows"]), 2)

    def test_exclude_drops_lab_rows_and_recomputes_aggregates(self):
        rep = report.build_report(self.records, self.ledgers, reader=self.reader)
        filtered = report.filter_rows_by_lab_sessions(rep, include_lab=False)
        self.assertEqual(len(filtered["rows"]), 1)
        self.assertTrue(all(r["lab_or_pilot"] != "lab" for r in filtered["rows"]))
        self.assertEqual(dict(filtered["by_verdict"]), {"strip": 1})

    def test_lab_sessions_is_a_cli_option_defaulting_to_include(self):
        args = report.parse_args(
            ["--log", os.path.join(_TESTDATA, "ov-recall-shadow-sample.txt")]
        )
        self.assertEqual(args.lab_sessions, "include")

    def test_lab_sessions_exclude_is_accepted(self):
        args = report.parse_args(
            [
                "--log",
                os.path.join(_TESTDATA, "ov-recall-shadow-sample.txt"),
                "--lab-sessions",
                "exclude",
            ]
        )
        self.assertEqual(args.lab_sessions, "exclude")


class RowKeyAndLabelsTests(unittest.TestCase):
    """D-5: the rendered per-turn table carries a stable row key; --labels <file>
    reads restatement|new-info|mixed back per key, rejecting unknown keys and labels."""

    def setUp(self):
        self.ledgers = report.load_ledgers(
            [f"pop={os.path.join(_TESTDATA, 'ledger-pop.jsonl')}"]
        )
        self.reader = report.LocalTreeReader(_ARCHIVE_TREE)
        self.records = [
            {
                "verdict": "strip",
                "first_message_id": "msg_p1_u0",
                "last_message_id": "msg_p1_a1",
                "message_count": 2,
                "created_at_min": "2026-09-27T19:00:00.000000+00:00",
            }
        ]

    def test_the_row_key_is_stable_across_runs(self):
        rep1 = report.build_report(self.records, self.ledgers, reader=self.reader)
        rep2 = report.build_report(self.records, self.ledgers, reader=self.reader)
        self.assertEqual(
            report._row_key(rep1["rows"][0]), report._row_key(rep2["rows"][0])
        )

    def test_the_row_key_appears_in_the_rendered_table(self):
        rep = report.build_report(self.records, self.ledgers, reader=self.reader)
        key = report._row_key(rep["rows"][0])
        text = report.render_markdown(rep)
        self.assertIn(key, text)

    def test_parse_labels_file_reads_back_valid_labels(self):
        import tempfile

        rep = report.build_report(self.records, self.ledgers, reader=self.reader)
        key = report._row_key(rep["rows"][0])
        with tempfile.NamedTemporaryFile("w", suffix=".tsv", delete=False) as fh:
            fh.write(f"# comment\n\n{key}\trestatement\n")
            path = fh.name
        try:
            labels = report.parse_labels_file(path)
        finally:
            os.unlink(path)
        self.assertEqual(labels, {key: "restatement"})

    def test_parse_labels_file_rejects_an_unknown_label(self):
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".tsv", delete=False) as fh:
            fh.write("some-key\tnot-a-real-label\n")
            path = fh.name
        try:
            with self.assertRaises(ValueError):
                report.parse_labels_file(path)
        finally:
            os.unlink(path)

    def test_apply_labels_sets_the_row_label(self):
        rep = report.build_report(self.records, self.ledgers, reader=self.reader)
        key = report._row_key(rep["rows"][0])
        report.apply_labels(rep, {key: "restatement"})
        self.assertEqual(rep["rows"][0]["label"], "restatement")

    def test_apply_labels_rejects_an_unknown_key(self):
        rep = report.build_report(self.records, self.ledgers, reader=self.reader)
        with self.assertRaises(ValueError):
            report.apply_labels(rep, {"no-such-row-key": "restatement"})

    def test_labels_is_a_cli_option(self):
        args = report.parse_args(
            [
                "--log",
                os.path.join(_TESTDATA, "ov-recall-shadow-sample.txt"),
                "--labels",
                "labels.tsv",
            ]
        )
        self.assertEqual(args.labels, "labels.tsv")


class ComputePrecisionTests(unittest.TestCase):
    """D-5: precision over labelled strip rows, with the denominator and every
    exclusion reason (unresolved, ambiguous, partial, errored, unlabelled) explicit."""

    def _row(self, **overrides):
        row = {
            "verdict": "strip",
            "backend": "anthropic",
            "mode": "recall",
            "machine": "pop",
            "session": "aaaaaaaa-0000-0000-0000-000000000001",
            "archive": "viking://user/noot-pilot/sessions/cc-aaaaaaaa-0000-0000-0000-000000000001/history/archive_001",
            "archive_status": "resolved",
            "window_status": "fit",
            "partial": False,
            "reconstructed_verdict": None,
            "errored": False,
            "label": "restatement",
            "turn_start": 0,
            "turn_end": 2,
            "first_message_id": "u0",
            "last_message_id": "a1",
            "ov_tools": [],
            "other_tools": [],
            "events": [],
            "occurrences": 1,
            "duplicate_verdicts": None,
            "ambiguous_candidates": None,
            "archive_ambiguous_matches": None,
            "candidate_coverage": None,
            "events_shared_archive": False,
        }
        row.update(overrides)
        return row

    def test_a_clean_labelled_set_computes_precision(self):
        rows = [
            self._row(label="restatement"),
            self._row(label="restatement"),
            self._row(label="new-info"),
            self._row(label="mixed"),
        ]
        result = report.compute_precision(rows)
        self.assertEqual(result["numerator"], 2)
        self.assertEqual(result["denominator"], 4)
        self.assertEqual(result["precision"], 0.5)
        self.assertEqual(result["excluded"], {})

    def test_non_strip_rows_are_not_in_the_denominator_at_all(self):
        rows = [self._row(verdict="keep-mixed", label="")]
        result = report.compute_precision(rows)
        self.assertEqual(result["denominator"], 0)
        self.assertNotIn("keep-mixed", result["excluded"])

    def test_unresolved_strip_rows_are_excluded(self):
        rows = [self._row(archive_status="unresolved", label="restatement")]
        result = report.compute_precision(rows)
        self.assertEqual(result["excluded"], {"unresolved": 1})
        self.assertEqual(result["denominator"], 0)

    def test_ambiguous_strip_rows_are_excluded(self):
        rows = [self._row(archive_status="ambiguous", label="restatement")]
        result = report.compute_precision(rows)
        self.assertEqual(result["excluded"], {"ambiguous": 1})

    def test_partial_strip_rows_are_excluded(self):
        rows = [self._row(partial=True, label="restatement")]
        result = report.compute_precision(rows)
        self.assertEqual(result["excluded"], {"partial": 1})

    def test_a_reconstructed_verdict_excludes_as_partial_even_when_not_flagged_partial(
        self,
    ):
        # D-7's reconstruction found more of the turn than was logged -- the logged
        # strip can't be trusted even if the record's own partial flag was false.
        rows = [self._row(reconstructed_verdict="keep-mixed", label="restatement")]
        result = report.compute_precision(rows)
        self.assertEqual(result["excluded"], {"partial": 1})

    def test_errored_strip_rows_are_excluded(self):
        rows = [self._row(errored=True, label="restatement")]
        result = report.compute_precision(rows)
        self.assertEqual(result["excluded"], {"errored": 1})

    def test_unlabelled_strip_rows_are_excluded(self):
        rows = [self._row(label="")]
        result = report.compute_precision(rows)
        self.assertEqual(result["excluded"], {"unlabelled": 1})

    def test_precision_is_none_with_an_empty_denominator(self):
        rows = [self._row(label="")]
        result = report.compute_precision(rows)
        self.assertIsNone(result["precision"])

    def test_render_markdown_renders_a_precision_section_when_given_one(self):
        rows = [self._row(label="restatement")]
        result = report.compute_precision(rows)
        rep = {"by_verdict": collections.Counter(), "by_backend": {}, "rows": rows}
        text = report.render_markdown(rep, precision=result)
        self.assertIn("## Precision", text)
        self.assertIn("1", text)

    def test_render_markdown_omits_the_precision_section_by_default(self):
        rep = {"by_verdict": collections.Counter(), "by_backend": {}, "rows": []}
        text = report.render_markdown(rep)
        self.assertNotIn("## Precision", text)


if __name__ == "__main__":
    unittest.main()
