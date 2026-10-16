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
import tempfile
import threading
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
    """Answers by metric name.

    ``totals``: the window's increase() per route. ``present``: routes with samples in
    the window (default: the keys of ``totals``). ``range_values``: per-route
    increase() values per bucket, None for no point. ``sampled``: per-route booleans
    per bucket for the samples query (default: where ``range_values`` has a point).
    """

    def __init__(
        self,
        totals=None,
        present=None,
        range_values=None,
        sampled=None,
        budget="88.921",
    ):
        self.calls = []
        self.totals = (
            totals if totals is not None else {"openai": 4383.4, "anthropic": 978.2}
        )
        self.present = present if present is not None else list(self.totals)
        self.range_values = range_values or {
            "openai": ["90", "50", "NaN"],
            "anthropic": ["10", "50", "0"],
        }
        self.sampled = sampled or {
            p: [v is not None for v in vals] for p, vals in self.range_values.items()
        }
        self.budget = budget

    def _range(self, params, table):
        start, end = float(params["start"]), float(params["end"])
        step = int(params["step"])
        ts = []
        t = start
        while t <= end:
            ts.append(t)
            t += step
        return [
            {
                "metric": {"api_provider": p},
                "values": [[t, v] for t, v in zip(ts, vals) if v],
            }
            for p, vals in table.items()
        ]

    def __call__(self, url):
        parsed = urllib.parse.urlparse(url)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        self.calls.append((parsed.path, params))
        q = params["query"]
        is_range = parsed.path.endswith("query_range")
        if "last_over_time" in q:
            if is_range:
                table = {
                    p: ["1" if s else None for s in flags]
                    for p, flags in self.sampled.items()
                }
                result = self._range(params, table)
            else:
                result = vec("api_provider", {p: 1 for p in self.present})
        elif is_range:
            result = self._range(params, self.range_values)
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

    def queries(self, path_end, needle):
        return [
            p
            for path, p in self.fake.calls
            if path.endswith(path_end) and needle in p["query"]
        ]


class CollectTests(FakeTest):
    def test_every_instant_query_uses_one_window_at_until(self):
        self.reading()
        instants = self.queries("/query", "")
        self.assertEqual(len(instants), 6)
        span = int((UNTIL - SINCE).total_seconds())
        for p in instants:
            self.assertEqual(float(p["time"]), UNTIL.timestamp())
            if "remaining_budget" not in p["query"]:
                self.assertIn(f"[{span}s]", p["query"])

    def test_bucket_queries_look_back_exactly_one_bucket(self):
        reading = self.reading()
        (counts,) = self.queries("query_range", "increase")
        (sampled,) = self.queries("query_range", "last_over_time")
        self.assertEqual(
            counts["query"], f"sum by (api_provider) (increase({hr.SUCCESS}[3600s]))"
        )
        self.assertIn(f"last_over_time({hr.SUCCESS}[3600s])", sampled["query"])
        for p in (counts, sampled):
            self.assertEqual(
                float(p["start"]), (SINCE + timedelta(hours=1)).timestamp()
            )
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
        (totals,) = [
            p for p in self.queries("/query", "increase") if hr.SUCCESS in p["query"]
        ]
        self.assertIn("[18000s]", totals["query"])
        (counts,) = self.queries("query_range", "increase")
        self.assertEqual(float(counts["end"]), until.timestamp())
        self.assertEqual(len(reading["buckets"]), 5)

    def test_bucket_longer_than_window_skips_the_table(self):
        reading = self.reading(bucket=timedelta(days=1))
        self.assertEqual(reading["buckets"], [])
        self.assertEqual(self.queries("query_range", ""), [])

    def test_reversed_window_is_refused(self):
        with self.assertRaises(ValueError):
            self.reading(UNTIL, SINCE)

    def test_failed_query_raises(self):
        hr._get = lambda url: {"status": "error", "error": "bad"}
        with self.assertRaises(RuntimeError):
            self.reading()

    def test_no_samples_in_window_is_an_error_not_zero(self):
        self.fake.totals, self.fake.present = {}, []
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

    def test_window_line_has_both_zones_and_utc(self):
        self.assertIn(
            "2026-10-15 08:28 CDT to 2026-10-15 11:58 CDT (13:28Z to 16:58Z), 3.5 h",
            self.out,
        )

    def test_bucket_rows_and_remainder_note(self):
        self.assertIn("| 10-15 09:28 CDT | 100 | 10% |", self.out)
        self.assertIn("| 10-15 10:28 CDT | 100 | 50% |", self.out)
        self.assertIn("| 10-15 11:28 CDT | unknown | unknown |", self.out)  # NaN
        self.assertIn(
            "_The last 30 min are in the totals but not in a bucket._", self.out
        )


class DstRenderTests(FakeTest):
    def make_fake(self):
        five = ["1", "1", "1", "1", "1"]
        return FakeProm(range_values={"openai": five, "anthropic": five})

    def test_repeated_hour_rows_carry_their_zone(self):
        since = datetime(2026, 11, 1, 0, 0, tzinfo=CHI)
        until = datetime(2026, 11, 1, 4, 0, tzinfo=CHI)
        out = hr.render(self.reading(since, until), CHI)
        self.assertIn("| 11-01 01:00 CDT |", out)
        self.assertIn("| 11-01 01:00 CST |", out)
        self.assertIn("2026-11-01 00:00 CDT to 2026-11-01 04:00 CST", out)


class UnknownDataTests(FakeTest):
    def make_fake(self):
        return FakeProm(
            totals={"openai": 100.0, "anthropic": float("nan")},
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
        self.assertIn("| 10-15 09:28 CDT | 6 | 17% |", out)
        self.assertIn("| 10-15 10:28 CDT | no data | no data |", out)
        self.assertIn("| 10-15 11:28 CDT | 7 | 0% |", out)  # Haiku had no samples

    def test_missing_spend_series_is_unknown(self):
        reading = self.reading()
        reading["spend"] = {}
        out = hr.render(reading, CHI)
        self.assertIn(
            "| Haiku spend in window | unknown (no Haiku spend series) |", out
        )


class SampledWithoutIncreaseTests(FakeTest):
    """A Haiku series whose only sample is the window's last has no increase()."""

    def make_fake(self):
        return FakeProm(
            totals={"openai": 100.0},
            present=["openai", "anthropic"],
            range_values={"openai": ["5", "6", "7"], "anthropic": [None, None, None]},
            sampled={"openai": [True, True, True], "anthropic": [False, False, True]},
        )

    def test_total_with_a_sampled_route_missing_is_unknown(self):
        out = hr.render(self.reading(), CHI)
        self.assertIn("| VLM calls | unknown: 100 on gemma, unknown on Haiku", out)
        self.assertIn("(Haiku unknown) |", out)

    def test_bucket_with_a_sampled_route_missing_is_unknown(self):
        out = hr.render(self.reading(), CHI)
        self.assertIn("| 10-15 09:28 CDT | 5 | 0% |", out)
        self.assertIn("| 10-15 11:28 CDT | unknown | unknown |", out)


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

    def test_output_after_ready_keeps_draining(self):
        # 4 MiB is far past any pipe buffer: the child reaches the marker only if
        # someone keeps reading after the ready line.
        with tempfile.TemporaryDirectory() as tmp:
            marker = os.path.join(tmp, "done")
            code = (
                f"import sys, time; {FORWARD}\n"
                "for _ in range(1024): sys.stdout.write('x' * 4095 + chr(10))\n"
                f"sys.stdout.flush(); open({marker!r}, 'w').close(); time.sleep(60)"
            )
            with hr.PortForward(child(code)):
                deadline = time.monotonic() + 10
                while not os.path.exists(marker) and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(os.path.exists(marker))

    def test_child_ignoring_terminate_is_killed(self):
        code = (
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            f"{FORWARD}; time.sleep(60)"
        )
        pf = hr.PortForward(child(code), stop_timeout=0.5)
        with pf:
            pass
        self.assertIsNotNone(pf.proc.poll())

    def test_interrupt_during_startup_stops_the_child(self):
        class InterruptedEvent(threading.Event):
            def wait(self, timeout=None):
                raise KeyboardInterrupt

        orig = hr.threading.Event
        hr.threading.Event = InterruptedEvent
        try:
            pf = hr.PortForward(child("import time; time.sleep(60)"))
            with self.assertRaises(KeyboardInterrupt):
                pf.__enter__()
        finally:
            hr.threading.Event = orig
        self.assertIsNotNone(pf.proc.poll())


if __name__ == "__main__":
    unittest.main()
