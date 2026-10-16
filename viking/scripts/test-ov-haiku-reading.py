"""Tests for ov-haiku-reading.py. Pure (fakes and local child processes) — runs anywhere:

    python3 test-ov-haiku-reading.py

Every Prometheus call goes through the module's single ``_get`` choke point, so these
tests replace it with a fake that holds individual series and answers the script's
sum/count queries over them the way Prometheus would, honours the range query's
start/end/step, and records each query. The port-forward tests run small Python
children in place of kubectl. The live check (the 2026-10-15 weekday reading in
compendium IMPR-1311) is run by hand.
"""

import argparse
import importlib.util
import os
import re
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
NAN = float("nan")
ONE = None  # a series with a single sample in the lookback: no increase() value


def s(label, value, inc):
    return ({label: value}, inc)


def gemma(inc):
    return s("api_provider", "openai", inc)


def haiku(inc):
    return s("api_provider", "anthropic", inc)


WINDOW = {
    hr.SUCCESS: [gemma(4383.4), haiku(978.2)],
    hr.FALLBACKS: [
        s("exception_status", "429", 956.6),
        s("exception_status", "408", 24.0),
    ],
    hr.FAILURES: [gemma(22.0)],
    hr.SPEND: [haiku(0.69), gemma(0.0)],
}
BUCKETS = [[gemma(90), haiku(10)], [gemma(50), haiku(50)], [gemma(NAN), haiku(0)]]


def aggregate(series, op, label):
    """What ``sum by``/``count by`` over increase() or last_over_time() returns."""
    groups = {}
    for labels, inc in series:
        key = labels[label]
        if op == "sampled":
            groups[key] = groups.get(key, 0) + 1
        elif inc is not None:
            groups[key] = groups.get(key, 0) + (1 if op == "counted" else inc)
    return groups


class FakeProm:
    def __init__(self, window=None, buckets=None, budget="88.921"):
        self.calls = []
        self.window = {**WINDOW, **(window or {})}
        self.buckets = BUCKETS if buckets is None else buckets
        self.budget = budget

    def __call__(self, url):
        parsed = urllib.parse.urlparse(url)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        self.calls.append((parsed.path, params))
        q = params["query"]
        if "remaining_budget" in q:
            result = [{"metric": {}, "value": [0, self.budget]}]
            return {"status": "success", "data": {"result": result}}
        label = re.search(r"by \((\w+)\)", q).group(1)
        metric = next(
            m for m in (hr.SUCCESS, hr.FALLBACKS, hr.FAILURES, hr.SPEND) if m in q
        )
        if "last_over_time" in q:
            op = "sampled"
        elif q.startswith("count"):
            op = "counted"
        else:
            op = "sum"
        if parsed.path.endswith("query_range"):
            start, end = float(params["start"]), float(params["end"])
            step = int(params["step"])
            rows = {}
            t, i = start, 0
            while t <= end:
                series = self.buckets[i] if i < len(self.buckets) else []
                for key, v in aggregate(series, op, label).items():
                    rows.setdefault(key, []).append([t, str(v)])
                t, i = t + step, i + 1
            result = [{"metric": {label: k}, "values": v} for k, v in rows.items()]
        else:
            groups = aggregate(self.window[metric], op, label)
            result = [
                {"metric": {label: k}, "value": [0, str(v)]} for k, v in groups.items()
            ]
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

    def out(self, **kw):
        return hr.render(self.reading(**kw), CHI)

    def queries(self, path_end, needle=""):
        return [
            p
            for path, p in self.fake.calls
            if path.endswith(path_end) and needle in p["query"]
        ]


class CollectTests(FakeTest):
    def test_every_instant_query_uses_one_window_at_until(self):
        self.reading()
        instants = self.queries("/query")
        self.assertEqual(len(instants), 13)  # 4 counters x 3 queries + budget
        span = int((UNTIL - SINCE).total_seconds())
        for p in instants:
            self.assertEqual(float(p["time"]), UNTIL.timestamp())
            if "remaining_budget" not in p["query"]:
                self.assertIn(f"[{span}s]", p["query"])

    def test_bucket_queries_look_back_exactly_one_bucket(self):
        reading = self.reading()
        ranges = self.queries("query_range")
        self.assertEqual(
            [p["query"] for p in ranges],
            list(hr.counter_queries(hr.SUCCESS, "api_provider", 3600)),
        )
        self.assertEqual(
            ranges[0]["query"], f"sum by (api_provider) (increase({hr.SUCCESS}[3600s]))"
        )
        for p in ranges:
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
        reading = self.reading(since=since, until=until)
        self.assertEqual(reading["span_s"], 5 * 3600)
        self.assertIn("[18000s]", self.queries("/query", hr.SUCCESS)[0]["query"])
        self.assertEqual(
            float(self.queries("query_range")[0]["end"]), until.timestamp()
        )
        self.assertEqual(len(reading["buckets"]), 5)

    def test_fractional_seconds_are_truncated_at_both_ends(self):
        since = datetime(2026, 10, 15, 13, 0, 0, tzinfo=UTC)
        until = datetime(2026, 10, 15, 14, 0, 0, 900000, tzinfo=UTC)
        reading = self.reading(since=since, until=until)
        self.assertEqual(reading["span_s"], 3600)
        self.assertEqual(reading["until"], "2026-10-15T14:00:00+00:00")
        self.assertEqual(reading["remainder_s"], 0)
        whole = datetime(2026, 10, 15, 14, 0, 0, tzinfo=UTC).timestamp()
        for p in self.queries("/query"):
            self.assertEqual(float(p["time"]), whole)
        self.assertEqual(float(self.queries("query_range")[0]["end"]), whole)

    def test_bucket_longer_than_window_skips_the_table(self):
        reading = self.reading(bucket=timedelta(days=1))
        self.assertEqual(reading["buckets"], [])
        self.assertEqual(self.queries("query_range"), [])

    def test_reversed_window_is_refused(self):
        with self.assertRaises(ValueError):
            self.reading(since=UNTIL, until=SINCE)

    def test_failed_query_raises(self):
        hr._get = lambda url: {"status": "error", "error": "bad"}
        with self.assertRaises(RuntimeError):
            self.reading()

    def test_no_samples_in_window_is_an_error_not_zero(self):
        self.fake.window[hr.SUCCESS] = []
        with self.assertRaises(RuntimeError):
            self.reading()


class RenderTests(FakeTest):
    def setUp(self):
        super().setUp()
        self.text = self.out()

    def test_totals_routes_and_share(self):
        self.assertIn(
            "| VLM calls | 5,362: 4,383 on gemma, 978 on Haiku (Haiku 18%) |", self.text
        )

    def test_failovers_named_by_reason(self):
        self.assertIn(
            "24 × `408` (gemma past 90 s), 957 × `429` (gemma full)", self.text
        )

    def test_spend_threshold_and_failures(self):
        self.assertIn("| Haiku spend in window | $0.69 (router tally", self.text)
        self.assertIn("| Threshold left at window end | $88.92 |", self.text)
        self.assertIn("| Failed responses | 22 on gemma |", self.text)

    def test_window_line_has_both_zones_and_utc(self):
        self.assertIn(
            "2026-10-15 08:28 CDT to 2026-10-15 11:58 CDT (13:28Z to 16:58Z), 3.5 h",
            self.text,
        )

    def test_bucket_rows_and_remainder_note(self):
        self.assertIn("| 10-15 09:28 CDT | 100 | 10% |", self.text)
        self.assertIn("| 10-15 10:28 CDT | 100 | 50% |", self.text)
        self.assertIn("| 10-15 11:28 CDT | unknown | unknown |", self.text)  # NaN
        self.assertIn(
            "_The last 30 min are in the totals but not in a bucket._", self.text
        )


class DstRenderTests(FakeTest):
    def make_fake(self):
        return FakeProm(buckets=[[gemma(1), haiku(1)]] * 5)

    def test_repeated_hour_rows_carry_their_zone(self):
        since = datetime(2026, 11, 1, 0, 0, tzinfo=CHI)
        until = datetime(2026, 11, 1, 4, 0, tzinfo=CHI)
        text = self.out(since=since, until=until)
        self.assertIn("| 11-01 01:00 CDT |", text)
        self.assertIn("| 11-01 01:00 CST |", text)
        self.assertIn("2026-11-01 00:00 CDT to 2026-11-01 04:00 CST", text)


class UnknownDataTests(FakeTest):
    def make_fake(self):
        return FakeProm(
            window={hr.SUCCESS: [gemma(100.0), haiku(NAN)]},
            buckets=[[gemma(5), haiku(1)], [], [gemma(7)]],
            budget="NaN",
        )

    def test_nan_total_is_unknown_not_zero(self):
        text = self.out()
        self.assertIn("| VLM calls | unknown: 100 on gemma, unknown on Haiku", text)
        self.assertIn("(Haiku unknown) |", text)
        self.assertIn("| Threshold left at window end | unknown |", text)

    def test_bucket_with_no_samples_is_no_data(self):
        text = self.out()
        self.assertIn("| 10-15 09:28 CDT | 6 | 17% |", text)
        self.assertIn("| 10-15 10:28 CDT | no data | no data |", text)
        self.assertIn("| 10-15 11:28 CDT | 7 | 0% |", text)  # Haiku had no samples


class IncompleteSeriesTests(FakeTest):
    """Series with samples but no increase(): a new pod's first sample, a counter
    first scraped at the window's end."""

    def test_singleton_route_is_unknown_in_totals_and_buckets(self):
        self.fake.window[hr.SUCCESS] = [gemma(100.0), haiku(ONE)]
        self.fake.buckets = [[gemma(5)], [gemma(6)], [gemma(7), haiku(ONE)]]
        text = self.out()
        self.assertIn("| VLM calls | unknown: 100 on gemma, unknown on Haiku", text)
        self.assertIn("| 10-15 09:28 CDT | 5 | 0% |", text)
        self.assertIn("| 10-15 11:28 CDT | unknown | unknown |", text)

    def test_restart_old_pod_counted_new_pod_singleton_is_unknown(self):
        # The sum alone would show Haiku 50; the per-series check catches the gap.
        self.fake.window[hr.SUCCESS] = [gemma(100.0), haiku(50.0), haiku(ONE)]
        reading = self.reading()
        self.assertIsNone(reading["calls"]["anthropic"])
        self.assertEqual(reading["calls"]["openai"], 100.0)
        self.assertIn("(Haiku unknown) |", hr.render(reading, CHI))

    def test_singleton_failover_and_failure_counters_are_unknown(self):
        self.fake.window[hr.FALLBACKS] = [s("exception_status", "429", ONE)]
        self.fake.window[hr.FAILURES] = [gemma(ONE)]
        text = self.out()
        self.assertIn("| Failovers | unknown × `429` (gemma full) |", text)
        self.assertIn("| Failed responses | unknown on gemma |", text)

    def test_no_failover_samples_is_none_recorded(self):
        self.fake.window[hr.FALLBACKS] = []
        self.assertIn("| Failovers | none recorded |", self.out())

    def test_spend_incomplete_versus_absent(self):
        self.fake.window[hr.SPEND] = [haiku(0.5), haiku(ONE)]
        self.assertIn(
            "| Haiku spend in window | unknown (incomplete spend series) |", self.out()
        )
        self.fake.window[hr.SPEND] = [gemma(0.0)]
        self.assertIn("| Haiku spend in window | no Haiku spend samples |", self.out())


class ParseTests(unittest.TestCase):
    def test_naive_time_takes_the_zone_and_becomes_utc(self):
        t = hr.parse_time("2026-10-15T08:28", CHI)
        self.assertEqual(t.utcoffset(), timedelta(0))
        self.assertEqual(t.hour, 13)

    def test_offset_time_keeps_its_instant(self):
        t = hr.parse_time("2026-10-15T08:28-05:00", CHI)
        self.assertEqual(t, datetime(2026, 10, 15, 13, 28, tzinfo=UTC))

    def test_now_is_whole_seconds(self):
        self.assertEqual(hr.parse_time("now", CHI).microsecond, 0)

    def test_bucket_spec(self):
        self.assertEqual(hr.parse_bucket("6h"), timedelta(hours=6))
        self.assertEqual(hr.parse_bucket("1d"), timedelta(days=1))
        for bad in ("90m", "0h", "0d"):
            with self.assertRaises(argparse.ArgumentTypeError):
                hr.parse_bucket(bad)

    def test_reconcile_rules(self):
        self.assertEqual(
            hr.reconcile({"a": 3.0}, {"a": 2, "b": 1}, {"a": 2}), {"a": 3.0, "b": None}
        )
        self.assertEqual(hr.reconcile({}, {}, {}), {})
        self.assertEqual(hr.reconcile({"a": None}, {"a": 1}, {"a": 1}), {"a": None})


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

    def test_interrupt_while_waiting_for_ready_stops_the_child(self):
        reached = []

        class InterruptedEvent(threading.Event):
            def wait(self, timeout=None):
                reached.append(timeout)
                raise KeyboardInterrupt

        class Interrupted(hr.PortForward):
            def _new_event(self):
                return InterruptedEvent()

        pf = Interrupted(child("import time; time.sleep(60)"), ready_timeout=7.0)
        with self.assertRaises(KeyboardInterrupt):
            pf.__enter__()
        self.assertEqual(reached, [7.0])  # the readiness wait itself was interrupted
        self.assertIsNotNone(pf.proc.poll())

    def test_interrupt_before_the_reader_exists_stops_the_child(self):
        def no_thread(*args, **kwargs):
            raise KeyboardInterrupt

        orig = hr.threading.Thread
        hr.threading.Thread = no_thread
        try:
            pf = hr.PortForward(child("import time; time.sleep(60)"))
            with self.assertRaises(KeyboardInterrupt):
                pf.__enter__()
        finally:
            hr.threading.Thread = orig
        self.assertIsNotNone(pf.proc.poll())


if __name__ == "__main__":
    unittest.main()
