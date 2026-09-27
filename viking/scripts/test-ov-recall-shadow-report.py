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
        launch = sessions["a5b818a0-c729-4063-abdd-efa1eceb5522"]
        self.assertEqual(launch, "20260927T184306Z-25126")
        self.assertEqual(starts[launch]["backend"], "anthropic")

    def test_a_session_row_with_no_start_row_is_indexed_but_unresolvable(self):
        rows = [{"event": "session", "launch_id": "L", "session_id": "u1"}]
        starts, sessions = report.index_ledger(rows)
        self.assertEqual(sessions["u1"], "L")
        self.assertNotIn("L", starts)

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

    def test_no_archive_field_is_unknown(self):
        self.assertEqual(report.backend_for({}, self.ledgers), ("unknown", None))

    def test_a_matching_parent_session_resolves_through_pop(self):
        rec = {
            "archive": "viking://user/noot-pilot/sessions/cc-a5b818a0-c729-4063-abdd-efa1eceb5522/history/archive_003"
        }
        self.assertEqual(report.backend_for(rec, self.ledgers), ("anthropic", "pop"))

    def test_a_matching_subagent_resolves_through_its_parents_launch(self):
        rec = {
            "archive": "viking://user/noot-pilot/sessions/cc-a5b818a0-c729-4063-abdd-efa1eceb5522__subagent-77aa/history/archive_001"
        }
        self.assertEqual(report.backend_for(rec, self.ledgers), ("anthropic", "pop"))

    def test_a_workbook_session_resolves_through_workbook(self):
        rec = {
            "archive": "viking://user/noot-pilot-lab/sessions/cc-11111111-2222-3333-4444-555555555555/history/archive_001"
        }
        self.assertEqual(
            report.backend_for(rec, self.ledgers), ("ollama:qwen3-coder", "workbook")
        )

    def test_an_unmatched_session_is_unknown_not_dropped(self):
        rec = {
            "archive": "viking://user/noot-pilot/sessions/cc-99999999-0000-1111-2222-333344445555/history/archive_002"
        }
        self.assertEqual(report.backend_for(rec, self.ledgers), ("unknown", None))


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


if __name__ == "__main__":
    unittest.main()
