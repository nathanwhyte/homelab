#!/usr/bin/env python3
"""Unit tests for ov-contention-watch.py (BUG-1220). Offline, no cluster, no sockets:
subprocess.run and the watcher's poll clocks (time.time, time.monotonic,
time.clock_gettime, time.sleep) are stubbed. The HH:MMZ line prefix and the log
cursor still read the real clock, and test_real_sigterm_exits_130_with_a_ledger runs
the watcher as a child process against a fake kubectl script."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_scripts" / "lib"))
from git_env import scrub_environ  # noqa: E402

scrub_environ()

import contextlib
import datetime as dt
import importlib.util
import io
import json
import os
import re
import subprocess
import tempfile
import unittest
from unittest import mock

_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _DIR)
_spec = importlib.util.spec_from_file_location(
    "ov_contention_watch", os.path.join(_DIR, "ov-contention-watch.py")
)
cw = importlib.util.module_from_spec(_spec)
sys.modules["ov_contention_watch"] = cw
_spec.loader.exec_module(cw)

POD_A = "openviking-aaa-1"
POD_B = "openviking-bbb-2"
LOCK = "/local/default/user/noot/sessions/cc-1/.path.ovlock"


# kubectl --timestamps stamps must not precede the watcher's own start cursor.
BASE = dt.datetime.now(dt.UTC).replace(microsecond=0) + dt.timedelta(seconds=5)


def stamped(line, sec=0, frac=0):
    t = BASE + dt.timedelta(seconds=sec)
    return f"{t:%Y-%m-%dT%H:%M:%S}.{frac:09d}Z {line}"


def offset_stamped(line, zone="-05:00"):
    """A stamp in a non-UTC zone, as the real cluster prints, for the same instant as BASE."""
    hours = int(zone[:3])
    t = BASE + dt.timedelta(hours=hours)
    return f"{t:%Y-%m-%dT%H:%M:%S}.123456789{zone} {line}"


def joined(*lines):
    return "\n".join(lines)


def wait_line(ms, path=LOCK):
    return (
        f'WARN ragfs::lock::manager manager.rs:988 lock_paths=["{path}"] wait_ms={ms}'
    )


def metrics_text(poll_ok=1, live_age=None, oldest_age=None):
    lines = [
        f"openviking_s3_ovlock_poll_ok {poll_ok}",
        "# TYPE openviking_s3_ovlock_live_age_seconds gauge",
    ]
    if oldest_age is not None:
        lines.append(f"openviking_s3_ovlock_oldest_live_age_seconds {oldest_age}")
    if live_age is not None:
        lines.append(
            "openviking_s3_ovlock_live_age_seconds"
            f'{{key="k1",uri="viking://x/user/noot/y",owner="abcdef012345",lock_type="E"}} {live_age}'
        )
    return "\n".join(lines)


def deploy_json(**over):
    d = {
        "metadata": {
            "annotations": {
                "deployment.kubernetes.io/revision": "17",
                cw.LAST_RESTART_NOTE: "2026-10-14T03:00:00Z",
            },
            "managedFields": [
                {
                    "manager": "kubectl-rollout",
                    "operation": "Update",
                    "time": "2026-10-14T02:59:00Z",
                },
                {
                    "manager": "ov-lock-watchdog",
                    "operation": "Update",
                    "time": "2026-10-14T03:00:01Z",
                },
            ],
        },
        "spec": {
            "template": {
                "metadata": {"annotations": {cw.RESTARTED_AT: "2026-10-14T03:00:00Z"}}
            }
        },
    }
    d.update(over)
    return json.dumps(d)


class FakeKubectl:
    """Stands in for subprocess.run, keyed on the kubectl verb."""

    def __init__(self, pods, logs="", metrics=None, deploy=None, deploy_rc=0):
        self.pods = list(pods)  # one pod name per poll; the last repeats
        # one log body per poll; the last repeats
        self.logs = list(logs) if isinstance(logs, list) else [logs]
        # one metrics body per poll when a list; the last repeats
        self.metrics = metrics if metrics is not None else metrics_text()
        self.deploy = deploy if deploy is not None else deploy_json()
        self.deploy_rc = deploy_rc
        self.calls = []
        self._polls = 0
        self._log_polls = 0
        self._metric_polls = 0

    def __call__(self, args, **kw):
        self.calls.append(list(args))
        ok = lambda out="": subprocess.CompletedProcess(args, 0, out, "")
        if "pods" in args:
            i = min(self._polls, len(self.pods) - 1)
            self._polls += 1
            return ok(self.pods[i])
        if "exec" in args:
            ms = self.metrics if isinstance(self.metrics, list) else [self.metrics]
            i = min(self._metric_polls, len(ms) - 1)
            self._metric_polls += 1
            return ok(ms[i])
        if "logs" in args:
            i = min(self._log_polls, len(self.logs) - 1)
            self._log_polls += 1
            return ok(self.logs[i])
        if "deploy" in args:
            if self.deploy_rc:
                return subprocess.CompletedProcess(
                    args, self.deploy_rc, "", "forbidden"
                )
            return ok(self.deploy)
        raise AssertionError(f"unexpected call {args}")


def sleep_counting_clock_ids():
    """The clock ids cw.mono() may read on this platform (mirrors its choice)."""
    if sys.platform == "darwin" and hasattr(cw.time, "CLOCK_MONOTONIC"):
        return {cw.time.CLOCK_MONOTONIC}
    if hasattr(cw.time, "CLOCK_BOOTTIME"):
        return {cw.time.CLOCK_BOOTTIME}
    return set()


def hms_secs(text):
    h, rest = text.split("h")
    m, rest = rest.split("m")
    return int(h) * 3600 + int(m) * 60 + int(rest.rstrip("s"))


class Clock:
    """time.time and time.monotonic that advance only when sleep is called.

    `skew` steps the wall clock without touching the others. `asleep` is host-sleep
    time: wall and the sleep-counting duration clock advance by it, time.monotonic
    (mach_absolute_time on macOS) does not.
    """

    def __init__(self):
        self.t = 1_000_000.0
        self.skew = 0.0
        self.asleep = 0.0

    def time(self):
        return self.t + self.skew + self.asleep

    def monotonic(self):
        return self.t

    def clock_gettime(self, clock_id):
        # Only the sleep-counting clock mono() should pick for this platform; any
        # other id (CLOCK_UPTIME_RAW, CLOCK_REALTIME, ...) fails the test.
        allowed = sleep_counting_clock_ids()
        if clock_id not in allowed:
            raise AssertionError(f"clock id {clock_id} is not in {allowed}")
        return self.t + self.asleep

    def sleep(self, s):
        self.t += s


def run_watch(fake, polls=1, log=None, wake_wait_ms=cw.DEFAULT_WAKE_WAIT_MS):
    """Run watch() for `polls` polls; return (stdout lines, log lines)."""
    clock = Clock()
    buf = io.StringIO()
    with (
        mock.patch.object(cw.subprocess, "run", fake),
        mock.patch.object(cw.time, "time", clock.time),
        mock.patch.object(cw.time, "sleep", clock.sleep),
        contextlib.redirect_stdout(buf),
    ):
        cw.watch((polls - 0.5) * cw.POLL / 3600, cw.Sink(log), wake_wait_ms)
    logged = []
    if log and os.path.exists(log):
        with open(log, encoding="utf-8") as fh:
            logged = fh.read().splitlines()
    return buf.getvalue().splitlines(), logged


def has(lines, token):
    return [ln for ln in lines if token in ln]


class WaitsBlind(unittest.TestCase):
    """BUG-1282: v0.5.1.1 logs no lock lines, which must not read as no waits."""

    def test_held_lock_without_lock_lines_reports_blind_once(self):
        fake = FakeKubectl([POD_A], metrics=metrics_text(live_age=1000))
        out, _ = run_watch(fake, polls=4)
        self.assertEqual(len(has(out, "WATCH ERROR waits:")), 1)
        self.assertIn("a lock live for 1000s", has(out, "WATCH ERROR waits:")[0])
        (end,) = has(out, "WATCH end:")
        self.assertIn("waits unknown", end)
        self.assertNotIn("waits 0", end)

    def test_an_unlabelled_lock_without_lock_lines_reports_blind(self):
        # Claude R4: locks under the 900 s label threshold are the usual case.
        fake = FakeKubectl([POD_A], metrics=metrics_text(oldest_age=600))
        out, _ = run_watch(fake, polls=4)
        self.assertEqual(len(has(out, "WATCH ERROR waits:")), 1)
        self.assertIn("waits unknown", has(out, "WATCH end:")[0])

    def test_a_lock_too_young_to_have_refreshed_stays_quiet(self):
        fake = FakeKubectl(
            [POD_A], metrics=metrics_text(oldest_age=cw.WAITS_BLIND_MIN_LOCK_AGE - 1)
        )
        out, _ = run_watch(fake, polls=4)
        self.assertEqual(has(out, "WATCH ERROR waits:"), [])

    def test_the_first_poll_alone_does_not_report_blind(self):
        fake = FakeKubectl([POD_A], metrics=metrics_text(live_age=1000))
        out, _ = run_watch(fake, polls=1)
        self.assertEqual(has(out, "WATCH ERROR waits:"), [])

    def test_no_held_lock_stays_quiet(self):
        out, _ = run_watch(FakeKubectl([POD_A]), polls=3)
        self.assertEqual(has(out, "WATCH ERROR waits:"), [])
        self.assertIn("waits 0 (max 0.0s)", has(out, "WATCH end:")[0])

    def test_any_lock_line_counts_as_the_log_being_readable(self):
        fake = FakeKubectl(
            [POD_A], metrics=metrics_text(live_age=1000), logs=[wait_line(1200), ""]
        )
        out, _ = run_watch(fake, polls=3)
        self.assertEqual(has(out, "WATCH ERROR waits:"), [])
        self.assertIn("waits 1", has(out, "WATCH end:")[0])

    def test_a_lock_line_after_the_report_restores_the_figures(self):
        fake = FakeKubectl(
            [POD_A], metrics=metrics_text(live_age=1000), logs=["", "", wait_line(1200)]
        )
        out, _ = run_watch(fake, polls=3)
        self.assertEqual(len(has(out, "WATCH ERROR waits:")), 1)
        self.assertIn("waits 1", has(out, "WATCH end:")[0])

    def test_a_new_pod_must_show_its_own_lock_lines(self):
        # Codex R1: a rollback from a logging image to a counts-only one.
        fake = FakeKubectl(
            [POD_A, POD_B, POD_B, POD_B],
            metrics=metrics_text(live_age=1000),
            logs=[wait_line(1200), ""],
        )
        out, _ = run_watch(fake, polls=4)
        self.assertEqual(len(has(out, "WATCH ERROR waits:")), 1)
        self.assertIn("waits unknown", has(out, "WATCH end:")[0])

    def test_a_blind_pod_keeps_the_run_unknown_after_a_logging_pod(self):
        # Codex R1: a pod that lost the lines is a gap the next pod cannot fill.
        fake = FakeKubectl(
            [POD_A, POD_A, POD_A, POD_B],
            metrics=metrics_text(live_age=1000),
            logs=["", "", "", wait_line(1200)],
        )
        out, _ = run_watch(fake, polls=4)
        self.assertEqual(len(has(out, "WATCH ERROR waits:")), 1)
        self.assertIn("waits unknown", has(out, "WATCH end:")[0])

    def test_each_blind_pod_reports(self):
        fake = FakeKubectl(
            [POD_A, POD_A, POD_B, POD_B], metrics=metrics_text(live_age=1000)
        )
        out, _ = run_watch(fake, polls=4)
        self.assertEqual(len(has(out, "WATCH ERROR waits:")), 2)
        self.assertIn("waits unknown", has(out, "WATCH end:")[0])

    def beats(self, fake, polls=3):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(cw, "BEAT_EVERY", 2),
        ):
            _, logged = run_watch(fake, polls=polls, log=os.path.join(tmp, "e.log"))
        return has(logged, "BEAT")

    def test_a_beat_after_the_report_gives_no_wait_figure(self):
        # Claude R5: the log showed the zeros the error line had just disowned.
        first, later = self.beats(
            FakeKubectl([POD_A], metrics=metrics_text(live_age=1000))
        )
        self.assertIn("waits_total=0 max_wait=0.0s", first)
        self.assertIn("waits_total=unknown max_wait=unknown", later)

    def test_a_beat_keeps_its_figures_when_lock_lines_show(self):
        _, later = self.beats(
            FakeKubectl(
                [POD_A], metrics=metrics_text(live_age=1000), logs=[wait_line(1200), ""]
            )
        )
        self.assertIn("waits_total=1 max_wait=1.2s", later)


class LogParsing(unittest.TestCase):
    def events(self, logs):
        fake = FakeKubectl([POD_A], logs=logs)
        with mock.patch.object(cw.subprocess, "run", fake):
            return cw.log_events(cw.Sink(), POD_A, cw.LogCursor())

    def test_dag_miss_is_not_a_timeout(self):
        logs = joined(
            "ERROR semantic Failed to dispatch directory viking://x: lock acquire timed out",
            "    LockAcquisitionError: could not lock /local/x",
        )
        waits, timeouts, dag_miss, outcomes, failed = self.events(logs)
        self.assertEqual((timeouts, dag_miss, failed), (0, 1, 0))
        self.assertEqual((waits, outcomes), ({}, {}))

    def test_bare_lock_error_still_a_timeout(self):
        logs = joined(
            "ERROR semantic Failed to dispatch directory viking://x: lock acquire timed out",
            "    LockAcquisitionError: could not lock /local/x",
            "ERROR session commit LockAcquisitionError: could not lock /local/y",
        )
        _, timeouts, dag_miss, _, _ = self.events(logs)
        self.assertEqual((timeouts, dag_miss), (1, 1))

    def test_waits_outcomes_and_failure_counted(self):
        logs = joined(
            wait_line(1200),
            wait_line(7000),
            'INFO ov-extract-outcome {"event": "failed", "failure_class": "timeout"}',
            "ERROR Memory extraction failed for s1",
        )
        waits, timeouts, _, outcomes, failed = self.events(logs)
        self.assertEqual(waits["user/noot/sessions/cc-1/.path.ovlock"], (2, 7000))
        self.assertEqual(outcomes, {"failed:timeout": 1})
        self.assertEqual((timeouts, failed), (0, 1))


class WaitRouting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = os.path.join(self.tmp.name, "events.log")

    def test_dag_miss_alone_emits_no_wait_and_no_wake(self):
        logs = joined(
            "ERROR Failed to dispatch directory viking://x: lock acquire timed out",
            "    LockAcquisitionError: could not lock /local/x",
        )
        out, logged = run_watch(FakeKubectl([POD_A], logs=logs), log=self.log)
        self.assertEqual(has(out + logged, " WAIT "), [])
        self.assertTrue(has(logged, "BEAT"))
        self.assertIn("dag_miss=1", has(logged, "BEAT")[0])
        self.assertIn("dag_miss 1", has(out, "WATCH end:")[0])

    def test_routine_wait_and_beat_only_in_log(self):
        out, logged = run_watch(
            FakeKubectl([POD_A], logs=wait_line(6000)), log=self.log
        )
        self.assertEqual(has(out, "WAIT"), [])
        self.assertEqual(has(out, "BEAT"), [])
        self.assertEqual(len(has(logged, "WAIT")), 1)
        self.assertEqual(len(has(logged, "BEAT")), 1)

    def test_wait_below_event_threshold_is_silent(self):
        out, logged = run_watch(
            FakeKubectl([POD_A], logs=wait_line(4999)), log=self.log
        )
        self.assertEqual(has(out + logged, " WAIT "), [])

    def test_wait_threshold_is_inclusive(self):
        out, logged = run_watch(
            FakeKubectl([POD_A], logs=wait_line(60000)), log=self.log
        )
        self.assertEqual(len(has(out, "WAIT")), 1)
        self.assertEqual(len(has(logged, "WAIT")), 1)
        out, _ = run_watch(
            FakeKubectl([POD_A], logs=wait_line(59999)),
            log=os.path.join(self.tmp.name, "b.log"),
        )
        self.assertEqual(has(out, "WAIT"), [])

    def test_wake_wait_ms_is_configurable(self):
        out, _ = run_watch(
            FakeKubectl([POD_A], logs=wait_line(8000)),
            log=self.log,
            wake_wait_ms=8000,
        )
        self.assertEqual(len(has(out, "WAIT")), 1)

    def test_wake_threshold_below_event_threshold_still_wakes(self):
        out, _ = run_watch(
            FakeKubectl([POD_A], logs=wait_line(4000)),
            log=self.log,
            wake_wait_ms=3000,
        )
        self.assertEqual(len(has(out, "WAIT")), 1)

    def test_overlapping_log_reads_count_each_line_once(self):
        a = stamped(wait_line(70000), frac=100)
        b = stamped(wait_line(80000), frac=900)
        out, logged = run_watch(
            FakeKubectl([POD_A], logs=[a, f"{a}\n{b}", f"{a}\n{b}"]),
            polls=3,
            log=self.log,
        )
        self.assertEqual(len(has(logged, " WAIT ")), 2)
        self.assertIn("waits=1 max=70.0s", has(logged, " WAIT ")[0])
        self.assertIn("waits=1 max=80.0s", has(logged, " WAIT ")[1])
        self.assertEqual(len(has(out, " WAIT ")), 2)

    def test_offset_timestamps_are_read_as_utc(self):
        # -05:00 stamps for an instant after the start cursor must dedupe, not replay
        a = offset_stamped(wait_line(70000))
        _out, logged = run_watch(
            FakeKubectl([POD_A], logs=[a, a, a]), polls=3, log=self.log
        )
        self.assertEqual(len(has(logged, " WAIT ")), 1)

    def test_dag_traceback_split_across_polls_is_not_a_timeout(self):
        t1 = stamped(
            "ERROR Failed to dispatch directory viking://x: lock acquire timed out"
        )
        t2 = stamped("    LockAcquisitionError: could not lock /local/x", frac=200)
        out, logged = run_watch(
            FakeKubectl([POD_A], logs=[t1, t2]), polls=2, log=self.log
        )
        self.assertEqual(has(out + logged, " WAIT "), [])
        self.assertIn("dag_miss 1", has(out, "WATCH end:")[0])

    def test_multiline_dag_traceback_split_across_polls(self):
        err = stamped(
            "2026-10-15 16:40:05,1 - ERROR - Failed to dispatch directory viking://x"
        )
        frames = joined(
            stamped("Traceback (most recent call last):", frac=1),
            stamped('  File "dag.py", line 9, in dispatch', frac=2),
            stamped("viking.LockAcquisitionError: could not lock /local/x", frac=3),
        )
        out, logged = run_watch(
            FakeKubectl([POD_A], logs=[err, frames]), polls=2, log=self.log
        )
        self.assertEqual(has(out + logged, " WAIT "), [])
        self.assertIn("dag_miss 1", has(out, "WATCH end:")[0])

    def test_raise_frame_does_not_close_the_dag_traceback(self):
        err = stamped(
            "2026-10-15 16:40:05,1 - ERROR - Failed to dispatch directory viking://x"
        )
        frames = joined(
            stamped("Traceback (most recent call last):", frac=1),
            stamped('  File "lock.py", line 40, in acquire', frac=2),
            stamped('    raise LockAcquisitionError(f"could not lock {p}")', frac=3),
            stamped("viking.LockAcquisitionError: could not lock /local/x", frac=4),
        )
        out, logged = run_watch(
            FakeKubectl([POD_A], logs=[err, frames]), polls=2, log=self.log
        )
        self.assertEqual(has(out + logged, " WAIT "), [])
        self.assertIn("dag_miss 1", has(out, "WATCH end:")[0])

    def test_new_record_closes_an_open_dag_traceback(self):
        err = stamped(
            "2026-10-15 16:40:05,1 - ERROR - Failed to dispatch directory viking://x"
        )
        nxt = stamped("2026-10-15 16:40:06,1 - INFO - ok", sec=1)
        real = stamped("viking.LockAcquisitionError: could not lock /local/y", sec=2)
        out, _ = run_watch(
            FakeKubectl([POD_A], logs=[err, joined(nxt, real)]),
            polls=2,
            log=self.log,
        )
        self.assertIn("timeouts=1", has(out, " WAIT ")[0])

    def test_session_lock_timeout_wakes(self):
        logs = "ERROR session commit LockAcquisitionError: could not lock /local/y"
        out, logged = run_watch(FakeKubectl([POD_A], logs=logs), log=self.log)
        self.assertIn("timeouts=1", has(out, "WAIT")[0])
        self.assertEqual(len(has(logged, "WAIT")), 1)

    def test_failed_outcome_wakes_but_recovered_does_not(self):
        failed = 'ov-extract-outcome {"event": "failed", "failure_class": "timeout"}'
        out, _ = run_watch(FakeKubectl([POD_A], logs=failed), log=self.log)
        self.assertEqual(len(has(out, "WAIT")), 1)
        recovered = 'ov-extract-outcome {"event": "recovered"}'
        out, logged = run_watch(
            FakeKubectl([POD_A], logs=recovered),
            log=os.path.join(self.tmp.name, "b.log"),
        )
        self.assertEqual(has(out, "WAIT"), [])
        self.assertEqual(len(has(logged, "WAIT")), 1)

    def test_memory_extraction_failed_wakes(self):
        out, _ = run_watch(
            FakeKubectl([POD_A], logs="ERROR Memory extraction failed for s1"),
            log=self.log,
        )
        self.assertIn("extract_failed=1", has(out, "WAIT")[0])

    def test_hold_wakes_and_fires_once_per_threshold(self):
        fake = FakeKubectl([POD_A], metrics=metrics_text(live_age=700))
        out, logged = run_watch(fake, polls=2, log=self.log)
        self.assertEqual(len(has(out, "HOLD >= 600s")), 1)
        self.assertEqual(has(out, "HOLD >= 1800s"), [])
        self.assertEqual(len(has(logged, "HOLD")), 1)

    def test_replacement_lease_on_same_path_alerts_again(self):
        def held(owner):
            return metrics_text().replace(
                "openviking_s3_ovlock_poll_ok 1",
                "openviking_s3_ovlock_poll_ok 1\n"
                "openviking_s3_ovlock_live_age_seconds"
                f'{{key="k1",uri="viking://x/user/noot/y",owner="{owner}",lock_type="E"}} 700',
            )

        fake = FakeKubectl([POD_A], metrics=[held("aaaaaaaa"), held("bbbbbbbb")])
        out, _ = run_watch(fake, polls=2, log=self.log)
        self.assertEqual(len(has(out, "HOLD >= 600s")), 2)

    def test_released_then_retaken_lock_alerts_again(self):
        fake = FakeKubectl(
            [POD_A],
            metrics=[
                metrics_text(live_age=700),
                metrics_text(),
                metrics_text(live_age=700),
            ],
        )
        out, _ = run_watch(fake, polls=3, log=self.log)
        self.assertEqual(len(has(out, "HOLD >= 600s")), 2)

    def test_bad_selection_does_not_reset_alert_history(self):
        good = metrics_text(live_age=700)
        bad = good + '\nopenviking_s3_ovlock_live_age_seconds{key="bad"} NaN'
        fake = FakeKubectl([POD_A], metrics=[good, bad, good])
        out, _ = run_watch(fake, polls=3, log=self.log)
        self.assertEqual(len(has(out, "HOLD >= 600s")), 1)
        self.assertEqual(len(has(out, "WATCH ERROR metrics:")), 1)

    def test_decoded_label_newline_keeps_hold_event_on_one_line(self):
        text = metrics_text(live_age=700).replace(
            'uri="viking://x/user/noot/y"', 'uri="viking://x/user/noot/new\\nline"'
        )
        out, _ = run_watch(FakeKubectl([POD_A], metrics=text), log=self.log)
        hold = has(out, "HOLD >= 600s")
        self.assertEqual(len(hold), 1)
        self.assertIn("uri=user/noot/new line", hold[0])
        self.assertFalse(any(line == "line" for line in out))

    def test_watch_error_wakes(self):
        fake = FakeKubectl([POD_A], metrics=metrics_text(poll_ok=0))
        out, logged = run_watch(fake, log=self.log)
        self.assertEqual(len(has(out, "WATCH ERROR metrics")), 1)
        self.assertEqual(len(has(logged, "WATCH ERROR metrics")), 1)

    def test_end_summary_on_stdout_and_log(self):
        out, logged = run_watch(FakeKubectl([POD_A]), log=self.log)
        self.assertEqual(len(has(out, "WATCH end:")), 1)
        self.assertEqual(len(has(logged, "WATCH end:")), 1)
        self.assertEqual(has(out, "WATCH start"), [])
        self.assertEqual(len(has(logged, "WATCH start")), 1)

    def test_every_stdout_line_is_in_the_log(self):
        logs = joined(wait_line(70000), wait_line(300))
        out, logged = run_watch(FakeKubectl([POD_A], logs=logs), log=self.log)
        self.assertTrue(out)
        for line in out:
            self.assertIn(line, logged)
        self.assertGreater(len(logged), len(out))

    def test_without_log_everything_goes_to_stdout(self):
        out, _ = run_watch(FakeKubectl([POD_A], logs=wait_line(6000)))
        for token in ("WATCH start", "WAIT", "BEAT", "WATCH end:"):
            self.assertEqual(len(has(out, token)), 1, token)


class PodTrigger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = os.path.join(self.tmp.name, "events.log")

    def test_pod_line_names_the_trigger(self):
        fake = FakeKubectl([POD_A, POD_B])
        out, logged = run_watch(fake, polls=2, log=self.log)
        (line,) = has(out, "POD changed")
        self.assertIn(f"{POD_A} -> {POD_B}", line)
        self.assertIn("rev=17", line)
        self.assertIn("restartedAt=2026-10-14T03:00:00Z", line)
        self.assertIn("last-restart=2026-10-14T03:00:00Z", line)
        # the newest managedFields entry, not the first
        self.assertIn("managed=ov-lock-watchdog/Update@2026-10-14T03:00:01Z", line)
        self.assertNotIn("kubectl-rollout", line)
        self.assertEqual(len(has(logged, "POD changed")), 1)
        deploy = [c for c in fake.calls if "deploy" in c]
        self.assertEqual(len(deploy), 1)
        self.assertEqual(
            deploy[0][:8],
            ["kubectl", "-n", "viking", "get", "deploy", "openviking", "-o", "json"],
        )
        # kubectl hides managedFields by default
        self.assertIn("--show-managed-fields=true", deploy[0])

    def test_absent_annotations_are_omitted(self):
        bare = json.dumps(
            {
                "metadata": {"annotations": {cw.REVISION: "3"}},
                "spec": {"template": {"metadata": {}}},
            }
        )
        out, _ = run_watch(FakeKubectl([POD_A, POD_B], deploy=bare), polls=2)
        (line,) = has(out, "POD changed")
        self.assertIn("rev=3", line)
        for token in ("restartedAt=", "last-restart=", "managed="):
            self.assertNotIn(token, line)

    def test_deploy_fetch_failure_still_prints_pod_line(self):
        fake = FakeKubectl([POD_A, POD_B], deploy_rc=1)
        out, logged = run_watch(fake, polls=2, log=self.log)
        (line,) = has(out, "POD changed")
        self.assertIn(f"{POD_A} -> {POD_B}", line)
        self.assertNotIn("rev=", line)
        (err,) = has(out, "WATCH ERROR deploy:")
        self.assertIn("exit 1: forbidden", err)
        self.assertEqual(len(has(logged, "WATCH ERROR deploy:")), 1)

    def test_unparsable_deployment_is_a_watch_error(self):
        out, _ = run_watch(FakeKubectl([POD_A, POD_B], deploy="not json"), polls=2)
        self.assertEqual(len(has(out, "POD changed")), 1)
        self.assertEqual(len(has(out, "WATCH ERROR deploy:")), 1)

    def test_first_pod_is_not_a_change_and_skips_deploy_fetch(self):
        fake = FakeKubectl([POD_A])
        out, _ = run_watch(fake, polls=2)
        self.assertEqual(has(out, "POD changed"), [])
        self.assertFalse([c for c in fake.calls if "deploy" in c])


T0 = 1_000_000  # Clock's first reading


def pod_item(name, ready=True, since=0, phase="Running", terminating=False):
    """A pod as `kubectl get pods -o json` shows it; `since` is seconds after T0."""
    when = dt.datetime.fromtimestamp(T0 + since, dt.UTC)
    meta = {"name": name}
    if terminating:
        meta["deletionTimestamp"] = when.strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "metadata": meta,
        "status": {
            "phase": phase,
            "conditions": [
                {
                    "type": "Ready",
                    "status": "True" if ready else "False",
                    "lastTransitionTime": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
            ],
        },
    }


class PodListKubectl(FakeKubectl):
    """FakeKubectl whose `get pods -o json` serves one scripted state per poll.

    A state is a list of pod_item dicts, or None for a failed probe. The last
    state repeats. The Deployment JSON is served only to restart_trigger; the uptime accounting
    does not read it (readyReplicas included).
    """

    def __init__(self, states, exec_fail=(), logs_fail=(), **kw):
        super().__init__([POD_A], **kw)
        self.states = list(states)
        self.exec_fail = set(exec_fail)  # indices of exec calls that fail
        self.logs_fail = set(logs_fail)  # indices of logs calls that fail
        self.fail_stderr = "connection refused"
        self._probes = 0
        self._execs = 0
        self._logs = 0

    def __call__(self, args, **kw):
        for verb, fail, attr in (
            ("exec", self.exec_fail, "_execs"),
            ("logs", self.logs_fail, "_logs"),
        ):
            if verb in args:
                i = getattr(self, attr)
                setattr(self, attr, i + 1)
                if i in fail:
                    self.calls.append(list(args))
                    return subprocess.CompletedProcess(args, 1, "", "refused")
        if "pods" in args and "json" in args:
            self.calls.append(list(args))
            state = self.states[min(self._probes, len(self.states) - 1)]
            self._probes += 1
            if state is None:
                return subprocess.CompletedProcess(args, 1, "", self.fail_stderr)
            return subprocess.CompletedProcess(
                args, 0, json.dumps({"items": state}), ""
            )
        return super().__call__(args, **kw)


class _Capped(Exception):
    pass


def run_target(
    fake,
    target,
    log=None,
    max_polls=500,
    capped_ok=False,
    raise_after=None,
    exc=None,
    caught=None,
    skew_after=None,
    suspend_after=None,
    latency=0.0,
):
    """Run watch() in target-uptime mode; return (stdout lines, log lines, sleeps).

    The third value counts the sleeps, one per poll that did not end the run.

    A run that has not stopped after max_polls fails unless capped_ok is set.
    `exc` is raised from the sleep after `raise_after` polls; watch() re-raises it
    and it is appended to `caught`.
    """
    clock = Clock()
    polls = []

    def sleep(s):
        polls.append(clock.t)
        if skew_after and len(polls) == skew_after[0]:
            clock.skew = skew_after[1]
        if suspend_after and len(polls) == suspend_after[0]:
            clock.asleep = suspend_after[1]
        if exc is not None and len(polls) >= raise_after:
            raise exc
        if len(polls) > max_polls:
            raise _Capped()
        clock.sleep(s)

    def slow(args, **kw):
        clock.t += latency  # every kubectl call takes `latency` seconds
        return fake(args, **kw)

    buf = io.StringIO()
    with (
        mock.patch.object(cw.subprocess, "run", slow if latency else fake),
        mock.patch.object(cw.time, "time", clock.time),
        mock.patch.object(cw.time, "monotonic", clock.monotonic),
        mock.patch.object(cw.time, "clock_gettime", clock.clock_gettime),
        mock.patch.object(cw.time, "sleep", sleep),
        contextlib.redirect_stdout(buf),
    ):
        try:
            cw.watch(None, cw.Sink(log), target_uptime=target)
        except _Capped:
            if not capped_ok:
                raise AssertionError("watcher did not stop at the target") from None
        except (KeyboardInterrupt, RuntimeError) as err:
            if caught is None:
                raise
            caught.append(err)
    logged = []
    if log and os.path.exists(log):
        with open(log, encoding="utf-8") as fh:
            logged = fh.read().splitlines()
    return buf.getvalue().splitlines(), logged, len(polls)


class TargetUptime(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = os.path.join(self.tmp.name, "events.log")

    def test_exits_at_target_with_summary(self):
        # one pod Ready since before the watcher started: uptime counts from the start
        states = [[pod_item(POD_A, since=-3600)]]
        out, logged, polls = run_target(PodListKubectl(states), 300, log=self.log)
        # polls at +0..+300 s; the target is met on the poll at +300 s
        self.assertEqual(polls, 5)
        (end,) = has(out, "WATCH end:")
        self.assertIn("1 segments, uptime 0h05m00s of 0h05m00s", end)
        (seg,) = has(out, "SEGMENT ")
        self.assertIn("SEGMENT 1 pod=openviking-aaa-1", seg)
        self.assertIn("dur=0h05m00s cum=0h05m00s remaining=0h00m00s", seg)
        self.assertTrue(seg.endswith("target reached"))
        self.assertEqual(has(out, "WATCH start"), [])
        self.assertIn("target uptime 0h05m00s", has(logged, "WATCH start")[0])

    def test_pod_change_makes_a_segment_and_sums(self):
        a = [pod_item(POD_A, since=-3600)]
        b = [pod_item(POD_B, since=290)]
        # A seen Ready on polls at +0..+240 s, B Ready since +290 s from the +300 s poll
        states = [a] * 5 + [b]
        out, logged, _ = run_target(PodListKubectl(states), 600, log=self.log)
        segs = has(out, "SEGMENT ")
        self.assertEqual(len(segs), 2)
        self.assertIn("SEGMENT 1 pod=openviking-aaa-1", segs[0])
        self.assertIn("dur=0h04m00s cum=0h04m00s remaining=0h06m00s", segs[0])
        # B went Ready after A's last poll: coverage broke between polls
        self.assertIn("unobserved=0h01m00s", segs[0])
        self.assertIn("Ready lost between polls", segs[0])
        self.assertIn("rev=17", segs[0])  # restart trigger rides on the line
        # B is counted from the poll at +300 s that first saw it, not from its
        # transition: 240 + 360 s
        self.assertIn("SEGMENT 2 pod=openviking-bbb-2", segs[1])
        self.assertIn("dur=0h06m00s cum=0h10m00s remaining=0h00m00s", segs[1])
        self.assertEqual(has(out, "OV DOWN"), [])
        self.assertEqual(has(out, "OV BACK"), [])
        self.assertIn(
            "2 segments, uptime 0h10m00s of 0h10m00s", has(out, "WATCH end:")[0]
        )
        self.assertEqual(len(has(logged, "SEGMENT ")), 2)

    def test_segment_line_carries_last_lock_state(self):
        a = [pod_item(POD_A, since=-3600)]
        fake = PodListKubectl(
            [a, a, [pod_item(POD_B, since=100)]],
            metrics=metrics_text(live_age=700),
        )
        out, _, _ = run_target(fake, 400, log=self.log)
        self.assertIn("live=1 oldest=0s", has(out, "SEGMENT 1")[0])

    def test_outage_is_one_down_and_one_back(self):
        a = [pod_item(POD_A, since=-3600)]
        gone = [pod_item(POD_A, ready=False, since=130, terminating=True)]
        # Ready for 3 polls, then NotReady, 4 failed probes, then B
        states = [a] * 3 + [gone] + [None] * 4 + [[pod_item(POD_B, since=600)]]
        out, logged, _ = run_target(PodListKubectl(states), 900, log=self.log)
        self.assertEqual(len(has(out, "OV DOWN")), 1)
        self.assertIn("openviking-aaa-1 terminating", has(out, "OV DOWN")[0])
        self.assertEqual(len(has(out, "OV BACK")), 1)
        self.assertIn("OV BACK openviking-bbb-2", has(out, "OV BACK")[0])
        # the probe failures during the outage never reach stdout, only the log
        self.assertEqual(has(out, "WATCH ERROR"), [])
        self.assertEqual(len(has(logged, "WATCH ERROR pods")), 4)
        self.assertEqual(len(has(logged, "OV DOWN")), 1)
        self.assertEqual(len(has(logged, "OV BACK")), 1)
        # segment A closed at its last Ready poll (+120 s), once
        segs = has(out, "SEGMENT ")
        self.assertIn("pod=openviking-aaa-1", segs[0])
        self.assertIn("dur=0h02m00s", segs[0])

    def test_same_pod_with_a_fresh_transition_is_back(self):
        a = [pod_item(POD_A, since=-3600)]
        down = [pod_item(POD_A, ready=False, since=130)]
        again = [pod_item(POD_A, since=250)]
        out, _, _ = run_target(PodListKubectl([a, a, down, down, again]), 500)
        (back,) = has(out, "OV BACK")
        self.assertIn("OV BACK openviking-aaa-1", back)
        self.assertEqual(len(has(out, "SEGMENT ")), 2)

    def test_probe_failure_while_up_wakes_once_per_streak(self):
        a = [pod_item(POD_A, since=-3600)]
        out, logged, _ = run_target(
            PodListKubectl([a, None, None, None, a]), 300, log=self.log
        )
        self.assertEqual(len(has(out, "WATCH ERROR pods")), 1)
        self.assertEqual(len(has(logged, "WATCH ERROR pods")), 3)
        self.assertEqual(has(out, "OV DOWN"), [])

    def test_rolling_update_overlap_keeps_accounting_and_collection(self):
        a = pod_item(POD_A, since=-3600)
        b = pod_item(POD_B, since=100)
        # A alone, five polls with A and B Ready, B alone, then the pods are gone
        states = [[a]] + [[a, b]] * 5 + [[b], []]
        fake = PodListKubectl(states)
        out, logged, _ = run_target(
            fake, 3600, log=self.log, max_polls=9, capped_ok=True
        )
        self.assertEqual(has(out + logged, "WATCH ERROR"), [])
        # metrics were read on all seven polls that had a Ready pod
        self.assertEqual(len([c for c in fake.calls if "exec" in c]), 7)
        # coverage never dropped, so the handoff is not a segment boundary
        (seg,) = has(out, "SEGMENT ")
        self.assertIn("pod=openviking-aaa-1,openviking-bbb-2", seg)
        self.assertIn("dur=0h06m00s cum=0h06m00s", seg)
        self.assertEqual(len(has(out, "OV DOWN")), 1)
        # the handoff itself is a log-only line
        self.assertEqual(has(out, "POD ready"), [])
        self.assertTrue(has(logged, "POD ready"))

    def test_handoff_replay_loses_no_time(self):
        a = pod_item(POD_A, since=-60)
        b = pod_item(POD_B, since=30)
        c = pod_item("openviking-ccc-3", since=90)
        # A at +0, A+B at +60 (B Ready since +30), B+C at +120 (C Ready since +90)
        out, _, polls = run_target(PodListKubectl([[a], [a, b], [b, c]]), 120)
        (seg,) = has(out, "SEGMENT ")
        self.assertIn("pod=openviking-aaa-1,openviking-bbb-2,openviking-ccc-3", seg)
        self.assertIn("dur=0h02m00s", seg)
        self.assertEqual(polls, 2)
        self.assertIn(
            "1 segments, uptime 0h02m00s of 0h02m00s", has(out, "WATCH end")[0]
        )

    def test_interrupt_inside_the_segment_emit_counts_the_interval_once(self):
        a = [pod_item(POD_A, since=-3600)]
        gone = [pod_item(POD_A, ready=False, since=130)]
        raised = []
        real = cw.Sink.emit

        def emit(self, line, wake=True):
            if line.startswith("SEGMENT") and not raised:
                raised.append(line)
                raise KeyboardInterrupt
            return real(self, line, wake)

        caught = []
        with mock.patch.object(cw.Sink, "emit", emit):
            out, _, _ = run_target(
                PodListKubectl([a, a, a, gone]),
                3600,
                log=os.path.join(self.tmp.name, "i.log"),
                caught=caught,
            )
        self.assertEqual(len(raised), 1)
        (end,) = has(out, "WATCH end:")
        self.assertIn("1 segments, uptime 0h02m00s", end)
        # the SEGMENT line the interrupt cut off is written by the finalizer
        self.assertEqual(len(has(out, "SEGMENT 1 ")), 1)

    def test_second_interrupt_in_the_finalizer_still_marks_watch_end(self):
        a = [pod_item(POD_A, since=-3600)]
        raised = []
        real = cw.Sink.emit

        def emit(self, line, wake=True):
            if line.startswith("SEGMENT") and not raised:
                raised.append(line)
                raise KeyboardInterrupt
            return real(self, line, wake)

        caught = []
        with mock.patch.object(cw.Sink, "emit", emit):
            out, _, _ = run_target(
                PodListKubectl([a]),
                3600,
                raise_after=3,
                exc=KeyboardInterrupt(),
                caught=caught,
            )
        (end,) = has(out, "WATCH end:")
        self.assertIn("incomplete (interrupted), 1 segments, uptime 0h02m00s", end)
        self.assertEqual(len(has(out, "SEGMENT 1 ")), 1)

    def test_finalizer_reads_no_clock_when_nothing_is_open(self):
        a = [pod_item(POD_A, since=-3600)]
        calls = []
        real = cw.instant

        def counting():
            calls.append(1)
            return real()

        with mock.patch.object(cw, "instant", counting):
            _, _, sleeps = run_target(PodListKubectl([a]), 120)
        # one per poll (3), one for the target SEGMENT; none in the finalizer
        self.assertEqual(len(calls), sleeps + 2)

    def test_host_sleep_is_counted(self):
        a = [pod_item(POD_A, since=-3600)]
        # the host sleeps 8 h after the third poll; the pod stays Ready throughout
        out, _, _ = run_target(PodListKubectl([a]), 600, suspend_after=(3, 8 * 3600.0))
        (seg,) = has(out, "SEGMENT ")
        self.assertIn("dur=8h03m00s", seg)
        self.assertIn("uptime 8h03m00s of 0h10m00s", has(out, "WATCH end")[0])

    def test_hours_mode_error_text_is_unchanged(self):
        def failing(args, **kw):
            return subprocess.CompletedProcess(args, 1, "", "first\n  second  third")

        out, _ = run_watch(failing, polls=1)
        # --hours keeps the raw stderr, newline and spacing included
        self.assertIn("  second  third", out)
        self.assertIn(True, ["WATCH ERROR pod: exit 1: first" in ln for ln in out])

    def test_failed_probes_in_a_continuous_pod_are_counted(self):
        a = [pod_item(POD_A, since=-3600)]
        out, _, _ = run_target(PodListKubectl([a, None, None, a, a, a]), 300)
        (seg,) = has(out, "SEGMENT ")
        self.assertIn("dur=0h05m00s", seg)

    def test_down_after_failed_probes_reports_the_unobserved_interval(self):
        a = [pod_item(POD_A, since=-3600)]
        gone = [pod_item(POD_A, ready=False, since=130)]
        # Ready at +0 and +60, ten failed probes, then NotReady at +660
        states = [a, a] + [None] * 9 + [gone]
        out, _, _ = run_target(
            PodListKubectl(states), 3600, max_polls=12, capped_ok=True
        )
        (seg,) = has(out, "SEGMENT ")
        self.assertIn("dur=0h01m00s", seg)
        self.assertIn("unobserved=0h10m00s", seg)

    def test_probe_failures_are_woken_once_per_streak(self):
        a = [pod_item(POD_A, since=-3600)]
        for verb, fail_kw in (("metrics", "exec_fail"), ("logs", "logs_fail")):
            with self.subTest(verb):
                log = os.path.join(self.tmp.name, f"{verb}.log")
                # five failures in a row, a success, then two more failures
                fake = PodListKubectl([a], **{fail_kw: {0, 1, 2, 3, 4, 6, 7}})
                out, logged, _ = run_target(fake, 540, log=log)
                token = f"WATCH ERROR {verb}"
                self.assertEqual(len(has(out, token)), 2)
                self.assertEqual(len(has(logged, token)), 7)

    def test_final_sample_is_read_before_the_summary(self):
        a = [pod_item(POD_A, since=-3600)]
        fake = PodListKubectl(
            [a],
            metrics=[metrics_text(), metrics_text(), metrics_text(live_age=700)],
            logs=["", "", wait_line(7000)],
        )
        out, _, _ = run_target(fake, 120, log=self.log)
        # the target is met on the third poll, which still reads metrics and logs
        self.assertEqual(len([c for c in fake.calls if "exec" in c]), 3)
        self.assertEqual(len([c for c in fake.calls if "logs" in c]), 3)
        self.assertEqual(len(has(out, "HOLD >= 600s")), 1)
        (end,) = has(out, "WATCH end:")
        self.assertIn("peak lease 700s, waits 1 (max 7.0s)", end)
        self.assertIn("live=1 oldest=0s", has(out, "SEGMENT 1")[0])

    def test_stale_ready_at_startup_counts_from_the_first_covering_poll(self):
        nr = [pod_item(POD_A, ready=False, since=-10)]
        stale = [pod_item(POD_A, since=-3600)]
        # NotReady at +0 and +60; a Ready condition from an hour before startup is
        # seen at +120: coverage is observed there, so counting starts there
        out, _, _ = run_target(PodListKubectl([nr, nr, stale]), 180)
        (back,) = has(out, "OV BACK")
        self.assertIn("OV BACK openviking-aaa-1", back)
        (seg,) = has(out, "SEGMENT ")
        self.assertIn("dur=0h03m00s", seg)

    def test_same_second_flap_is_a_new_segment_by_observation(self):
        a = [pod_item(POD_A, since=-3600)]
        nr = [pod_item(POD_A, ready=False, since=0)]
        # Ready, NotReady at the next poll, then Ready with the identical transition
        # string: no inference from the string, the outage gap is not counted
        out, _, _ = run_target(PodListKubectl([a, nr, a]), 120)
        segs = has(out, "SEGMENT ")
        self.assertEqual(len(segs), 2)
        self.assertIn("SEGMENT 1 ", segs[0])
        self.assertIn("dur=0h00m00s", segs[0])
        self.assertIn("dur=0h02m00s", segs[1])
        self.assertEqual(len(has(out, "OV DOWN")), 1)
        self.assertEqual(len(has(out, "OV BACK")), 1)

    def test_restart_within_one_poll_is_a_boundary(self):
        a = [pod_item(POD_A, since=-3600)]
        # a different transition string at the next poll: not continuous
        a2 = [pod_item(POD_A, since=30)]
        out, _, _ = run_target(PodListKubectl([a, a, a2, a2, a2]), 120)
        segs = has(out, "SEGMENT ")
        self.assertIn("dur=0h01m00s", segs[0])
        self.assertIn("Ready lost between polls", segs[0])
        self.assertEqual(has(out, "OV DOWN"), [])

    def test_unchanged_transition_bridges_failed_probes(self):
        a = [pod_item(POD_A, since=-3600)]
        out, _, _ = run_target(PodListKubectl([a, None, None, None, a]), 240)
        (seg,) = has(out, "SEGMENT ")
        self.assertIn("dur=0h04m00s", seg)

    def test_metrics_streak_survives_pods_failures_that_skip_it(self):
        a = [pod_item(POD_A, since=-3600)]
        states = [a, None, a, None, a, None, a]
        fake = PodListKubectl(states, metrics=metrics_text(poll_ok=0))
        out, logged, _ = run_target(fake, 360, log=self.log)
        # metrics runs on 4 polls and fails each time: one stdout line
        self.assertEqual(len(has(out, "WATCH ERROR metrics")), 1)
        self.assertEqual(len(has(logged, "WATCH ERROR metrics")), 4)
        # each pods failure follows a successful pods probe: a streak of its own
        self.assertEqual(len(has(out, "WATCH ERROR pods")), 3)

    def test_pods_failure_streaks_are_counted_per_streak(self):
        a = [pod_item(POD_A, since=-3600)]
        states = [a, None, None, None, a, None, None, a]
        out, logged, _ = run_target(PodListKubectl(states), 420, log=self.log)
        self.assertEqual(len(has(out, "WATCH ERROR pods")), 2)
        self.assertEqual(len(has(logged, "WATCH ERROR pods")), 5)

    def test_deploy_error_goes_through_the_gate(self):
        a = [pod_item(POD_A, since=-3600)]
        b = [pod_item(POD_B, since=30)]
        c = [pod_item("openviking-ccc-3", since=60)]
        fake = PodListKubectl([a, b, c, c, c], deploy_rc=1)
        out, logged, _ = run_target(fake, 120, log=self.log)
        # two boundaries, two failed deploy fetches: one streak, one stdout line
        self.assertEqual(len(has(out, "WATCH ERROR deploy")), 1)
        self.assertEqual(len(has(logged, "WATCH ERROR deploy")), 2)

    def test_bad_exporter_reads_never_become_lock_state(self):
        a = [pod_item(POD_A, since=-3600)]
        out, _, _ = run_target(
            PodListKubectl([a], metrics=metrics_text(poll_ok=0)), 120
        )
        self.assertIn("locks=unknown", has(out, "SEGMENT ")[0])

    def test_a_bad_read_keeps_the_last_good_lock_state(self):
        a = [pod_item(POD_A, since=-3600)]
        fake = PodListKubectl(
            [a],
            metrics=[
                metrics_text(live_age=700),
                metrics_text(poll_ok=0),
                metrics_text(poll_ok=0),
            ],
        )
        out, _, _ = run_target(fake, 120)
        self.assertIn("live=1 oldest=0s", has(out, "SEGMENT ")[0])
        # HOLD alerting is not changed by this: it still sees the first read
        self.assertEqual(len(has(out, "HOLD >= 600s")), 1)

    def test_missing_or_malformed_selection_keeps_last_good_state(self):
        good = metrics_text(live_age=700)
        for bad in [
            "openviking_s3_ovlock_poll_ok 1",
            good + '\nopenviking_s3_ovlock_live_age_seconds{key="bad"} NaN',
        ]:
            with self.subTest(bad=bad):
                a = [pod_item(POD_A, since=-3600)]
                fake = PodListKubectl([a], metrics=[good, bad, bad])
                out, _, _ = run_target(fake, 120, log=self.log)
                self.assertIn("live=1 oldest=0s", has(out, "SEGMENT ")[0])
                self.assertEqual(len(has(out, "WATCH ERROR metrics:")), 1)

    def test_missing_selection_is_unknown_from_startup(self):
        a = [pod_item(POD_A, since=-3600)]
        out, _, _ = run_target(
            PodListKubectl([a], metrics="openviking_s3_ovlock_poll_ok 1"), 120
        )
        self.assertIn("locks=unknown", has(out, "SEGMENT ")[0])

    def test_log_only_deploy_failure_does_not_open_the_streak(self):
        a = [pod_item(POD_A, since=-3600)]
        a_term = [pod_item(POD_A, since=-3600, terminating=True)]
        b = [pod_item(POD_B, since=10)]
        c = [pod_item("openviking-ccc-3", since=20)]
        states = [a] * 3 + [a_term] + [b] * 3 + [c] * 3
        fake = PodListKubectl(states, deploy_rc=1)
        out, logged, _ = run_target(
            fake, 3600, log=self.log, max_polls=12, capped_ok=True
        )
        # the fetch at OV DOWN is log-only; the next one, with OV up, wakes
        self.assertEqual(len(has(logged, "WATCH ERROR deploy")), 2)
        self.assertEqual(len(has(out, "WATCH ERROR deploy")), 1)

    def test_wall_clock_step_does_not_change_durations(self):
        a = [pod_item(POD_A, since=-3600)]
        # the wall clock steps back 2 h 5 m after the second poll
        out, _, polls = run_target(PodListKubectl([a]), 180, skew_after=(2, -7500.0))
        self.assertEqual(polls, 3)
        (seg,) = has(out, "SEGMENT ")
        self.assertIn("dur=0h03m00s cum=0h03m00s remaining=0h00m00s", seg)
        self.assertIn(
            "1 segments, uptime 0h03m00s of 0h03m00s", has(out, "WATCH end")[0]
        )

    def test_interrupt_during_the_target_segment_write_is_incomplete(self):
        a = [pod_item(POD_A, since=-3600)]
        raised = []
        real = cw.Sink.emit

        def emit(self, line, wake=True):
            if line.startswith("SEGMENT") and not raised:
                raised.append(line)
                raise KeyboardInterrupt
            return real(self, line, wake)

        caught = []
        with mock.patch.object(cw.Sink, "emit", emit):
            out, _, _ = run_target(
                PodListKubectl([a]), 120, log=self.log, caught=caught
            )
        self.assertEqual(len(caught), 1)
        (end,) = has(out, "WATCH end:")
        self.assertIn("incomplete (interrupted)", end)
        self.assertNotIn("target reached", end)
        self.assertEqual(len(has(out, "SEGMENT 1 ")), 1)

    def test_interrupt_during_the_start_line_still_writes_watch_end(self):
        a = [pod_item(POD_A, since=-3600)]
        raised = []
        real = cw.Sink.emit

        def emit(self, line, wake=True):
            if line.startswith("WATCH start") and not raised:
                raised.append(line)
                raise KeyboardInterrupt
            return real(self, line, wake)

        caught = []
        with mock.patch.object(cw.Sink, "emit", emit):
            out, _, _ = run_target(PodListKubectl([a]), 120, caught=caught)
        self.assertEqual(len(caught), 1)
        (end,) = has(out, "WATCH end:")
        self.assertIn("incomplete (interrupted), 0 segments", end)

    def test_multiline_stderr_is_one_stdout_line(self):
        a = [pod_item(POD_A, since=-3600)]
        fake = PodListKubectl([a, None, a])
        fake.fail_stderr = "first line\nsecond line\n\n  third"
        out, logged, _ = run_target(fake, 120, log=self.log)
        (err,) = has(out, "WATCH ERROR pods")
        self.assertIn("first line second line third", err)
        for line in out + logged:
            self.assertRegex(line, r"^\d\d:\d\dZ ")

    def test_host_sleep_shows_as_bridged(self):
        a = [pod_item(POD_A, since=-3600)]
        out, _, _ = run_target(PodListKubectl([a]), 600, suspend_after=(3, 8 * 3600.0))
        (seg,) = has(out, "SEGMENT ")
        # the whole 8 h 1 m gap between the two polls is bridged
        self.assertIn("dur=8h03m00s", seg)
        self.assertIn("unobserved=0h00m00s bridged=8h01m00s", seg)
        self.assertIn("bridged 8h01m00s", has(out, "WATCH end")[0])

    def test_failed_probe_run_over_two_polls_is_bridged(self):
        a = [pod_item(POD_A, since=-3600)]
        out, _, _ = run_target(PodListKubectl([a, None, None, None, a, a]), 300)
        (seg,) = has(out, "SEGMENT ")
        self.assertIn("dur=0h05m00s", seg)
        self.assertIn("bridged=0h04m00s", seg)

    def test_normal_polls_have_nothing_bridged(self):
        a = [pod_item(POD_A, since=-3600)]
        # with no latency a single failed probe makes exactly 120 s: not over (strict >)
        out, _, _ = run_target(PodListKubectl([a, None, a, a, a, a]), 300)
        (seg,) = has(out, "SEGMENT ")
        self.assertIn("bridged=0h00m00s", seg)
        self.assertIn("bridged 0h00m00s", has(out, "WATCH end")[0])

    def test_bridged_total_is_the_sum_over_segments(self):
        a = [pod_item(POD_A, since=-3600)]
        gone = [pod_item(POD_A, ready=False, since=130)]
        b = [pod_item(POD_B, since=400)]
        states = [a, None, None, None, a, gone, b, None, None, None, b, b]
        out, _, _ = run_target(PodListKubectl(states), 540)
        segs = has(out, "SEGMENT ")
        self.assertEqual(len(segs), 2)
        parts = [hms_secs(re.search(r"bridged=(\S+)", x).group(1)) for x in segs]
        self.assertEqual(parts, [240, 240])
        for x in segs:
            self.assertLessEqual(
                hms_secs(re.search(r"bridged=(\S+)", x).group(1)),
                hms_secs(re.search(r"dur=(\S+)", x).group(1)),
            )
        total = hms_secs(
            re.search(r"bridged (\S+),", has(out, "WATCH end")[0]).group(1)
        )
        self.assertEqual(total, sum(parts))

    def test_mono_reads_only_the_sleep_counting_clock(self):
        clock = Clock()
        clock.asleep = 500.0
        with mock.patch.object(cw.time, "clock_gettime", clock.clock_gettime):
            if sleep_counting_clock_ids():
                self.assertEqual(cw.mono(), clock.t + 500.0)

    def test_interrupt_at_the_finalizer_status_pickup_is_still_marked(self):
        a = [pod_item(POD_A, since=-3600)]

        class SysProxy:
            armed = False

            def __getattr__(self, name):
                return getattr(sys, name)

            def exception(self):
                if self.armed:
                    self.armed = False
                    raise KeyboardInterrupt
                return sys.exception()

        proxy = SysProxy()
        real = cw.ReadyTracker.finish

        def finish(self, now, note=""):
            real(self, now, note)
            proxy.armed = True  # the next sys.exception() is the finalizer's

        caught = []
        with (
            mock.patch.object(cw, "sys", proxy),
            mock.patch.object(cw.ReadyTracker, "finish", finish),
        ):
            out, _, _ = run_target(PodListKubectl([a]), 120, caught=caught)
        (end,) = has(out, "WATCH end:")
        self.assertIn("incomplete (interrupted)", end)
        self.assertEqual(len(caught), 1)

    def test_status_is_taken_from_the_exception_when_the_handler_is_cut(self):
        a = [pod_item(POD_A, since=-3600)]
        calls = []
        real = cw.outcome

        def outcome(exc):
            calls.append(exc)
            if len(calls) == 1:
                raise KeyboardInterrupt  # cut inside the except clause
            return real(exc)

        caught = []
        with mock.patch.object(cw, "outcome", outcome):
            out, _, _ = run_target(
                PodListKubectl([a]),
                3600,
                raise_after=3,
                exc=RuntimeError("boom"),
                caught=caught,
            )
        (end,) = has(out, "WATCH end:")
        self.assertIn("incomplete (interrupted)", end)

    def test_interrupt_cutting_the_watch_end_write_is_retried(self):
        a = [pod_item(POD_A, since=-3600)]
        raised = []
        real = cw.Sink.emit

        def emit(self, line, wake=True):
            if line.startswith("WATCH end") and not raised:
                raised.append(line)
                raise KeyboardInterrupt
            return real(self, line, wake)

        caught = []
        with mock.patch.object(cw.Sink, "emit", emit):
            out, _, _ = run_target(
                PodListKubectl([a]), 120, log=self.log, caught=caught
            )
        # the first write was cut: the retry writes it once, marked incomplete, and
        # the interrupt still propagates (exit 130 in main)
        (end,) = has(out, "WATCH end:")
        self.assertIn("incomplete (interrupted)", end)
        self.assertEqual(len(caught), 1)

    def test_one_failed_probe_with_real_latency_is_bridged(self):
        a = [pod_item(POD_A, since=-3600)]
        # 0.5 s per call: a full poll takes 61.5 s, a failed one 60.5 s, so the
        # two-poll gap is 122 s, over the 120 s threshold
        out, _, _ = run_target(PodListKubectl([a, None, a, a]), 300, latency=0.5)
        (seg,) = has(out, "SEGMENT ")
        self.assertIn("bridged=0h02m02s", seg)

    def test_a_gap_between_two_and_four_polls_is_bridged(self):
        a = [pod_item(POD_A, since=-3600)]
        # 15 s per call: 105 s for a full poll, 75 s for a failed one: a 180 s gap
        out, _, _ = run_target(PodListKubectl([a, None, a, a]), 400, latency=15.0)
        (seg,) = has(out, "SEGMENT ")
        self.assertIn("bridged=0h03m00s", seg)

    def test_printed_bridged_values_add_up_to_the_total(self):
        a = [pod_item(POD_A, since=-3600)]
        gone = [pod_item(POD_A, ready=False, since=130)]
        b = [pod_item(POD_B, since=400)]
        # a 121.6 s gap in each of two segments: each prints 2m02s, total 4m04s
        states = [a, None, a, gone, b, None, b, b]
        out, _, _ = run_target(PodListKubectl(states), 400, latency=0.4)
        segs = has(out, "SEGMENT ")
        parts = [hms_secs(re.search(r"bridged=(\S+)", x).group(1)) for x in segs]
        self.assertEqual(parts, [122, 122])
        total = hms_secs(
            re.search(r"bridged (\S+),", has(out, "WATCH end")[0]).group(1)
        )
        self.assertEqual(total, sum(parts))

    def test_error_then_interrupt_inside_finish_keeps_the_open_segment(self):
        a = [pod_item(POD_A, since=-3600)]
        real = cw.ReadyTracker.finish
        calls = []

        def finish(self, now, note=""):
            calls.append(1)
            if len(calls) == 1:
                raise KeyboardInterrupt  # cut before the segment is closed
            return real(self, now, note)

        caught = []
        with mock.patch.object(cw.ReadyTracker, "finish", finish):
            out, _, _ = run_target(
                PodListKubectl([a]),
                3600,
                raise_after=3,
                exc=RuntimeError("boom"),
                caught=caught,
            )
        (end,) = has(out, "WATCH end:")
        self.assertIn("incomplete (error: boom), 1 segments, uptime 0h02m00s", end)
        self.assertEqual(len(has(out, "SEGMENT 1 ")), 1)

    def test_multiline_exception_text_stays_on_one_line(self):
        a = [pod_item(POD_A, since=-3600)]
        caught = []
        out, logged, _ = run_target(
            PodListKubectl([a]),
            3600,
            log=self.log,
            raise_after=3,
            exc=RuntimeError("first\nsecond  third"),
            caught=caught,
        )
        (end,) = has(out, "WATCH end:")
        self.assertIn("incomplete (error: first second third), 1 segments", end)
        for line in out + logged:
            self.assertRegex(line, r"^\d\d:\d\dZ ")

    def test_hours_mode_still_ends_by_the_clock(self):
        out, _ = run_watch(FakeKubectl([POD_A]), polls=2)
        self.assertEqual(has(out, "SEGMENT"), [])
        self.assertNotIn("segments", has(out, "WATCH end:")[0])


class UptimeRule(unittest.TestCase):
    """The observation-only rule against an independent implementation."""

    def test_last_soak_segment_c_replay(self):
        start = dt.datetime(2026, 10, 21, 22, 7, 19, tzinfo=dt.UTC)
        end = dt.datetime(2026, 10, 22, 0, 26, 51, tzinfo=dt.UTC)
        lines = []

        class Lines:
            def emit(self, line, wake=True):
                lines.append(line)

            def ok(self, probe):
                pass

        def at(wall):
            return cw.Instant(wall, (wall - start).total_seconds())

        t = cw.ReadyTracker(72 * 3600, Lines())
        state = ("ready", [(POD_A, "2026-10-21T20:00:00Z")])
        with mock.patch.object(cw, "ready_state", return_value=state):
            t.poll(at(start))
            t.poll(at(end))
        t.finish(at(end))
        self.assertEqual(cw.fmt_dur(t.closed), "2h19m32s")
        self.assertIn("start=2026-10-21T22:07:19Z end=2026-10-22T00:26:51Z", lines[0])
        self.assertIn("dur=2h19m32s cum=2h19m32s remaining=69h40m28s", lines[0])

    def test_matches_an_independent_implementation(self):
        import random

        t0 = dt.datetime.fromtimestamp(T0, dt.UTC)
        names = ["p0", "p1", "p2", "p3"]
        strings = [-100, 0, 60, 120, 200]
        bridged = 0
        for seed in range(400):
            rng = random.Random(seed)
            n = rng.randint(4, 25)
            states, outcomes = [], []
            for _ in range(n + 1):
                r = rng.random()
                if r < 0.15:
                    states.append(None)
                    outcomes.append(None)
                elif r < 0.3:
                    states.append([pod_item(rng.choice(names), ready=False)])
                    outcomes.append(frozenset())
                else:
                    pods = {
                        p: rng.choice(strings)
                        for p in rng.sample(names, rng.randint(1, 3))
                    }
                    states.append([pod_item(p, since=v) for p, v in pods.items()])
                    outcomes.append(frozenset(pods.items()))
            expected, prev = 0, None  # prev: (poll index, covering set)
            for k, cover in enumerate(outcomes):
                if cover is None:
                    continue
                if prev and prev[1] and cover and prev[1] & cover:
                    expected += 60 * (k - prev[0])
                    bridged += k - prev[0] > 1
                prev = (k, cover)
            tracker = cw.ReadyTracker(10**9, mock.Mock())
            fake = PodListKubectl(states)
            with (
                mock.patch.object(cw.subprocess, "run", fake),
                mock.patch.object(cw, "restart_trigger", return_value=""),
            ):
                for k in range(n + 1):
                    tracker.poll(
                        cw.Instant(t0 + dt.timedelta(seconds=60 * k), 60.0 * k)
                    )
            tracker.finish(cw.Instant(t0 + dt.timedelta(seconds=60 * n), 60.0 * n))
            self.assertEqual(tracker.closed, expected, f"seed {seed}")
        self.assertGreater(bridged, 20)


class ErrorGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = os.path.join(self.tmp.name, "events.log")

    def one_line(self, fake, target, token):
        out, logged, _ = run_target(fake, target, log=self.log)
        self.assertEqual(len(has(out, token)), 1, out)
        return logged

    def test_exporter_poll_ok_zero_streak_wakes_once(self):
        a = [pod_item(POD_A, since=-3600)]
        fake = PodListKubectl([a], metrics=metrics_text(poll_ok=0))
        logged = self.one_line(fake, 360, "WATCH ERROR metrics")
        self.assertEqual(len(has(logged, "WATCH ERROR metrics")), 7)

    def test_unparsed_wait_line_streak_wakes_once(self):
        a = [pod_item(POD_A, since=-3600)]
        bad = "WARN ragfs::lock::manager manager.rs:988 no path or wait here"
        logged = self.one_line(PodListKubectl([a], logs=bad), 360, "WATCH ERROR logs")
        self.assertEqual(len(has(logged, "WATCH ERROR logs")), 7)

    def test_mixed_command_and_validation_failures_are_one_streak(self):
        a = [pod_item(POD_A, since=-3600)]
        fake = PodListKubectl([a], metrics=metrics_text(poll_ok=0), exec_fail={1, 3, 5})
        self.one_line(fake, 360, "WATCH ERROR metrics")

    def test_a_clean_poll_resets_the_streak(self):
        a = [pod_item(POD_A, since=-3600)]
        fake = PodListKubectl([a], exec_fail={0, 1, 3})
        out, _, _ = run_target(fake, 240, log=self.log)
        self.assertEqual(len(has(out, "WATCH ERROR metrics")), 2)


class Finalizer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = os.path.join(self.tmp.name, "events.log")

    def interrupted(self, exc, expect):
        a = [pod_item(POD_A, since=-3600)]
        caught = []
        out, logged, _ = run_target(
            PodListKubectl([a]),
            3600,
            log=self.log,
            raise_after=3,
            exc=exc,
            caught=caught,
        )
        self.assertEqual(caught, [exc])
        (seg,) = has(out, "SEGMENT ")
        self.assertIn("dur=0h02m00s", seg)
        self.assertTrue(seg.endswith(expect), seg)
        (end,) = has(out, "WATCH end:")
        self.assertIn(f"incomplete ({expect.split('(')[1]}", end)
        self.assertIn("1 segments, uptime 0h02m00s", end)
        self.assertEqual(len(has(logged, "WATCH end:")), 1)

    def test_interrupt_writes_the_ledger(self):
        self.interrupted(KeyboardInterrupt(), "incomplete (interrupted)")

    def test_error_writes_the_ledger_and_reraises(self):
        self.interrupted(RuntimeError("boom"), "incomplete (error: boom)")

    def test_main_exits_130_on_interrupt(self):
        with (
            mock.patch.object(cw, "watch", side_effect=KeyboardInterrupt),
            mock.patch.object(cw.signal, "signal"),
        ):
            self.assertEqual(cw.main(["--target-uptime", "1h"]), 130)

    def test_main_installs_the_sigterm_handler(self):
        with (
            mock.patch.object(cw, "watch"),
            mock.patch.object(cw.signal, "signal") as install,
        ):
            cw.main(["--target-uptime", "1h"])
        install.assert_called_once_with(cw.signal.SIGTERM, cw.on_sigterm)

    def test_hours_mode_does_not_install_it(self):
        with (
            mock.patch.object(cw, "watch"),
            mock.patch.object(cw.signal, "signal") as install,
        ):
            cw.main(["--hours", "1"])
        install.assert_not_called()

    def test_real_sigterm_exits_130_with_a_ledger(self):
        import signal as sig
        import time as real_time

        kubectl = os.path.join(self.tmp.name, "kubectl")
        pods = json.dumps({"items": [pod_item(POD_A, since=-3600)]})
        with open(kubectl, "w", encoding="utf-8") as fh:
            fh.write(
                "#!/bin/sh\n"
                'case "$*" in\n'
                f"  *'get pods'*) printf '%s' '{pods}' ;;\n"
                "  *exec*) printf '%s\\n' 'openviking_s3_ovlock_poll_ok 1' '# TYPE openviking_s3_ovlock_live_age_seconds gauge' ;;\n"
                "esac\n"
            )
        os.chmod(kubectl, 0o755)
        env = dict(os.environ, PATH=self.tmp.name + os.pathsep + os.environ["PATH"])
        proc = subprocess.Popen(
            [
                sys.executable,
                os.path.join(_DIR, "ov-contention-watch.py"),
                "--target-uptime",
                "1h",
                "--log",
                self.log,
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = real_time.time() + 15
            while real_time.time() < deadline:
                if os.path.exists(self.log):
                    with open(self.log, encoding="utf-8") as fh:
                        if "BEAT" in fh.read():
                            break
                real_time.sleep(0.05)
            else:
                self.fail("watcher never completed a poll")
            proc.send_signal(sig.SIGTERM)
            out, _ = proc.communicate(timeout=15)
        finally:
            proc.kill()
            proc.communicate()
        self.assertEqual(proc.returncode, 130)
        self.assertIn("SEGMENT 1 ", out)
        self.assertIn("WATCH end: incomplete (interrupted)", out)

    def test_sigterm_is_an_interrupt(self):
        with self.assertRaises(KeyboardInterrupt):
            cw.on_sigterm(15, None)


class Durations(unittest.TestCase):
    def test_parse(self):
        for text, secs in (
            ("72h", 259200),
            ("90m", 5400),
            ("1h30m", 5400),
            ("1.5h", 5400),
            ("45s", 45),
        ):
            self.assertEqual(cw.parse_duration(text), secs, text)

    def test_rejects_junk(self):
        for text in ("", "72", "h", "1d", "1h junk", "0h", "-1h", "30m1h", "1h1h"):
            with self.assertRaises(ValueError, msg=text):
                cw.parse_duration(text)


class Cli(unittest.TestCase):
    def test_main_parses_hours_and_log(self):
        with mock.patch.object(cw, "watch") as watch:
            cw.main(["--hours", "0.05", "--log", "/x/y.log", "--wake-wait-ms", "9"])
        hours, sink, wake = watch.call_args.args
        self.assertEqual((hours, sink.log_path, wake), (0.05, "/x/y.log", 9))

    def test_defaults(self):
        with mock.patch.object(cw, "watch") as watch:
            cw.main([])
        hours, sink, wake = watch.call_args.args
        self.assertEqual((hours, sink.log_path, wake), (6.0, None, 60000))

    def test_target_uptime_calls_watch_with_seconds(self):
        with (
            mock.patch.object(cw, "watch") as watch,
            mock.patch.object(cw.signal, "signal"),
        ):
            cw.main(["--target-uptime", "1h30m", "--log", "/x/y.log"])
        self.assertEqual(watch.call_args.kwargs, {"target_uptime": 5400.0})
        self.assertIsNone(watch.call_args.args[0])

    def test_target_uptime_and_hours_are_exclusive(self):
        with (
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            cw.main(["--hours", "6", "--target-uptime", "72h"])

    def test_bad_target_uptime_is_an_error(self):
        with (
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            cw.main(["--target-uptime", "soon"])

    def test_positional_hours_is_rejected(self):
        with (
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            cw.main(["6"])


if __name__ == "__main__":
    unittest.main()
