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
    def test_start_and_session_rows_join_by_launch_id(self):
        rows = report.parse_ledger_rows(os.path.join(_TESTDATA, "ledger-pop.jsonl"))
        starts, sessions = report.index_ledger(rows)
        entry = sessions["a5b818a0-c729-4063-abdd-efa1eceb5522"]
        self.assertEqual(entry["launch_id"], "20260927T184306Z-25126")
        self.assertEqual(entry["ts"], "2026-09-27T18:43:07Z")
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
            sessions["u1"], {"launch_id": "L", "ts": "2026-09-27T00:00:00Z"}
        )
        self.assertNotIn("L", starts)

    def test_the_earliest_ts_wins_across_resume_and_compact_rows(self):
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
        self.assertEqual(sessions["u1"]["ts"], "2026-09-27T08:00:00Z")

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
        self.assertEqual(report.backend_for(None, self.ledgers), ("unknown", None))

    def test_a_matching_parent_session_resolves_through_pop(self):
        archive = "viking://user/noot-pilot/sessions/cc-a5b818a0-c729-4063-abdd-efa1eceb5522/history/archive_003"
        self.assertEqual(
            report.backend_for(archive, self.ledgers), ("anthropic", "pop")
        )

    def test_a_matching_subagent_resolves_through_its_parents_launch(self):
        archive = "viking://user/noot-pilot/sessions/cc-a5b818a0-c729-4063-abdd-efa1eceb5522__subagent-77aa/history/archive_001"
        self.assertEqual(
            report.backend_for(archive, self.ledgers), ("anthropic", "pop")
        )

    def test_a_workbook_session_resolves_through_workbook(self):
        archive = "viking://user/noot-pilot-lab/sessions/cc-11111111-2222-3333-4444-555555555555/history/archive_001"
        self.assertEqual(
            report.backend_for(archive, self.ledgers),
            ("ollama:qwen3-coder", "workbook"),
        )

    def test_an_unmatched_session_is_unknown_not_dropped(self):
        archive = "viking://user/noot-pilot/sessions/cc-99999999-0000-1111-2222-333344445555/history/archive_002"
        self.assertEqual(report.backend_for(archive, self.ledgers), ("unknown", None))


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
        rep = report.build_report(self.records, self.ledgers)
        by_backend = rep["by_backend"]
        self.assertEqual(
            {
                k: (len(v["sessions"]), v["turns"], v["strip"])
                for k, v in by_backend.items()
            },
            {
                "unknown": (3, 3, 2),
                "anthropic": (2, 2, 1),
                "ollama:qwen3-coder": (1, 1, 1),
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
        self.assertIn("| anthropic | 2 | 2 | 1 |", text)
        self.assertIn("| ollama:qwen3-coder | 1 | 1 | 1 |", text)
        self.assertIn("| unknown | 3 | 3 | 2 |", text)


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
