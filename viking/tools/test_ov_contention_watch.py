#!/usr/bin/env python3
"""Unit tests for ov-contention-watch.py (BUG-1220). Offline: subprocess.run and
the clock are stubbed; no cluster, no sockets."""

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
import subprocess
import tempfile
import unittest
from unittest import mock

_DIR = os.path.dirname(os.path.abspath(__file__))
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


def metrics_text(poll_ok=1, live_age=None):
    lines = [f"openviking_s3_ovlock_poll_ok {poll_ok}"]
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


class Clock:
    """time.time that advances only when sleep is called."""

    def __init__(self):
        self.t = 1_000_000.0

    def time(self):
        return self.t

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

    def test_positional_hours_is_rejected(self):
        with (
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            cw.main(["6"])


if __name__ == "__main__":
    unittest.main()
