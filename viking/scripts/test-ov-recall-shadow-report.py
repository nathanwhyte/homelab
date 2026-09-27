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

    def test_a_turn_after_its_only_launch_ended_is_unknown(self):
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
        self.assertEqual(
            report.backend_for(archive, ledgers, "2026-09-27T08:15:00Z")[0],
            "ollama:deepseek",
        )
        self.assertEqual(
            report.backend_for(archive, ledgers, "2026-09-27T09:30:00Z")[0], "unknown"
        )

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
            report.backend_for(None, self.ledgers), ("unknown", None, None)
        )

    def test_a_matching_parent_session_resolves_through_pop(self):
        archive = "viking://user/noot-pilot/sessions/cc-a5b818a0-c729-4063-abdd-efa1eceb5522/history/archive_003"
        self.assertEqual(
            report.backend_for(archive, self.ledgers), ("anthropic", "pop", None)
        )

    def test_a_matching_subagent_resolves_through_its_parents_launch(self):
        archive = "viking://user/noot-pilot/sessions/cc-a5b818a0-c729-4063-abdd-efa1eceb5522__subagent-77aa/history/archive_001"
        self.assertEqual(
            report.backend_for(archive, self.ledgers), ("anthropic", "pop", None)
        )

    def test_a_workbook_session_resolves_through_workbook(self):
        archive = "viking://user/noot-pilot-lab/sessions/cc-11111111-2222-3333-4444-555555555555/history/archive_001"
        self.assertEqual(
            report.backend_for(archive, self.ledgers),
            ("ollama:qwen3-coder", "workbook", None),
        )

    def test_an_unmatched_session_is_unknown_not_dropped(self):
        archive = "viking://user/noot-pilot/sessions/cc-99999999-0000-1111-2222-333344445555/history/archive_002"
        self.assertEqual(
            report.backend_for(archive, self.ledgers), ("unknown", None, None)
        )

    def test_created_at_min_is_accepted_and_does_not_change_a_single_window_case(self):
        archive = "viking://user/noot-pilot/sessions/cc-a5b818a0-c729-4063-abdd-efa1eceb5522/history/archive_003"
        self.assertEqual(
            report.backend_for(
                archive, self.ledgers, created_at_min="2026-09-27T19:00:00+00:00"
            ),
            ("anthropic", "pop", None),
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
        backend, machine, candidates = report.backend_for(
            self.archive, self.ledgers, created_at_min="2026-09-12T00:00:00+00:00"
        )
        self.assertEqual(
            (backend, machine, candidates), ("ollama:qwen3-coder", "pop", None)
        )

    def test_a_turn_after_the_resume_gets_the_anthropic_backend(self):
        # Regression: an earlier version kept only the session's first (earliest)
        # launch and reported ollama:qwen3-coder here too.
        backend, machine, candidates = report.backend_for(
            self.archive, self.ledgers, created_at_min="2026-09-20T00:00:00+00:00"
        )
        self.assertEqual((backend, machine, candidates), ("anthropic", "pop", None))

    def test_a_turn_before_any_launch_is_unknown(self):
        backend, machine, candidates = report.backend_for(
            self.archive, self.ledgers, created_at_min="2026-09-01T00:00:00+00:00"
        )
        self.assertEqual((backend, machine, candidates), ("unknown", None, None))

    def test_no_created_at_min_with_two_windows_is_ambiguous(self):
        backend, machine, candidates = report.backend_for(self.archive, self.ledgers)
        self.assertEqual(backend, "ambiguous")
        self.assertIsNone(machine)
        self.assertEqual(
            candidates,
            [
                {"machine": "pop", "backend": "ollama:qwen3-coder"},
                {"machine": "pop", "backend": "anthropic"},
            ],
        )


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
        self.assertEqual(
            sorted(self.reader.list_sessions("noot-pilot")),
            [
                "cc-a5b818a0-c729-4063-abdd-efa1eceb5522",
                "cc-a5b818a0-c729-4063-abdd-efa1eceb5522__subagent-77aa",
                "cc-bbbbbbbb-1111-2222-3333-444444444444",
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
        names = {e["name"] for e in report._diff_events(diff)}
        self.assertEqual(names, {"a", "b"})

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


if __name__ == "__main__":
    unittest.main()
