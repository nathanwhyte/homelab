"""Tests for ov-haiku-reading.py. Pure (fakes only) — runs anywhere:

    python3 test-ov-haiku-reading.py

Every Prometheus call goes through the module's single ``_get`` choke point, so these
tests replace it with a fake that answers by metric name and records each query. The
live check (the 2026-10-15 weekday reading in compendium IMPR-1311) is run by hand.
"""

import argparse
import importlib.util
import os
import unittest
import urllib.parse
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

_HERE = os.path.dirname(os.path.abspath(__file__))
_SPEC = importlib.util.spec_from_file_location(
    "ov_haiku_reading", os.path.join(_HERE, "ov-haiku-reading.py")
)
hr = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(hr)

CHI = ZoneInfo("America/Chicago")
SINCE = datetime(2026, 10, 15, 8, 28, tzinfo=CHI)
UNTIL = datetime(2026, 10, 15, 11, 58, tzinfo=CHI)  # 3.5 h: three buckets + 30 min


def vec(label, values):
    return [{"metric": {label: k}, "value": [0, str(v)]} for k, v in values.items()]


class FakeProm:
    def __init__(self):
        self.calls = []

    def __call__(self, url):
        parsed = urllib.parse.urlparse(url)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        self.calls.append((parsed.path, params))
        q = params["query"]
        if parsed.path.endswith("query_range"):
            start, step = float(params["start"]), int(params["step"])
            ts = [start + i * step for i in range(3)]
            result = [
                {
                    "metric": {"api_provider": "openai"},
                    "values": [[t, v] for t, v in zip(ts, ["90", "50", "NaN"])],
                },
                {
                    "metric": {"api_provider": "anthropic"},
                    "values": [[t, v] for t, v in zip(ts, ["10", "50", "0"])],
                },
            ]
        elif hr.SUCCESS in q:
            result = vec("api_provider", {"openai": 4383.4, "anthropic": 978.2})
        elif hr.FALLBACKS in q:
            result = vec("exception_status", {"429": 956.6, "408": 24.0})
        elif hr.FAILURES in q:
            result = vec("api_provider", {"openai": 22.0})
        elif hr.SPEND in q:
            result = vec("api_provider", {"anthropic": 0.69, "openai": 0.0})
        elif "remaining_budget" in q:
            result = [{"metric": {}, "value": [0, "88.921"]}]
        else:
            raise AssertionError(f"unexpected query {q}")
        return {"status": "success", "data": {"result": result}}


class CollectTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeProm()
        self._orig = hr._get
        hr._get = self.fake

    def tearDown(self):
        hr._get = self._orig

    def test_every_instant_query_uses_one_window_at_until(self):
        hr.collect("http://p", SINCE, UNTIL, timedelta(hours=1))
        instants = [p for path, p in self.fake.calls if path.endswith("/query")]
        self.assertEqual(len(instants), 5)
        span = int((UNTIL - SINCE).total_seconds())
        for p in instants:
            self.assertEqual(float(p["time"]), UNTIL.timestamp())
            if "increase" in p["query"]:
                self.assertIn(f"[{span}s]", p["query"])

    def test_buckets_end_on_whole_steps_and_report_the_remainder(self):
        reading = hr.collect("http://p", SINCE, UNTIL, timedelta(hours=1))
        p = next(p for path, p in self.fake.calls if path.endswith("query_range"))
        self.assertEqual(float(p["start"]), (SINCE + timedelta(hours=1)).timestamp())
        self.assertEqual(float(p["end"]), (SINCE + timedelta(hours=3)).timestamp())
        self.assertEqual(p["step"], "3600")
        self.assertEqual(len(reading["buckets"]), 3)
        self.assertEqual(reading["remainder_s"], 1800)
        self.assertEqual(reading["buckets"][2]["calls"]["openai"], 0.0)  # NaN -> 0

    def test_bucket_longer_than_window_skips_the_table(self):
        reading = hr.collect("http://p", SINCE, UNTIL, timedelta(days=1))
        self.assertEqual(reading["buckets"], [])
        self.assertFalse(any(c[0].endswith("query_range") for c in self.fake.calls))

    def test_reversed_window_is_refused(self):
        with self.assertRaises(ValueError):
            hr.collect("http://p", UNTIL, SINCE, timedelta(hours=1))

    def test_failed_query_raises(self):
        hr._get = lambda url: {"status": "error", "error": "bad"}
        with self.assertRaises(RuntimeError):
            hr.collect("http://p", SINCE, UNTIL, timedelta(hours=1))


class RenderTests(unittest.TestCase):
    def setUp(self):
        self._orig = hr._get
        hr._get = FakeProm()
        self.out = hr.render(
            hr.collect("http://p", SINCE, UNTIL, timedelta(hours=1)), CHI
        )

    def tearDown(self):
        hr._get = self._orig

    def test_totals_routes_and_share(self):
        self.assertIn(
            "| VLM calls | 5,362: 4,383 on gemma, 978 on Haiku (Haiku 18%) |", self.out
        )

    def test_failovers_named_by_reason(self):
        self.assertIn(
            "24 × `408` (gemma past 90 s), 957 × `429` (gemma full)", self.out
        )

    def test_spend_threshold_and_failures(self):
        self.assertIn("| Haiku spend in window | $0.69 (router tally", self.out)
        self.assertIn("| Threshold left at window end | $88.92 |", self.out)
        self.assertIn("| Failed responses | 22 on gemma |", self.out)

    def test_window_line_in_local_and_utc(self):
        self.assertIn(
            "2026-10-15 08:28 to 2026-10-15 11:58 CDT (13:28Z to 16:58Z), 3.5 h",
            self.out,
        )

    def test_bucket_rows_and_remainder_note(self):
        self.assertIn("| 10-15 09:28 | 100 | 10% |", self.out)
        self.assertIn("| 10-15 11:28 | 0 | n/a |", self.out)
        self.assertIn(
            "_The last 30 min are in the totals but not in a bucket._", self.out
        )


class ParseTests(unittest.TestCase):
    def test_naive_time_takes_the_zone(self):
        t = hr.parse_time("2026-10-15T08:28", CHI)
        self.assertEqual(t.astimezone(UTC).hour, 13)

    def test_offset_time_keeps_its_offset(self):
        t = hr.parse_time("2026-10-15T13:28+00:00", CHI)
        self.assertEqual(t.utcoffset(), timedelta(0))

    def test_bucket_spec(self):
        self.assertEqual(hr.parse_bucket("6h"), timedelta(hours=6))
        self.assertEqual(hr.parse_bucket("1d"), timedelta(days=1))
        with self.assertRaises(argparse.ArgumentTypeError):
            hr.parse_bucket("90m")


if __name__ == "__main__":
    unittest.main()
