#!/usr/bin/env python3
"""Unit tests for ov-contention-watch.py (BUG-1220). Offline: subprocess.run and
the clock are stubbed; no cluster, no sockets."""

from __future__ import annotations

import contextlib
import datetime as dt
import importlib.util
import io
import json
import os
import subprocess
import sys
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
    state repeats. The Deployment (readyReplicas included) is never consulted.
    """

    def __init__(self, states, exec_fail=(), logs_fail=(), **kw):
        super().__init__([POD_A], **kw)
        self.states = list(states)
        self.exec_fail = set(exec_fail)  # indices of exec calls that fail
        self.logs_fail = set(logs_fail)  # indices of logs calls that fail
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
                return subprocess.CompletedProcess(args, 1, "", "connection refused")
            return subprocess.CompletedProcess(
                args, 0, json.dumps({"items": state}), ""
            )
        return super().__call__(args, **kw)


class _Capped(Exception):
    pass


def run_target(fake, target, log=None, max_polls=500, capped_ok=False):
    """Run watch() in target-uptime mode; return (stdout lines, log lines, polls).

    A run that has not stopped after max_polls fails unless capped_ok is set.
    """
    clock = Clock()
    polls = []

    def sleep(s):
        polls.append(clock.t)
        if len(polls) > max_polls:
            raise _Capped()
        clock.sleep(s)

    buf = io.StringIO()
    with (
        mock.patch.object(cw.subprocess, "run", fake),
        mock.patch.object(cw.time, "time", clock.time),
        mock.patch.object(cw.time, "sleep", sleep),
        contextlib.redirect_stdout(buf),
    ):
        try:
            cw.watch(None, cw.Sink(log), target_uptime=target)
        except _Capped:
            if not capped_ok:
                raise AssertionError("watcher did not stop at the target") from None
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
        self.assertIn("rev=17", segs[0])  # restart trigger rides on the line
        # B: 290 s .. last Ready poll at +660 s = 370 s; 240 + 370 = 610 s
        self.assertIn("SEGMENT 2 pod=openviking-bbb-2", segs[1])
        self.assertIn("dur=0h06m10s cum=0h10m10s remaining=0h00m00s", segs[1])
        self.assertEqual(has(out, "OV DOWN"), [])
        self.assertEqual(has(out, "OV BACK"), [])
        self.assertIn(
            "2 segments, uptime 0h10m10s of 0h10m00s", has(out, "WATCH end:")[0]
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
        # Ready for 3 polls, then NotReady, 4 failed probes, a stale Ready, then B
        states = [a] * 3 + [gone] + [None] * 4 + [a, [pod_item(POD_B, since=600)]]
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

    def test_stale_ready_pod_or_replicas_is_not_back(self):
        a = [pod_item(POD_A, since=-3600)]
        down = [pod_item(POD_A, ready=False, since=130)]
        # the old pod reports Ready again with a transition from before the stop, and
        # the Deployment still says readyReplicas=1: neither is a recovery
        stale = {"status": {"readyReplicas": 1}}
        fake = PodListKubectl([a, a, down, a, a, a], deploy=json.dumps(stale))
        out, _, _ = run_target(fake, 180, log=self.log, max_polls=12, capped_ok=True)
        # never reaches the target again: the watcher is still down at the cap
        self.assertEqual(len(has(out, "OV DOWN")), 1)
        self.assertEqual(has(out, "OV BACK"), [])

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
        segs = has(out, "SEGMENT ")
        self.assertEqual(len(segs), 2)
        # A is kept while Ready (+0..+300 s); B takes over from there to +360 s
        self.assertIn("pod=openviking-aaa-1", segs[0])
        self.assertIn("dur=0h05m00s", segs[0])
        self.assertIn("pod=openviking-bbb-2", segs[1])
        self.assertIn("dur=0h01m00s cum=0h06m00s", segs[1])
        self.assertEqual(len(has(out, "OV DOWN")), 1)

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

    def test_hours_mode_still_ends_by_the_clock(self):
        out, _ = run_watch(FakeKubectl([POD_A]), polls=2)
        self.assertEqual(has(out, "SEGMENT"), [])
        self.assertNotIn("segments", has(out, "WATCH end:")[0])


class UptimeSum(unittest.TestCase):
    def test_last_soak_segment_c_replay(self):
        start = dt.datetime(2026, 10, 21, 22, 7, 19, tzinfo=dt.UTC)
        end = dt.datetime(2026, 10, 22, 0, 26, 51, tzinfo=dt.UTC)
        lines = []

        class Lines:
            def emit(self, line, wake=True):
                lines.append(line)

        t = cw.ReadyTracker(72 * 3600, Lines(), start)
        t.open(POD_A, start, start)
        t.last_ready = end
        t.close()
        self.assertEqual(t.closed, 2 * 3600 + 19 * 60 + 32)
        self.assertEqual(cw.fmt_dur(t.closed), "2h19m32s")
        self.assertIn("start=2026-10-21T22:07:19Z end=2026-10-22T00:26:51Z", lines[0])
        self.assertIn("dur=2h19m32s cum=2h19m32s remaining=69h40m28s", lines[0])

    def test_segment_never_starts_before_the_previous_one_ended(self):
        t0 = dt.datetime(2026, 10, 21, 0, 0, tzinfo=dt.UTC)
        t = cw.ReadyTracker(3600, mock.Mock(), t0)
        t.open(POD_A, t0, t0)
        t.last_ready = t0 + dt.timedelta(minutes=10)
        t.close()
        # a rolling update: the new pod went Ready 5 minutes before the old one's end
        t.open(POD_B, t0 + dt.timedelta(minutes=5), t0 + dt.timedelta(minutes=11))
        self.assertEqual(t.start, t0 + dt.timedelta(minutes=10))


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
        for text in ("", "72", "h", "1d", "1h junk", "0h", "-1h"):
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
        with mock.patch.object(cw, "watch") as watch:
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
