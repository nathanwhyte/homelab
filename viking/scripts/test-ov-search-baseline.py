"""Tests for ov-search-baseline.py's compare. Pure (stdlib fixtures only) — runs anywhere:

python3 test-ov-search-baseline.py
"""

import importlib.util
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SPEC = importlib.util.spec_from_file_location(
    "ov_search_baseline", os.path.join(_HERE, "ov-search-baseline.py")
)
sb = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sb)


def capture(scores: dict[str, dict[str, float]]) -> dict:
    """{qid: {uri: score}} -> a capture document."""
    queries = {}
    for qid, hits in scores.items():
        h = [{"section": "resources", "uri": u, "score": s} for u, s in hits.items()]
        queries[qid] = {"question": qid, "hits": h, "top3": sb.top3(h)}
    return {"queries": queries}


OLD = {
    "E1": {"a": 0.66, "b": 0.50, "c": 0.44, "d": 0.30},
    "E2": {"e": 0.51, "f": 0.46, "g": 0.20},
}


def halved(scores, jitter=0.0, swap=None):
    out = {
        q: {u: (s + 1) / 2 + jitter for u, s in hits.items()}
        for q, hits in scores.items()
    }
    if swap:
        q, x, y = swap
        out[q][x], out[q][y] = out[q][y], out[q][x]
    return out


class CompareTests(unittest.TestCase):
    def run_compare(self, old, new, mapping="half"):
        return sb.compare(
            capture(old), capture(new), [0.45, 0.50, 0.65], mapping, 0.01, 0.02
        )

    def test_exact_rescale_accepts_the_formula(self):
        code, report = self.run_compare(OLD, halved(OLD))
        self.assertEqual(code, 0)
        self.assertIn("ACCEPTED", report)
        self.assertIn("| 0.500 | 0.750 |", report)
        self.assertTrue(report.rstrip().endswith("| 0.825 |"))

    def test_jitter_within_tolerance_still_accepts(self):
        code, report = self.run_compare(OLD, halved(OLD, jitter=0.009))
        self.assertEqual(code, 0)
        self.assertIn("ACCEPTED", report)

    def test_deviation_past_tolerance_measures_the_floors(self):
        code, report = self.run_compare(OLD, halved(OLD, jitter=0.03))
        self.assertEqual(code, 0)
        self.assertIn("REJECTED", report)
        # old 0.50 window holds b (0.50) and e (0.51): new 0.78, 0.785 -> median 0.7825
        self.assertIn("| 0.500 | 0.750 | 0.782 | 2 | 0.782 |", report)

    def test_a_top3_change_rejects_the_formula(self):
        code, report = self.run_compare(OLD, halved(OLD, swap=("E1", "a", "d")))
        self.assertIn("REJECTED", report)
        self.assertIn("top-3 differs on 1 queries: E1", report)
        self.assertEqual(code, 0)

    def test_a_floor_with_nothing_in_its_window_exits_2_when_rejected(self):
        old = {"E1": {"a": 0.90, "b": 0.10}}
        code, report = self.run_compare(old, halved(old, jitter=0.05))
        self.assertEqual(code, 2)
        self.assertIn("NO DATA", report)

    def test_identity_passes_on_an_unchanged_capture(self):
        code, report = self.run_compare(OLD, OLD, mapping="identity")
        self.assertEqual(code, 0)
        self.assertIn("RESULT: PASS", report)

    def test_identity_fails_on_a_missing_query(self):
        new = {"E1": OLD["E1"]}
        code, report = self.run_compare(OLD, new, mapping="identity")
        self.assertEqual(code, 1)
        self.assertIn("top-3 differs on 1 queries: E2", report)


if __name__ == "__main__":
    unittest.main()
