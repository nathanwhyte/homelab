"""Tests for ov-queue-drained.py. Stdlib only — runs anywhere:

    python3 test-ov-queue-drained.py

Fixtures are `ov status --output json` documents built around the v0.4.20 queue
table shape and the v0.5.1.1 one (a ninth row, Reindex); the exporter's parser
reads the same text in production.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_scripts" / "lib"))
from git_env import scrub_environ  # noqa: E402

scrub_environ()

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_SPEC = importlib.util.spec_from_file_location(
    "ov_queue_drained", os.path.join(_HERE, "ov-queue-drained.py")
)
qd = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(qd)

ROWS = [
    ("Embedding", 0, 0),
    ("Semantic", 0, 0),
    ("ExternalParse", 0, 0),
    ("AddResource", 0, 0),
    ("SessionCommit", 0, 0),
    ("ExternalTask", 0, 0),
    ("UserDeletion", 0, 0),
    ("Semantic-Nodes", 0, 0),
]


def table(rows):
    sep = "+----------------+---------+-------------+-----------+----------+--------+-------+"
    out = [
        sep,
        "|     Queue      | Pending | In Progress | Processed | Requeued | Errors | Total |",
        sep,
    ]
    out += [f"| {n} | {p} | {i} | 10 | 0 | 0 | 10 |" for n, p, i in rows]
    out += [
        "|     TOTAL      |    0    |      0      |    80     |    0     |   0    |  80   |",
        sep,
    ]
    return "\n".join(out)


def status(rows=ROWS, healthy=True, queue_healthy=True, text=None):
    return {
        "ok": True,
        "result": {
            "is_healthy": healthy,
            "components": {
                "queue": {
                    "name": "queue",
                    "is_healthy": queue_healthy,
                    "status": table(rows) if text is None else text,
                }
            },
        },
    }


# v0.5.1.1 as the IMPR-1412 rehearsal captured it: a ninth queue row (Reindex), and
# the server unhealthy only because the vikingdb component's aggregate call 404s.
ROWS_0511 = ROWS[:4] + [("Reindex", 0, 0)] + ROWS[4:]


def status_0511(rows=ROWS_0511):
    s = status(rows, healthy=False)
    s["result"]["errors"] = ["vikingdb has errors"]
    s["result"]["components"]["vikingdb"] = {
        "name": "vikingdb",
        "is_healthy": False,
        "has_errors": True,
        "status": "VectorDB unavailable: 404 Client Error: Not Found for url: "
        "http://ov-vectordb:5000/api/vikingdb/data/aggregate",
    }
    return s


class JudgeTests(unittest.TestCase):
    def test_all_zero_is_drained(self):
        code, r = qd.judge(status())
        self.assertEqual((code, r["drained"], r["queues"]), (0, True, 8))

    def test_in_progress_on_semantic_nodes_is_busy_even_though_total_reads_zero(self):
        rows = [
            r if r[0] != "Semantic-Nodes" else ("Semantic-Nodes", 0, 1) for r in ROWS
        ]
        code, r = qd.judge(status(rows))
        self.assertEqual(code, 3)
        self.assertEqual(
            r["busy"], {"Semantic-Nodes": {"pending": 0, "in_progress": 1}}
        )

    def test_an_unhealthy_queue_with_no_rows_is_not_drained(self):
        code, r = qd.judge(status(queue_healthy=False, text="Not initialized"))
        self.assertEqual(code, 1)

    def test_a_healthy_flag_over_a_rowless_status_is_not_drained(self):
        code, _ = qd.judge(status(text="Not initialized"))
        self.assertEqual(code, 1)

    def test_a_missing_queue_row_is_not_drained(self):
        code, r = qd.judge(status(ROWS[:-1]))
        self.assertEqual(code, 1)
        self.assertIn("Semantic-Nodes", r["error"])

    def test_a_non_numeric_count_is_not_drained(self):
        rows = [r if r[0] != "Embedding" else ("Embedding", "?", 0) for r in ROWS]
        code, _ = qd.judge(status(rows))
        self.assertEqual(code, 1)

    def test_an_unhealthy_server_over_a_healthy_drained_queue_is_drained(self):
        code, r = qd.judge(status(healthy=False))
        self.assertEqual((code, r["drained"]), (0, True))

    def test_v0511_with_vikingdb_unhealthy_is_drained_and_names_it(self):
        code, r = qd.judge(status_0511())
        self.assertEqual((code, r["drained"], r["queues"]), (0, True, 9))
        self.assertEqual(r["unhealthy"], ["vikingdb"])

    def test_a_non_dict_component_is_listed_unhealthy_and_does_not_block(self):
        for value in ("unavailable", ["x"], True):
            s = status()
            s["result"]["components"]["models"] = value
            code, r = qd.judge(s)
            self.assertEqual((code, r["unhealthy"]), (0, ["models"]), value)

    def test_a_non_dict_queue_component_or_components_is_not_drained(self):
        s = status()
        s["result"]["components"]["queue"] = "down"
        self.assertEqual(qd.judge(s)[0], 1)
        s = status()
        s["result"]["components"] = ["queue"]
        self.assertEqual(qd.judge(s)[0], 1)
        self.assertEqual(qd.judge({"ok": True, "result": "x"})[0], 1)
        for value in ("x", [1], None):
            self.assertEqual(qd.judge(value)[0], 1, value)

    def test_a_non_string_queue_status_is_not_drained(self):
        for value in ({"queues": []}, 5, ["x"], True):
            code, r = qd.judge(status(text=value))
            self.assertEqual(code, 1, value)
            self.assertIn("found 0", r["error"])

    def test_only_a_boolean_true_counts_as_ok_or_healthy(self):
        for value in ("false", 1, [0]):
            self.assertEqual(qd.judge({**status(), "ok": value})[0], 1, value)
            self.assertEqual(qd.judge(status(queue_healthy=value))[0], 1, value)

    def test_a_component_whose_flag_is_not_true_is_listed_unhealthy(self):
        s = status()
        s["result"]["components"]["lock"] = {"is_healthy": "false"}
        s["result"]["components"]["models"] = {"name": "models"}
        self.assertEqual(qd.judge(s)[1]["unhealthy"], ["lock", "models"])

    def test_a_refusal_still_lists_the_unhealthy_components(self):
        code, r = qd.judge(status_0511(rows=ROWS_0511[:-1]))
        self.assertEqual(code, 1)
        self.assertEqual(r["unhealthy"], ["vikingdb"])
        s = status_0511()
        s["result"]["components"]["queue"]["is_healthy"] = False
        self.assertEqual(qd.judge(s)[1]["unhealthy"], ["queue", "vikingdb"])

    def test_v0511_reindex_work_is_busy(self):
        rows = [r if r[0] != "Reindex" else ("Reindex", 4, 0) for r in ROWS_0511]
        code, r = qd.judge(status_0511(rows))
        self.assertEqual(code, 3)
        self.assertEqual(r["busy"], {"Reindex": {"pending": 4, "in_progress": 0}})

    # Each refusal below on its own, over an otherwise drained table.
    def test_a_status_without_ok_is_not_drained(self):
        code, r = qd.judge({**status(), "ok": False})
        self.assertEqual(code, 1)
        self.assertIn("not healthy", r["error"])

    def test_an_unhealthy_queue_over_a_drained_table_is_not_drained(self):
        code, r = qd.judge(status(queue_healthy=False))
        self.assertEqual(code, 1)
        self.assertIn("not healthy", r["error"])

    def test_two_queue_tables_are_not_drained(self):
        code, r = qd.judge(status(text=table(ROWS) + "\n\n" + table(ROWS)))
        self.assertEqual(code, 1)
        self.assertIn("found 2", r["error"])


class MainTests(unittest.TestCase):
    """The command's contract: exit 0/3/1 and exactly one JSON line on stdout."""

    def run_main(self, *argv, ov=None):
        out = io.StringIO()
        with (
            mock.patch.object(sys, "argv", ["ov-queue-drained.py", *argv]),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            if ov is None:
                code = qd.main()
            else:
                with mock.patch.object(qd.subprocess, "run", ov):
                    code = qd.main()
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 1, out.getvalue())
        return code, json.loads(lines[0])

    def saved(self, doc, raw=None):
        f = tempfile.NamedTemporaryFile("wb", suffix=".json", delete=False)
        self.addCleanup(os.unlink, f.name)
        with f:
            f.write(raw if raw is not None else json.dumps(doc).encode())
        return f.name

    @staticmethod
    def ov_returning(rc, stdout="", stderr=""):
        return lambda *a, **k: subprocess.CompletedProcess(a, rc, stdout, stderr)

    def test_drained_busy_and_unreadable_files_map_to_0_3_1(self):
        busy = [r if r[0] != "Embedding" else ("Embedding", 2, 0) for r in ROWS]
        self.assertEqual(self.run_main("--file", self.saved(status()))[0], 0)
        self.assertEqual(self.run_main("--file", self.saved(status(busy)))[0], 3)
        self.assertEqual(self.run_main("--file", self.saved(None, b"{nope"))[0], 1)

    def test_a_failing_ov_status_is_not_drained(self):
        code, r = self.run_main(ov=self.ov_returning(2, json.dumps(status())))
        self.assertEqual(code, 1)
        self.assertIn("exited 2", r["error"])

    def test_a_drained_ov_status_passes(self):
        self.assertEqual(
            self.run_main(ov=self.ov_returning(0, json.dumps(status())))[0], 0
        )

    def test_io_failures_print_one_json_line_and_exit_1(self):
        def missing_ov(*a, **k):
            raise FileNotFoundError("ov")

        def undecodable(*a, **k):
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

        cases = {
            "missing ov": ((), missing_ov),
            "non-UTF-8 ov stdout": ((), undecodable),
            "missing file": (("--file", "/nonexistent/status.json"), None),
            "directory": (("--file", tempfile.gettempdir()), None),
            "non-UTF-8 file": (("--file", self.saved(None, b"\xff\xfe")), None),
        }
        for label, (argv, ov) in cases.items():
            with self.subTest(label):
                code, r = self.run_main(*argv, ov=ov)
                self.assertEqual(code, 1)
                self.assertIn("error", r)

    def test_bad_arguments_print_one_json_line_and_exit_1(self):
        for argv in (("--file",), ("--help",), ("-h",), ("--bogus",)):
            with self.subTest(argv):
                code, r = self.run_main(*argv)
                self.assertEqual(code, 1)
                self.assertIn("error", r)


if __name__ == "__main__":
    unittest.main()
