"""Tests for ov-haiku-reading.py. Pure (fakes and local child processes) — runs anywhere:

    python3 test-ov-haiku-reading.py

Every Prometheus call goes through the module's single ``_get`` choke point, so these
tests replace it with a fake that answers by metric name, honours the range query's
start/end/step, and records each query. The port-forward tests run small Python
children in place of kubectl. The live check (the 2026-10-15 weekday reading in
compendium IMPR-1311) is run by hand.
"""

import argparse
import importlib.util
import os
import sys
import time
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
    """Answers by metric name. ``range_values`` maps provider -> per-step values."""

    def __init__(self, calls=None, range_values=None, budget="88.921"):
        self.calls = []
        self.totals = calls or {"openai": 4383.4, "anthropic": 978.2}
        self.range_values = range_values or {
            "openai": ["90", "50", "NaN"],
            "anthropic": ["10", "50", "0"],
        }
        self.budget = budget

    def __call__(self, url):
        parsed = urllib.parse.urlparse(url)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        self.calls.append((parsed.path, params))
        q = params["query"]
        if parsed.path.endswith("query_range"):
            start, end = float(params["start"]), float(params["end"])
            step = int(params["step"])
            ts = []
            t = start
            while t <= end:
                ts.append(t)
                t += step
            result = [
                {
                    "metric": {"api_provider": p},
                    "values": [[t, v] for t, v in zip(ts, vals) if v is not None],
                }
                for p, vals in self.range_values.items()
            ]
        elif hr.SUCCESS in q:
            result = vec("api_provider", self.totals)
        elif hr.FALLBACKS in q:
            result = vec("exception_status", {"429": 956.6, "408": 24.0})
        elif hr.FAILURES in q:
            result = vec("api_provider", {"openai": 22.0})
        elif hr.SPEND in q:
            result = vec("api_provider", {"anthropic": 0.69, "openai": 0.0})
        elif "remaining_budget" in q:
            result = [{"metric": {}, "value": [0, self.budget]}]
        else:
            raise AssertionError(f"unexpected query {q}")
        return {"status": "success", "data": {"result": result}}


class FakeTest(unittest.TestCase):
    def make_fake(self):
        return FakeProm()

    def setUp(self):
        self.fake = self.make_fake()
        self._orig = hr._get
        hr._get = self.fake

    def tearDown(self):
        hr._get = self._orig

    def reading(self, since=SINCE, until=UNTIL, bucket=timedelta(hours=1)):
        return hr.collect("http://p", since, until, bucket)

    def range_params(self):
        return next(p for path, p in self.fake.calls if path.endswith("query_range"))


class CollectTests(FakeTest):
    def test_every_instant_query_uses_one_window_at_until(self):
        self.reading()
        instants = [p for path, p in self.fake.calls if path.endswith("/query")]
        self.assertEqual(len(instants), 5)
        span = int((UNTIL - SINCE).total_seconds())
        for p in instants:
            self.assertEqual(float(p["time"]), UNTIL.timestamp())
            if "increase" in p["query"]:
                self.assertIn(f"[{span}s]", p["query"])

    def test_bucket_query_looks_back_exactly_one_bucket(self):
        reading = self.reading()
        p = self.range_params()
        self.assertEqual(
            p["query"],
            f"sum by (api_provider) (increase({hr.SUCCESS}[3600s]))",
        )
        self.assertEqual(float(p["start"]), (SINCE + timedelta(hours=1)).timestamp())
        self.assertEqual(float(p["end"]), (SINCE + timedelta(hours=3)).timestamp())
        self.assertEqual(p["step"], "3600")
        self.assertEqual(len(reading["buckets"]), 3)
        self.assertEqual(reading["remainder_s"], 1800)

    def test_dst_window_is_measured_in_elapsed_time(self):
        # 2026-11-01 00:00-04:00 Chicago spans five elapsed hours (01:00 repeats).
        since = datetime(2026, 11, 1, 0, 0, tzinfo=CHI)
        until = datetime(2026, 11, 1, 4, 0, tzinfo=CHI)
        reading = self.reading(since, until)
        self.assertEqual(reading["span_s"], 5 * 3600)
        totals = next(
            p for path, p in self.fake.calls if hr.SUCCESS in p["query"] and "time" in p
        )
        self.assertIn("[18000s]", totals["query"])
        p = self.range_params()
        self.assertEqual(float(p["end"]), until.timestamp())
        self.assertEqual(len(reading["buckets"]), 5)

    def test_bucket_longer_than_window_skips_the_table(self):
        reading = self.reading(bucket=timedelta(days=1))
        self.assertEqual(reading["buckets"], [])
        self.assertFalse(any(c[0].endswith("query_range") for c in self.fake.calls))

    def test_reversed_window_is_refused(self):
        with self.assertRaises(ValueError):
            self.reading(UNTIL, SINCE)

    def test_failed_query_raises(self):
        hr._get = lambda url: {"status": "error", "error": "bad"}
        with self.assertRaises(RuntimeError):
            self.reading()

    def test_no_success_series_is_an_error_not_zero(self):
        self.fake.totals = {}
        with self.assertRaises(RuntimeError):
            self.reading()


class RenderTests(FakeTest):
    def setUp(self):
        super().setUp()
        self.out = hr.render(self.reading(), CHI)

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
        self.assertIn("| 10-15 10:28 | 100 | 50% |", self.out)
        self.assertIn("| 10-15 11:28 | unknown | unknown |", self.out)  # NaN
        self.assertIn(
            "_The last 30 min are in the totals but not in a bucket._", self.out
        )


class UnknownDataTests(FakeTest):
    def make_fake(self):
        return FakeProm(
            calls={"openai": 100.0, "anthropic": float("nan")},
            range_values={"openai": ["5", None, "7"], "anthropic": ["1", None, None]},
            budget="NaN",
        )

    def test_nan_total_is_unknown_not_zero(self):
        out = hr.render(self.reading(), CHI)
        self.assertIn("| VLM calls | unknown: 100 on gemma, unknown on Haiku", out)
        self.assertIn("(Haiku unknown) |", out)
        self.assertIn("| Threshold left at window end | unknown |", out)

    def test_bucket_with_no_samples_is_no_data(self):
        out = hr.render(self.reading(), CHI)
        self.assertIn("| 10-15 09:28 | 6 | 17% |", out)
        self.assertIn("| 10-15 10:28 | no data | no data |", out)
        self.assertIn("| 10-15 11:28 | 7 | 0% |", out)

    def test_missing_spend_series_is_unknown(self):
        reading = self.reading()
        reading["spend"] = {}
        out = hr.render(reading, CHI)
        self.assertIn(
            "| Haiku spend in window | unknown (no Haiku spend series) |", out
        )


class ParseTests(unittest.TestCase):
    def test_naive_time_takes_the_zone_and_becomes_utc(self):
        t = hr.parse_time("2026-10-15T08:28", CHI)
        self.assertEqual(t.utcoffset(), timedelta(0))
        self.assertEqual(t.hour, 13)

    def test_offset_time_keeps_its_instant(self):
        t = hr.parse_time("2026-10-15T08:28-05:00", CHI)
        self.assertEqual(t, datetime(2026, 10, 15, 13, 28, tzinfo=UTC))

    def test_bucket_spec(self):
        self.assertEqual(hr.parse_bucket("6h"), timedelta(hours=6))
        self.assertEqual(hr.parse_bucket("1d"), timedelta(days=1))
        for bad in ("90m", "0h", "0d"):
            with self.assertRaises(argparse.ArgumentTypeError):
                hr.parse_bucket(bad)


def child(code):
    return [sys.executable, "-c", code]


FORWARD = "print('Forwarding from 127.0.0.1:43210 -> 9090', flush=True)"


class PortForwardTests(unittest.TestCase):
    def test_ready_line_gives_the_url_and_exit_reaps_the_child(self):
        pf = hr.PortForward(child(f"import time; {FORWARD}; time.sleep(60)"))
        with pf as url:
            self.assertEqual(url, "http://127.0.0.1:43210")
        self.assertIsNotNone(pf.proc.poll())

    def test_silent_child_times_out_and_is_stopped(self):
        pf = hr.PortForward(child("import time; time.sleep(60)"), ready_timeout=0.5)
        started = time.monotonic()
        with self.assertRaises(RuntimeError):
            pf.__enter__()
        self.assertLess(time.monotonic() - started, 10)
        self.assertIsNotNone(pf.proc.poll())

    def test_child_that_exits_early_reports_its_output(self):
        pf = hr.PortForward(child("print('error: no such service')"))
        with self.assertRaises(RuntimeError) as ctx:
            pf.__enter__()
        self.assertIn("no such service", str(ctx.exception))

    def test_output_flood_after_ready_does_not_block(self):
        flood = (
            f"import sys; {FORWARD}\nwhile True: sys.stdout.write('x' * 4096 + chr(10))"
        )
        pf = hr.PortForward(child(flood))
        with pf:
            time.sleep(0.5)  # the child fills a pipe nobody drained before the fix
            self.assertIsNone(pf.proc.poll())
        self.assertIsNotNone(pf.proc.poll())

    def test_child_ignoring_terminate_is_killed(self):
        code = (
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            f"{FORWARD}; time.sleep(60)"
        )
        pf = hr.PortForward(child(code), stop_timeout=0.5)
        with pf:
            pass
        self.assertIsNotNone(pf.proc.poll())


if __name__ == "__main__":
    unittest.main()
