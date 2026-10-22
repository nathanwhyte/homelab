#!/usr/bin/env python3
"""Unit tests for ov-lock-watchdog.py (BUG-1220). Offline: no sockets, no
Kubernetes API; the cluster and the exporter are stubbed."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_scripts" / "lib"))
from git_env import scrub_environ  # noqa: E402

scrub_environ()

import datetime
import importlib.util
import json
import os
import unittest

_DIR = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "ov_lock_watchdog", os.path.join(_DIR, "ov-lock-watchdog.py")
)
wd = importlib.util.module_from_spec(_spec)
sys.modules["ov_lock_watchdog"] = wd  # dataclasses resolve annotations here
_spec.loader.exec_module(wd)

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 10, 13, 12, 0, 0, tzinfo=UTC)
HOUR = 3600.0
KEY = "default/user/noot/sessions/mcp-store-e71a7b670fca/.path.ovlock"
PATH = "/local/" + KEY
LEASE = "5e9d7e9a-9f35-4b2d-af0f-ec494b7bd6c8"
OWNER_ID = "9959d01e-e133-4e46-8074-04cc1de18b00"
CFG = wd.Config()


def ago(seconds: float) -> datetime.datetime:
    return NOW - datetime.timedelta(seconds=seconds)


def lock(age: float, key: str = KEY, owner: str = "tok-1") -> wd.LiveLock:
    return wd.LiveLock(
        key=key, uri="viking://x", owner=owner, lock_type="E", age=float(age)
    )


def exporter(
    locks=(),
    work: int | None = 0,
    up: bool = True,
    poll_ok: bool = True,
    last_poll: float | None = None,
) -> wd.Exporter:
    return wd.Exporter(
        up=up,
        lock_poll_ok=poll_ok,
        last_poll=NOW.timestamp() - 5 if last_poll is None else last_poll,
        locks=tuple(locks),
        queue_work=work,
    )


def deploy(
    restarted_ago: float | None = 10 * HOUR, settled: bool = True, err: str = ""
) -> wd.DeployState:
    return wd.DeployState(
        resource_version="42",
        settled=settled,
        unsettled_why="" if settled else "rollout in progress",
        restarted_at=None if restarted_ago is None else ago(restarted_ago),
        restarted_at_error=err,
    )


def refresh_log(
    path: str = PATH,
    hold: float = 4 * HOUR,
    lease: str = LEASE,
    last_ago: float = 5.0,
    covered_for: float | None = None,
    step: float = 10.0,
    drop: tuple[float, float] | None = None,
    swap_at: float | None = None,
    other_lease: str = "00000000-aaaa-bbbb-cccc-111111111111",
) -> wd.LogRead:
    """A log with one refresh every `step` s on `path` for `hold` s.

    drop=(from_ago, to_ago) removes the lines in that span; swap_at=N makes
    lines older than N s ago belong to other_lease.
    """
    sightings = []
    t = last_ago
    while t <= hold:
        if not (drop and drop[1] <= t <= drop[0]):
            use = other_lease if swap_at is not None and t > swap_at else lease
            sightings.append((ago(t), use, OWNER_ID, (path,)))
        t += step
    covered = hold + 5 if covered_for is None else covered_for
    return wd.LogRead(
        now=NOW, covered_from=ago(covered), sightings=tuple(sorted(sightings))
    )


def decide(**kw) -> wd.Decision:
    log = kw.pop("log", refresh_log())
    return wd.decide(
        CFG,
        NOW,
        kw.pop("deploy", deploy()),
        kw.pop("exporter", exporter([lock(4 * HOUR)])),
        log,
        kw.pop("errors", None),
    )


class TestBug1220Policy(unittest.TestCase):
    """The incident: one lease held for hours while the queue stays busy."""

    def test_quiet_queue_restarts_at_the_hold_threshold(self):
        d = decide(
            exporter=exporter(
                [lock(3.2 * HOUR)], work=0
            ),  # the wedged task is not in the queue
            log=refresh_log(hold=3.2 * HOUR),
        )
        self.assertEqual(d.action, "restart", d.reason)
        self.assertIn("quiet", d.reason)
        self.assertEqual(d.qualified[0].verdict.lease_ref, LEASE)

    def test_busy_queue_defers_between_hold_and_force(self):
        d = decide(
            exporter=exporter([lock(4 * HOUR)], work=11),
            log=refresh_log(hold=4 * HOUR),
        )
        self.assertEqual(d.action, "defer", d.reason)
        self.assertIn("11 queue item(s) pending or in progress", d.reason)
        self.assertIn("forced restart in 7200s", d.reason)

    def test_busy_queue_is_forced_at_the_cap(self):
        d = decide(
            exporter=exporter([lock(6.1 * HOUR)], work=11),
            log=refresh_log(hold=6.1 * HOUR),
        )
        self.assertEqual(d.action, "restart", d.reason)
        self.assertIn("forced", d.reason)

    def test_unreadable_queue_counts_as_busy(self):
        e = exporter([lock(4 * HOUR)], work=None)
        self.assertEqual(decide(exporter=e).action, "defer")
        e = exporter([lock(7 * HOUR)], work=None)
        self.assertEqual(
            decide(exporter=e, log=refresh_log(hold=7 * HOUR)).action, "restart"
        )

    def test_below_the_hold_threshold_does_nothing(self):
        d = decide(exporter=exporter([lock(2.9 * HOUR)]))
        self.assertEqual(d.action, "none")
        self.assertIn("no lock held >=", d.reason)

    def test_quiet_max_work_is_configurable(self):
        cfg = wd.Config(quiet_max_work=3)
        for work, action in ((3, "restart"), (4, "defer")):
            d = wd.decide(
                cfg, NOW, deploy(), exporter([lock(4 * HOUR)], work=work), refresh_log()
            )
            self.assertEqual(d.action, action, d.reason)

    def test_any_queued_work_defers_by_default(self):
        d = decide(exporter=exporter([lock(4 * HOUR)], work=1))
        self.assertEqual(d.action, "defer", d.reason)


class TestIdentity(unittest.TestCase):
    def test_lease_ref_changing_on_the_path_is_not_a_wedge(self):
        d = decide(log=refresh_log(swap_at=2 * HOUR))
        self.assertEqual(d.action, "held")
        self.assertIn("lease changed", d.locks[0].reason)

    def test_refreshes_stopped_is_the_janitors_case(self):
        d = decide(log=refresh_log(last_ago=600.0))
        self.assertEqual(d.action, "held")
        self.assertIn("refreshes stopped", d.locks[0].reason)

    def test_a_refresh_gap_inside_the_window_blocks(self):
        d = decide(log=refresh_log(drop=(2 * HOUR + 600, 2 * HOUR)))
        self.assertEqual(d.action, "held")
        self.assertIn("refresh gap", d.locks[0].reason)

    def test_no_lease_line_for_the_path_blocks(self):
        d = decide(log=refresh_log(path="/local/default/other/.path.ovlock"))
        self.assertEqual(d.action, "held")
        self.assertIn("no lease line", d.locks[0].reason)

    def test_log_missing_blocks(self):
        d = decide(log=None, errors={"log": "HTTP 403 on GET .../log"})
        self.assertEqual(d.action, "held")
        self.assertIn("pod log unavailable: HTTP 403", d.locks[0].reason)

    def test_partial_log_coverage_is_judged_on_what_it_holds(self):
        # The log starts 2 h into a 4 h hold (rotation): still one lease, no gap.
        d = decide(log=refresh_log(covered_for=2 * HOUR))
        self.assertEqual(d.action, "restart", d.reason)
        self.assertIn("partial", d.locks[0].reason)

    def test_a_lease_swap_before_the_covered_part_is_not_visible(self):
        # Honest limit: that part rests on the exporter's holder-change reset.
        d = decide(log=refresh_log(covered_for=2 * HOUR, swap_at=3 * HOUR))
        self.assertEqual(d.action, "restart")

    def test_too_little_log_blocks(self):
        d = decide(log=refresh_log(covered_for=300.0))
        self.assertEqual(d.action, "held")
        self.assertIn("log covers only 300s", d.locks[0].reason)

    def test_unknown_exporter_owner_blocks(self):
        d = decide(exporter=exporter([lock(4 * HOUR, owner="unknown")]))
        self.assertEqual(d.action, "held")
        self.assertIn("owner unknown", d.locks[0].reason)

    def test_clock_skew_blocks(self):
        log = refresh_log(last_ago=-120.0)
        self.assertEqual(decide(log=log).action, "held")


class TestGates(unittest.TestCase):
    # A stop before the lock series are read is "blind"; a stop after a lock
    # held >= hold was seen is "held"; "none" is left for runs that read usable
    # lock series and saw no lock held >= hold (IMPR-1416 row 2).
    def test_exporter_down(self):
        d = decide(exporter=None, errors={"exporter": "URLError: refused"})
        self.assertEqual(d.action, "blind")
        self.assertIn("exporter unusable: URLError", d.reason)

    def test_exporter_reports_ov_down(self):
        for kw in ({"up": False}, {"poll_ok": False}):
            d = decide(exporter=exporter([lock(9 * HOUR)], **kw))
            self.assertEqual(d.action, "blind")
            self.assertIn("stale", d.reason)

    def test_exporter_stale_poll(self):
        e = exporter([lock(9 * HOUR)], last_poll=NOW.timestamp() - 900)
        d = decide(exporter=e)
        self.assertEqual(d.action, "blind")
        self.assertIn("last polled 900s ago", d.reason)

    def test_deployment_unreadable(self):
        d = decide(deploy=None, errors={"deploy": "HTTP 403"})
        self.assertEqual(d.action, "blind")
        self.assertIn("deployment unreadable: HTTP 403", d.reason)

    def test_no_long_lock_is_an_observed_none(self):
        d = decide(exporter=exporter([lock(600.0)]))
        self.assertEqual(d.action, "none")
        self.assertIn("no lock held >=", d.reason)

    def test_cooldown_after_a_recent_restart(self):
        d = decide(deploy=deploy(restarted_ago=1800.0))
        self.assertEqual(d.action, "held")
        self.assertIn("cooldown", d.reason)

    def test_cooldown_expires(self):
        self.assertEqual(decide(deploy=deploy(restarted_ago=3700.0)).action, "restart")

    def test_never_restarted_deployment(self):
        self.assertEqual(decide(deploy=deploy(restarted_ago=None)).action, "restart")

    def test_unparseable_restarted_at_blocks(self):
        d = decide(deploy=deploy(restarted_ago=None, err="unparseable restartedAt"))
        self.assertEqual(d.action, "held")
        self.assertIn("cooldown", d.reason)

    def test_unsettled_rollout_blocks(self):
        d = decide(deploy=deploy(settled=False))
        self.assertEqual(d.action, "held")
        self.assertIn("not settled", d.reason)


class TestTwoLongLocks(unittest.TestCase):
    def test_one_decision_covers_both_and_names_both(self):
        key2 = "default/user/noot/sessions/other/.path.ovlock"
        log = refresh_log()
        second = refresh_log(
            path="/local/" + key2, lease="22222222-3333-4444-5555-666666666666"
        )
        both = wd.LogRead(
            now=NOW,
            covered_from=log.covered_from,
            sightings=tuple(sorted(log.sightings + second.sightings)),
        )
        d = decide(
            exporter=exporter([lock(4 * HOUR), lock(4 * HOUR, key=key2)], work=0),
            log=both,
        )
        self.assertEqual(d.action, "restart", d.reason)
        self.assertEqual(len(d.qualified), 2)
        self.assertIn("quiet", d.reason)

    def test_one_rejected_lock_does_not_veto_the_other(self):
        key2 = "default/user/noot/sessions/other/.path.ovlock"
        d = decide(
            exporter=exporter([lock(4 * HOUR), lock(5 * HOUR, key=key2)], work=0),
        )  # the log only has KEY
        self.assertEqual(d.action, "restart", d.reason)
        self.assertEqual([r.qualified for r in d.locks], [False, True])


class FakeCluster:
    def __init__(self, deploy_state=None, log=None, pod=("ov-1", "uid-1")):
        self.deploy_state = deploy_state or deploy()
        self.log = log or refresh_log()
        self.pod = pod
        self.patches = []
        self.restart_error = None

    def get_deployment(self):
        return self.deploy_state

    def ov_pod(self):
        return self.pod

    def ov_log(self, pod, since, now):
        return self.log

    def restart(self, resource_version, note, now):
        if self.restart_error:
            raise self.restart_error
        self.patches.append((resource_version, note))
        return "patched"


def metrics_text(age=4 * HOUR, work=11, up=1, poll_ok=1) -> str:
    return (
        "# TYPE openviking_up gauge\n"
        f"openviking_up {up}\n"
        f"openviking_last_poll_unix {NOW.timestamp() - 3}\n"
        f"openviking_s3_ovlock_poll_ok {poll_ok}\n"
        "# TYPE openviking_s3_ovlock_live_age_seconds gauge\n"
        f'openviking_s3_ovlock_live_age_seconds{{key="{KEY}",uri="viking://u/x",'
        f'owner="tok-1",lock_type="E"}} {age}\n'
        '# TYPE openviking_queue_pending gauge\nopenviking_queue_pending{queue="total"} '
        f"{work - (1 if work else 0)}\n"
        f'openviking_queue_pending{{queue="semantic"}} 40\n'
        'openviking_queue_in_progress{queue="total"} ' + ("1" if work else "0") + "\n"
    )


class TestRun(unittest.TestCase):
    def go(self, cluster, text, dry_run=False, cfg=CFG):
        out: list[str] = []
        code = wd.run(
            cfg,
            cluster,
            lambda url: text if isinstance(text, str) else text(url),
            dry_run=dry_run,
            now_fn=lambda: NOW,
            say=out.append,
        )
        return code, "\n".join(out)

    def test_quiet_restart_patches_once_with_the_read_resource_version(self):
        cluster = FakeCluster(log=refresh_log(hold=4 * HOUR))
        code, out = self.go(cluster, metrics_text(work=0))
        self.assertEqual(code, 0, out)
        self.assertEqual(len(cluster.patches), 1)
        self.assertEqual(cluster.patches[0][0], "42")
        self.assertIn(f"lease_ref={LEASE}", out)
        self.assertIn(f"owner_id={OWNER_ID}", out)
        self.assertIn("ov-lock-watchdog: RESTARTED deploy/openviking", out)
        self.assertIn(PATH, out)

    def test_dry_run_never_patches(self):
        cluster = FakeCluster()
        code, out = self.go(cluster, metrics_text(work=0), dry_run=True)
        self.assertEqual(code, 0)
        self.assertEqual(cluster.patches, [])
        self.assertIn("DRY-RUN would restart", out)
        self.assertIn("mode=dry-run", out)

    def test_long_lock_in_cooldown_logs_held_without_patching(self):
        cluster = FakeCluster(deploy_state=deploy(restarted_ago=1800.0))
        code, out = self.go(cluster, metrics_text(work=0))
        self.assertEqual((code, cluster.patches), (0, []))
        self.assertIn("decision=held", out)
        self.assertIn("cooldown", out)

    def test_busy_queue_defers_without_patching(self):
        cluster = FakeCluster()
        code, out = self.go(cluster, metrics_text(work=11))
        self.assertEqual((code, cluster.patches), (0, []))
        self.assertIn("decision=defer", out)

    def test_exporter_unreachable_is_a_quiet_no_restart(self):
        def boom(url):
            raise OSError("connection refused")

        cluster = FakeCluster()
        code, out = self.go(cluster, boom)
        self.assertEqual((code, cluster.patches), (0, []))
        self.assertIn("decision=blind", out)
        self.assertIn("exporter unusable: OSError", out)

    def test_exporter_without_the_labelled_series_is_unusable(self):
        cluster = FakeCluster()
        text = "openviking_up 1\nopenviking_last_poll_unix 1\nopenviking_s3_ovlock_poll_ok 1\n"
        code, out = self.go(cluster, text)
        self.assertEqual((code, cluster.patches), (0, []))
        self.assertIn("predates homelab#205", out)

    def test_missing_rbac_is_blind_and_exits_2(self):
        class Blind(FakeCluster):
            def get_deployment(self):
                raise wd.K8sError("HTTP 403 on GET deployments", blind=True)

        cluster = Blind()
        code, out = self.go(cluster, metrics_text(work=0))
        self.assertEqual((code, cluster.patches), (2, []))
        self.assertIn("BLIND", out)

    def test_log_forbidden_is_blind_and_never_restarts(self):
        class NoLog(FakeCluster):
            def ov_log(self, pod, since, now):
                raise wd.K8sError("HTTP 403 on GET pods/log", blind=True)

        cluster = NoLog()
        code, out = self.go(cluster, metrics_text(work=0))
        self.assertEqual((code, cluster.patches), (2, []))
        self.assertIn("pod log unavailable", out)

    def test_pod_replaced_during_the_log_read_blocks(self):
        class Swap(FakeCluster):
            calls = 0

            def ov_pod(self):
                Swap.calls += 1
                return ("ov-1", "a") if Swap.calls == 1 else ("ov-2", "b")

        cluster = Swap()
        code, out = self.go(cluster, metrics_text(work=0))
        self.assertEqual((code, cluster.patches), (0, []))
        self.assertIn("pod changed", out)

    def test_cooldown_blocks_before_the_log_is_read(self):
        class NoLogRead(FakeCluster):
            def ov_log(self, *a):
                raise AssertionError("log must not be read")

        cluster = NoLogRead(deploy_state=deploy(restarted_ago=600.0))
        code, out = self.go(cluster, metrics_text(work=0))
        self.assertEqual((code, cluster.patches), (0, []))
        self.assertIn("cooldown", out)

    def test_conflict_on_patch_restarts_nothing_and_is_not_a_failure(self):
        cluster = FakeCluster()
        cluster.restart_error = wd.K8sError("HTTP 409", conflict=True)
        code, out = self.go(cluster, metrics_text(work=0))
        self.assertEqual((code, cluster.patches), (0, []))
        self.assertIn("lost a race", out)

    def test_failed_patch_exits_1(self):
        cluster = FakeCluster()
        cluster.restart_error = wd.K8sError("HTTP 500")
        code, out = self.go(cluster, metrics_text(work=0))
        self.assertEqual(code, 1)
        self.assertIn("FAILED", out)

    def test_state_change_between_read_and_patch_aborts(self):
        class Moving(FakeCluster):
            reads = 0

            def get_deployment(self):
                Moving.reads += 1
                return deploy() if Moving.reads == 1 else deploy(restarted_ago=5.0)

        cluster = Moving()
        code, out = self.go(cluster, metrics_text(work=0))
        self.assertEqual((code, cluster.patches), (0, []))
        self.assertIn("ABORTED before restart", out)

    def test_lock_gone_before_the_patch_aborts(self):
        texts = iter([metrics_text(work=0), metrics_text(age=60)])
        cluster = FakeCluster()
        code, out = self.go(cluster, lambda url: next(texts))
        self.assertEqual((code, cluster.patches), (0, []))
        self.assertIn("ABORTED before restart", out)

    def test_exporter_failing_on_the_recheck_aborts(self):
        calls = []

        def flaky(url):
            calls.append(url)
            if len(calls) > 1:
                raise OSError("reset")
            return metrics_text(work=0)

        cluster = FakeCluster()
        code, out = self.go(cluster, flaky)
        self.assertEqual((code, cluster.patches), (0, []))
        self.assertIn("ABORTED before restart: OSError", out)

    def test_owner_change_before_the_patch_aborts(self):
        swapped = metrics_text(work=0).replace('owner="tok-1"', 'owner="tok-9"')
        texts = iter([metrics_text(work=0), swapped])
        cluster = FakeCluster()
        code, out = self.go(cluster, lambda url: next(texts))
        self.assertEqual((code, cluster.patches), (0, []))
        self.assertIn("ABORTED before restart", out)

    def test_queue_busy_on_the_recheck_aborts(self):
        # homelab#251 Codex R1: the recheck must apply the quiet-queue rule to
        # the fresh queue count, not only the lock gates.
        texts = iter([metrics_text(work=0), metrics_text(work=11)])
        cluster = FakeCluster(log=refresh_log(hold=4 * HOUR))
        code, out = self.go(cluster, lambda url: next(texts))
        self.assertEqual((code, cluster.patches), (0, []))
        self.assertIn("ABORTED before restart: busy:", out)

    def test_queue_unreadable_on_the_recheck_aborts(self):
        unreadable = "".join(
            line
            for line in metrics_text(work=0).splitlines(keepends=True)
            if "queue" not in line
        )
        texts = iter([metrics_text(work=0), unreadable])
        cluster = FakeCluster(log=refresh_log(hold=4 * HOUR))
        code, out = self.go(cluster, lambda url: next(texts))
        self.assertEqual((code, cluster.patches), (0, []))
        self.assertIn("queue unreadable", out)

    def test_busy_recheck_past_the_force_cap_still_restarts(self):
        texts = iter([metrics_text(age=7 * HOUR, work=0), metrics_text(age=7 * HOUR)])
        cluster = FakeCluster(log=refresh_log(hold=7 * HOUR))
        code, out = self.go(cluster, lambda url: next(texts))
        self.assertEqual(code, 0, out)
        self.assertEqual(len(cluster.patches), 1)
        self.assertIn("forced:", out)

    def test_two_long_locks_still_patch_once(self):
        key2 = "default/user/noot/sessions/other/.path.ovlock"
        second = refresh_log(
            path="/local/" + key2, lease="22222222-3333-4444-5555-666666666666"
        )
        base = refresh_log()
        log = wd.LogRead(
            now=NOW,
            covered_from=base.covered_from,
            sightings=tuple(sorted(base.sightings + second.sightings)),
        )
        text = metrics_text(work=0) + (
            f'openviking_s3_ovlock_live_age_seconds{{key="{key2}",uri="viking://u/y",'
            f'owner="tok-2",lock_type="E"}} {4 * HOUR}\n'
        )
        cluster = FakeCluster(log=log)
        code, out = self.go(cluster, text)
        self.assertEqual(code, 0, out)
        self.assertEqual(len(cluster.patches), 1)
        self.assertEqual(out.count("identity=same-lease"), 2)


class TestConfigMapInSync(unittest.TestCase):
    def test_configmap_carries_the_current_script(self):
        # The pod runs the ConfigMap copy; an unregenerated edit ships nothing.
        path = os.path.join(_DIR, "..", "manifests", "ov-lock-watchdog-configmap.yaml")
        with open(path) as fh:
            lines = fh.read().splitlines()
        start = lines.index("  ov-lock-watchdog.py: |") + 1
        body = []
        for line in lines[start:]:
            if line and not line.startswith("    "):
                break
            body.append(line[4:])
        with open(os.path.join(_DIR, "ov-lock-watchdog.py")) as fh:
            source = fh.read().splitlines()
        self.assertEqual(
            [b.rstrip() for b in body],
            [x.rstrip() for x in source],
            "run viking/scripts/regen-lock-watchdog-configmap.sh",
        )


class TestParsers(unittest.TestCase):
    REFRESH = (
        "2026-10-13T14:38:40.214593Z  INFO ragfs::lock::manager: crates/ragfs/src/"
        "lock/manager.rs:538: pathlock lease automatically refreshed "
        f"lease_ref={LEASE} owner_id={OWNER_ID} lock_path={PATH}"
    )

    def test_refresh_line_with_kubelet_timestamp_prefix(self):
        text = f"2026-10-13T14:38:40.214600123Z {self.REFRESH}\n"
        read = wd.parse_pod_log(text, NOW)
        ts, lease, owner, paths = read.sightings[0]
        self.assertEqual((lease, owner, paths), (LEASE, OWNER_ID, (PATH,)))
        self.assertEqual(ts, datetime.datetime(2026, 10, 13, 14, 38, 40, 214593, UTC))
        self.assertEqual(read.covered_from.second, 40)

    def test_kubelet_timestamp_offset_is_applied(self):
        # Seen live: the API returns the node's local offset, not Z.
        text = f"2026-10-13T09:38:40.214600123-05:00 {self.REFRESH}\n"
        read = wd.parse_pod_log(text, NOW)
        self.assertEqual(
            read.covered_from,
            datetime.datetime(2026, 10, 13, 14, 38, 40, 214600, UTC),
        )
        text = f"2026-10-13T16:38:40+02:00 {self.REFRESH}\n"
        self.assertEqual(wd.parse_pod_log(text, NOW).covered_from.hour, 14)

    def test_drifted_lock_line_fails_the_read(self):
        bad = self.REFRESH.replace("lock_path=", "path=")
        with self.assertRaises(ValueError):
            wd.parse_pod_log(f"2026-10-13T14:38:40Z {bad}\n", NOW)

    def test_other_lines_are_ignored(self):
        text = "2026-10-13T11:39:59Z 2026-10-13 11:39:59,596 - uvicorn.access - INFO\n"
        read = wd.parse_pod_log(text, NOW)
        self.assertEqual(read.sightings, ())

    def test_exporter_text_round_trip(self):
        e = wd.parse_exporter(metrics_text(age=12345, work=11))
        self.assertTrue(e.up and e.lock_poll_ok)
        self.assertEqual(
            (e.locks[0].key, e.locks[0].owner, e.locks[0].age), (KEY, "tok-1", 12345)
        )
        self.assertEqual(e.queue_work, 11)

    def test_empty_labelled_series_is_fine_but_missing_header_is_not(self):
        text = metrics_text().replace(
            "# TYPE openviking_s3_ovlock_live_age_seconds gauge\n", ""
        )
        with self.assertRaises(wd.ParseError):
            wd.parse_exporter(text)

    def test_escaped_label_values(self):
        text = metrics_text().replace('uri="viking://u/x"', r'uri="viking://u/a\"b}"')
        self.assertEqual(wd.parse_exporter(text).locks[0].uri, 'viking://u/a"b}')

    def test_nan_on_a_needed_series_is_an_error(self):
        with self.assertRaises(wd.ParseError):
            wd.parse_exporter(
                metrics_text().replace("openviking_up 1", "openviking_up NaN")
            )

    def test_queue_total_missing_is_unknown_not_zero(self):
        text = "\n".join(
            line for line in metrics_text().splitlines() if 'queue="total"' not in line
        )
        self.assertIsNone(wd.parse_exporter(text).queue_work)

    def test_deployment_document(self):
        doc = {
            "metadata": {"resourceVersion": "9", "generation": 3},
            "spec": {
                "replicas": 1,
                "template": {
                    "metadata": {
                        "annotations": {wd.RESTARTED_AT: "2026-10-08T07:56:17-05:00"}
                    }
                },
            },
            "status": {
                "observedGeneration": 3,
                "replicas": 1,
                "updatedReplicas": 1,
                "availableReplicas": 1,
            },
        }
        state = wd.parse_deployment(doc)
        self.assertTrue(state.settled)
        self.assertEqual(state.restarted_at.utcoffset(), datetime.timedelta(hours=-5))
        doc["status"]["availableReplicas"] = 0
        self.assertFalse(wd.parse_deployment(doc).settled)

    def test_config_rejects_force_below_hold(self):
        with self.assertRaises(ValueError):
            wd.Config.from_env(
                {"WATCHDOG_HOLD_SECONDS": "100", "WATCHDOG_FORCE_SECONDS": "50"}
            )

    def test_config_defaults_match_the_spec(self):
        cfg = wd.Config.from_env({})
        self.assertEqual(
            (cfg.hold, cfg.force, cfg.cooldown), (10800.0, 21600.0, 3600.0)
        )

    def test_restart_patch_body_carries_the_precondition(self):
        sent = {}

        class Capture(wd.Cluster):
            def _call(self, method, path, body=None, content_type="", **kw):
                sent.update(method=method, path=path, body=body, ct=content_type)
                return b"{}"

        Capture("viking", "openviking").restart("77", "note", NOW)
        body = json.loads(sent["body"])
        self.assertEqual(sent["method"], "PATCH")
        self.assertTrue(
            sent["path"].endswith("/namespaces/viking/deployments/openviking")
        )
        # RBAC cannot limit a Deployment PATCH to annotations; this body does.
        self.assertEqual(set(body), {"metadata", "spec"})
        self.assertEqual(set(body["metadata"]), {"resourceVersion", "annotations"})
        self.assertEqual(set(body["spec"]), {"template"})
        self.assertEqual(set(body["spec"]["template"]), {"metadata"})
        self.assertEqual(set(body["spec"]["template"]["metadata"]), {"annotations"})
        self.assertEqual(body["metadata"]["resourceVersion"], "77")
        self.assertEqual(
            body["spec"]["template"]["metadata"]["annotations"][wd.RESTARTED_AT],
            "2026-10-13T12:00:00Z",
        )


if __name__ == "__main__":
    unittest.main()
