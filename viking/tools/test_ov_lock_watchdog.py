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
sys.path.insert(0, _DIR)
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
KEY2 = "default/user/noot/sessions/other/.path.ovlock"
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


def decide(**kw) -> wd.Decision:
    return wd.decide(
        CFG,
        NOW,
        kw.pop("deploy", deploy()),
        kw.pop("exporter", exporter([lock(4 * HOUR)])),
        kw.pop("errors", None),
    )


class TestBug1220Policy(unittest.TestCase):
    """The incident: one lease held for hours while the queue stays busy."""

    def test_quiet_queue_restarts_at_the_hold_threshold(self):
        d = decide(
            exporter=exporter(
                [lock(3.2 * HOUR)], work=0
            ),  # the wedged task is not in the queue
        )
        self.assertEqual(d.action, "restart", d.reason)
        self.assertIn("quiet", d.reason)
        self.assertEqual(d.qualified[0].lock.owner, "tok-1")

    def test_busy_queue_defers_between_hold_and_force(self):
        d = decide(exporter=exporter([lock(4 * HOUR)], work=11))
        self.assertEqual(d.action, "defer", d.reason)
        self.assertIn("11 queue item(s) pending or in progress", d.reason)
        self.assertIn("forced restart in 7200s", d.reason)

    def test_busy_queue_is_forced_at_the_cap(self):
        d = decide(exporter=exporter([lock(6.1 * HOUR)], work=11))
        self.assertEqual(d.action, "restart", d.reason)
        self.assertIn("forced", d.reason)

    def test_unreadable_queue_counts_as_busy(self):
        e = exporter([lock(4 * HOUR)], work=None)
        self.assertEqual(decide(exporter=e).action, "defer")
        e = exporter([lock(7 * HOUR)], work=None)
        self.assertEqual(decide(exporter=e).action, "restart")

    def test_below_the_hold_threshold_does_nothing(self):
        d = decide(exporter=exporter([lock(2.9 * HOUR)]))
        self.assertEqual(d.action, "none")
        self.assertIn("no lock held >=", d.reason)

    def test_quiet_max_work_is_configurable(self):
        cfg = wd.Config(quiet_max_work=3)
        for work, action in ((3, "restart"), (4, "defer")):
            d = wd.decide(cfg, NOW, deploy(), exporter([lock(4 * HOUR)], work=work))
            self.assertEqual(d.action, action, d.reason)

    def test_any_queued_work_defers_by_default(self):
        d = decide(exporter=exporter([lock(4 * HOUR)], work=1))
        self.assertEqual(d.action, "defer", d.reason)


class TestIdentity(unittest.TestCase):
    def test_a_readable_owner_qualifies_on_exporter_evidence_alone(self):
        # BUG-1282: v0.5.1.1 logs no lease line, so none may be required.
        d = decide()
        self.assertEqual(d.action, "restart", d.reason)
        self.assertTrue(d.locks[0].qualified)
        self.assertIn("one owner", d.locks[0].reason)

    def test_unreadable_owner_blocks(self):
        for owner in ("unknown", ""):
            d = decide(exporter=exporter([lock(4 * HOUR, owner=owner)]))
            self.assertEqual(d.action, "held", owner)
            self.assertIn("owner unknown", d.locks[0].reason)
            self.assertIn("none with a readable owner", d.reason)

    def test_unread_lock_type_does_not_block_a_readable_owner(self):
        e = exporter(
            [wd.LiveLock(KEY, "viking://x", "tok-1", "unknown", 4 * HOUR)], work=0
        )
        self.assertEqual(decide(exporter=e).action, "restart")


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
        d = decide(
            exporter=exporter([lock(4 * HOUR), lock(4 * HOUR, key=KEY2)], work=0),
        )
        self.assertEqual(d.action, "restart", d.reason)
        self.assertEqual(len(d.qualified), 2)
        self.assertIn("quiet", d.reason)

    def test_one_rejected_lock_does_not_veto_the_other(self):
        d = decide(
            exporter=exporter(
                [lock(5 * HOUR, owner="unknown"), lock(4 * HOUR, key=KEY2)], work=0
            ),
        )
        self.assertEqual(d.action, "restart", d.reason)
        self.assertEqual([r.qualified for r in d.locks], [False, True])


class FakeCluster:
    """No pod calls: the job needs only the Deployment since BUG-1282."""

    def __init__(self, deploy_state=None):
        self.deploy_state = deploy_state or deploy()
        self.patches = []
        self.restart_error = None

    def get_deployment(self):
        return self.deploy_state

    def restart(self, resource_version, note, now):
        if self.restart_error:
            raise self.restart_error
        self.patches.append((resource_version, note))
        return "patched"


POLL_LINE = f"openviking_last_poll_unix {NOW.timestamp() - 3}\n"


def metrics_text(age=4 * HOUR, work=11, up=1, poll_ok=1) -> str:
    return (
        "# TYPE openviking_up gauge\n"
        f"openviking_up {up}\n"
        + POLL_LINE
        + f"openviking_s3_ovlock_poll_ok {poll_ok}\n"
        "# TYPE openviking_s3_ovlock_live_age_seconds gauge\n"
        f'openviking_s3_ovlock_live_age_seconds{{key="{KEY}",uri="viking://u/x",'
        f'owner="tok-1",lock_type="E"}} {age}\n'
        '# TYPE openviking_queue_pending gauge\nopenviking_queue_pending{queue="total"} '
        f"{work - (1 if work else 0)}\n"
        f'openviking_queue_pending{{queue="semantic"}} 40\n'
        'openviking_queue_in_progress{queue="total"} ' + ("1" if work else "0") + "\n"
    )


def locks_text(
    generation=1, mtime=1000.0, owner="tok-1", lock_type="E", read_ok=True, ok=True
) -> str:
    return json.dumps(
        {
            "ok": ok,
            "generation": generation,
            "polled_at": 2000.0,
            "live_window_seconds": 120.0,
            "locks": [
                {
                    "key": KEY,
                    "uri": "viking://u/x",
                    "first_seen": 1.0,
                    "last_modified": mtime,
                    "refresh_age_seconds": 5.0,
                    "owner": owner,
                    "lock_type": lock_type,
                    "owner_read_ok": read_ok,
                }
            ],
        }
    )


class TestRun(unittest.TestCase):
    def go(self, cluster, text, dry_run=False, cfg=CFG, locks=None):
        """`locks` are the GET /locks replies in order; an exception is raised.
        The default shows a later poll and a refreshed lock."""
        out: list[str] = []
        self.events: list[str] = []
        replies = iter(locks or [locks_text(), locks_text(generation=2, mtime=1030.0)])

        def fetch(url):
            self.events.append(f"fetch {url.rsplit('/', 1)[-1]}")
            if url != cfg.locks_url:
                return text if isinstance(text, str) else text(url)
            reply = next(replies)
            if isinstance(reply, Exception):
                raise reply
            return reply

        code = wd.run(
            cfg,
            cluster,
            fetch,
            dry_run=dry_run,
            now_fn=lambda: NOW,
            say=out.append,
            sleep=lambda s: self.events.append(f"sleep {s}"),
        )
        return code, "\n".join(out)

    def test_quiet_restart_patches_once_with_the_read_resource_version(self):
        cluster = FakeCluster()
        code, out = self.go(cluster, metrics_text(work=0))
        self.assertEqual(code, 0, out)
        self.assertEqual(len(cluster.patches), 1)
        self.assertEqual(cluster.patches[0][0], "42")
        self.assertIn("owner=tok-1", out)
        self.assertIn("ov-lock-watchdog: RESTARTED deploy/openviking", out)
        self.assertIn(KEY, out)

    def test_restart_notes_name_no_lease(self):
        cluster = FakeCluster()
        code, out = self.go(cluster, metrics_text(work=0))
        self.assertEqual(code, 0, out)
        self.assertNotIn("lease_ref", out)
        self.assertNotIn("lease_ref", cluster.patches[0][1])
        self.assertIn("identity=same-owner", out)

    def test_the_two_lock_reads_straddle_the_wait(self):
        cluster = FakeCluster()
        self.go(cluster, metrics_text(work=0))
        self.assertEqual(
            self.events,
            [
                "fetch metrics",
                "fetch locks",
                "sleep 30.0",
                "fetch metrics",
                "fetch locks",
            ],
        )

    def assert_aborts(self, locks, reason):
        cluster = FakeCluster()
        code, out = self.go(cluster, metrics_text(work=0), locks=locks)
        self.assertEqual((code, cluster.patches), (0, []))
        self.assertIn(f"ABORTED before restart: {reason}", out)

    def test_two_reads_of_one_poll_abort(self):
        # The exporter compares holders from the poll after labelling, so a
        # re-read of the same poll proves nothing (BUG-1282). Codex R1: the mark
        # comes with the lock data, never from a separately published series.
        self.assert_aborts(
            [locks_text(), locks_text(mtime=1030.0)],
            "the exporter has not polled again",
        )

    def test_a_cached_owner_on_either_poll_aborts(self):
        # Codex R1: a failed GET keeps the old label, so the holder is unproven.
        later = {"generation": 2, "mtime": 1030.0}
        for locks in (
            [locks_text(read_ok=False), locks_text(**later)],
            [locks_text(), locks_text(read_ok=False, **later)],
        ):
            with self.subTest():
                self.assert_aborts(locks, "an owner read failed")

    def test_a_lock_that_stopped_refreshing_aborts(self):
        # Codex R1: a frozen lock stays in the series for the live window.
        self.assert_aborts(
            [locks_text(), locks_text(generation=2)],
            "the lock was not refreshed",
        )

    def test_owner_or_type_change_in_the_lock_reads_aborts(self):
        later = {"generation": 2, "mtime": 1030.0}
        for changed in ({"owner": "tok-9"}, {"lock_type": "T"}):
            with self.subTest(changed):
                self.assert_aborts(
                    [locks_text(), locks_text(**later, **changed)],
                    "lock gone or changed",
                )
                self.assert_aborts(
                    [locks_text(**changed), locks_text(**later)],
                    "lock gone or changed",
                )

    def test_exporter_without_the_route_aborts(self):
        self.assert_aborts([OSError("HTTP 404")], "OSError: HTTP 404")

    def test_failing_lock_poll_aborts(self):
        self.assert_aborts(
            [locks_text(ok=False)], "ParseError: the exporter's lock poll is failing"
        )

    def test_lock_type_change_before_the_patch_aborts(self):
        swapped = metrics_text(work=0).replace('lock_type="E"', 'lock_type="T"')
        texts = iter([metrics_text(work=0), swapped])
        cluster = FakeCluster()
        code, out = self.go(cluster, lambda url: next(texts))
        self.assertEqual((code, cluster.patches), (0, []))
        self.assertIn("ABORTED before restart: lock gone or changed", out)

    def test_dry_run_never_patches(self):
        cluster = FakeCluster()
        code, out = self.go(cluster, metrics_text(work=0), dry_run=True)
        self.assertEqual(code, 0)
        self.assertEqual(cluster.patches, [])
        self.assertEqual(self.events, ["fetch metrics"])
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

    def test_partial_lock_selection_is_blind_and_never_patches(self):
        cluster = FakeCluster()
        text = (
            metrics_text(work=0)
            + 'openviking_s3_ovlock_live_age_seconds{key="first-bad"} NaN\n'
            + 'openviking_s3_ovlock_live_age_seconds{key="last-bad"} Inf\n'
        )
        code, out = self.go(cluster, text)
        self.assertEqual((code, cluster.patches), (0, []))
        self.assertIn("decision=blind", out)
        self.assertIn("non-finite", out)
        self.assertIn("first-bad", out)
        self.assertIn("last-bad", out)

    def test_malformed_recheck_aborts_and_retains_all_diagnostics(self):
        cluster = FakeCluster()
        good = metrics_text(work=0)
        bad = (
            good
            + 'openviking_s3_ovlock_live_age_seconds{key="first-bad"} NaN\n'
            + 'openviking_s3_ovlock_live_age_seconds{key="last-bad"} Inf\n'
        )
        reads = iter([good, bad])
        code, out = self.go(cluster, lambda url: next(reads))
        self.assertEqual((code, cluster.patches), (0, []))
        self.assertIn("ABORTED before restart", out)
        self.assertIn("first-bad", out)
        self.assertIn("last-bad", out)

    def test_missing_rbac_is_blind_and_exits_2(self):
        class Blind(FakeCluster):
            def get_deployment(self):
                raise wd.K8sError("HTTP 403 on GET deployments", blind=True)

        cluster = Blind()
        code, out = self.go(cluster, metrics_text(work=0))
        self.assertEqual((code, cluster.patches), (2, []))
        self.assertIn("BLIND", out)

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
        cluster = FakeCluster()
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
        cluster = FakeCluster()
        code, out = self.go(cluster, lambda url: next(texts))
        self.assertEqual((code, cluster.patches), (0, []))
        self.assertIn("queue unreadable", out)

    def test_busy_recheck_past_the_force_cap_still_restarts(self):
        texts = iter([metrics_text(age=7 * HOUR, work=0), metrics_text(age=7 * HOUR)])
        cluster = FakeCluster()
        code, out = self.go(cluster, lambda url: next(texts))
        self.assertEqual(code, 0, out)
        self.assertEqual(len(cluster.patches), 1)
        self.assertIn("forced:", out)

    def test_two_long_locks_still_patch_once(self):
        text = metrics_text(work=0) + (
            f'openviking_s3_ovlock_live_age_seconds{{key="{KEY2}",uri="viking://u/y",'
            f'owner="tok-2",lock_type="E"}} {4 * HOUR}\n'
        )
        cluster = FakeCluster()
        code, out = self.go(cluster, text)
        self.assertEqual(code, 0, out)
        self.assertEqual(len(cluster.patches), 1)
        self.assertEqual(out.count("identity=same-owner"), 2)


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


class TestLockSample(unittest.TestCase):
    def test_reads_the_named_lock(self):
        sample = wd.parse_lock_sample(locks_text(generation=7, mtime=5.5), KEY)
        self.assertEqual(sample, wd.LockSample(7, "tok-1", "E", True, 5.5))

    def test_a_lock_not_listed_is_an_error(self):
        with self.assertRaisesRegex(wd.ParseError, "lock gone"):
            wd.parse_lock_sample(locks_text(), "other-key")

    def test_an_unlabelled_lock_names_no_owner(self):
        doc = json.loads(locks_text())
        for field in ("owner", "lock_type", "owner_read_ok"):
            del doc["locks"][0][field]
        sample = wd.parse_lock_sample(json.dumps(doc), KEY)
        self.assertEqual((sample.owner, sample.owner_read_ok), ("", False))

    def test_a_body_that_is_not_the_expected_document_is_an_error(self):
        no_mtime = json.loads(locks_text())
        del no_mtime["locks"][0]["last_modified"]
        for text in ("not json", "[]", "null", '{"ok": true}', json.dumps(no_mtime)):
            with self.subTest(text), self.assertRaises(wd.ParseError):
                wd.parse_lock_sample(text, KEY)

    def test_the_locks_url_follows_the_exporter_url(self):
        cfg = wd.Config.from_env({"WATCHDOG_EXPORTER_URL": "http://x:9210/metrics"})
        self.assertEqual(cfg.locks_url, "http://x:9210/locks")


class TestParsers(unittest.TestCase):
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

    def test_config_rejects_a_negative_recheck(self):
        with self.assertRaises(ValueError):
            wd.Config.from_env({"WATCHDOG_RECHECK_SECONDS": "-1"})

    def test_config_defaults_match_the_spec(self):
        cfg = wd.Config.from_env({})
        self.assertEqual(
            (cfg.hold, cfg.force, cfg.cooldown, cfg.recheck),
            (10800.0, 21600.0, 3600.0, 30.0),
        )

    def test_config_reads_the_recheck(self):
        cfg = wd.Config.from_env({"WATCHDOG_RECHECK_SECONDS": "45"})
        self.assertEqual(cfg.recheck, 45.0)

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
