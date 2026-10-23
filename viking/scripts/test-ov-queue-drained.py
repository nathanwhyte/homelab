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


if __name__ == "__main__":
    unittest.main()
