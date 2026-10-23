"""Tests for ov-queue-drained.py. Stdlib only — runs anywhere:

    python3 test-ov-queue-drained.py

Fixtures are `ov status --output json` documents built around the v0.4.20 queue
table shape (the exporter's parser reads the same text in production).
"""

import importlib.util
import os
import unittest

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

    def test_an_unhealthy_server_is_not_drained(self):
        code, _ = qd.judge(status(healthy=False))
        self.assertEqual(code, 1)

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


if __name__ == "__main__":
    unittest.main()
