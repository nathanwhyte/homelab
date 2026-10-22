#!/usr/bin/env python3
"""Hermetic tests for maintenance-active.py (IDEA-1150): no cluster, a stub kubectl."""

import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/maintenance-active.py"
spec = importlib.util.spec_from_file_location("maintenance_active", SCRIPT)
ma = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ma)


def matcher(name, value, regex=False, equal=True):
    return {"name": name, "value": value, "isRegex": regex, "isEqual": equal}


def silence(*matchers, state="active"):
    return {
        "id": "s1",
        "status": {"state": state},
        "endsAt": "2026-10-23T00:00:00Z",
        "comment": "planned",
        "matchers": list(matchers),
    }


def deployment(generation=2, observed=2, replicas=2, updated=2, available=2, total=2):
    return {
        "kind": "Deployment",
        "metadata": {"generation": generation},
        "spec": {"replicas": replicas},
        "status": {
            "observedGeneration": observed,
            "replicas": total,
            "updatedReplicas": updated,
            "availableReplicas": available,
        },
    }


class SilenceTests(unittest.TestCase):
    def test_timmy_offline_silence_covers_everything(self):
        s = silence(matcher("alertname", ".+", regex=True))
        self.assertTrue(ma.silence_covers(s, []))
        self.assertTrue(ma.silence_covers(s, ["viking"]))

    def test_namespace_silence_covers_only_its_namespace(self):
        s = silence(
            matcher("alertname", ".+", regex=True), matcher("namespace", "viking")
        )
        self.assertTrue(ma.silence_covers(s, ["viking"]))
        self.assertFalse(ma.silence_covers(s, ["viking", "garage"]))
        self.assertFalse(ma.silence_covers(s, []))

    def test_regex_namespace_matcher_is_anchored(self):
        s = silence(matcher("namespace", "vik", regex=True))
        self.assertFalse(ma.silence_covers(s, ["viking"]))
        s = silence(matcher("namespace", "vik.*|garage", regex=True))
        self.assertTrue(ma.silence_covers(s, ["viking", "garage"]))

    def test_negated_namespace_matcher(self):
        s = silence(matcher("namespace", "viking", equal=False))
        self.assertFalse(ma.silence_covers(s, ["viking"]))
        self.assertTrue(ma.silence_covers(s, ["garage"]))

    def test_one_alert_name_is_not_maintenance(self):
        s = silence(
            matcher("alertname", "KubeJobFailed"), matcher("namespace", "viking")
        )
        self.assertFalse(ma.silence_covers(s, ["viking"]))

    def test_expired_or_pending_silence_does_not_count(self):
        for state in ("expired", "pending"):
            s = silence(matcher("alertname", ".+", regex=True), state=state)
            self.assertFalse(ma.silence_covers(s, []))


class RolloutTests(unittest.TestCase):
    def test_finished_rollout(self):
        self.assertIsNone(ma.rollout_in_progress(deployment()))

    def test_unobserved_generation(self):
        self.assertIn("generation", ma.rollout_in_progress(deployment(generation=3)))

    def test_pods_not_yet_updated(self):
        self.assertEqual(
            ma.rollout_in_progress(deployment(updated=1)), "1/2 pods updated"
        )

    def test_old_pods_still_running(self):
        reason = ma.rollout_in_progress(deployment(total=3))
        self.assertEqual(reason, "1 old pods still running")

    def test_updated_but_not_available(self):
        self.assertEqual(
            ma.rollout_in_progress(deployment(available=1)), "1/2 pods available"
        )

    def test_scaled_to_zero_is_finished(self):
        self.assertIsNone(
            ma.rollout_in_progress(
                deployment(replicas=0, updated=0, available=0, total=0)
            )
        )

    def test_daemonset_fields(self):
        ds = {
            "kind": "DaemonSet",
            "metadata": {"generation": 1},
            "status": {
                "observedGeneration": 1,
                "desiredNumberScheduled": 3,
                "updatedNumberScheduled": 2,
                "numberAvailable": 3,
            },
        }
        self.assertEqual(ma.rollout_in_progress(ds), "2/3 pods updated")


class TargetTests(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(ma.parse_target("viking"), (None, "viking", None))
        self.assertEqual(
            ma.parse_target("Deployment/viking/openviking"),
            ("deployment", "viking", "openviking"),
        )
        for bad in ("", "viking/openviking", "a//b", "a/b/c/d"):
            with self.assertRaises(ValueError):
                ma.parse_target(bad)


class LocalStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, text="x\n"):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_nothing_recorded(self):
        self.assertEqual(ma.local_windows(self.root), [])

    def test_empty_state_files_do_not_count(self):
        self.write("homelab-timmy-offline/silence-id", "")
        self.write("homelab-timmy-offline/parked-scales", "")
        (self.root / "homelab-node-maintenance").mkdir()
        self.write("homelab-node-maintenance/reboot-timmy.log")  # a log is history
        self.assertEqual(ma.local_windows(self.root), [])

    def test_timmy_offline_and_node_cycle(self):
        self.write("homelab-timmy-offline/silence-id", "abc\n")
        self.write(
            "homelab-timmy-offline/parked-scales", "statefulset/garage/garage\t3\n"
        )
        self.write("homelab-node-maintenance/timmy.boot-id")
        self.write("homelab-node-maintenance/lock/pid", "123\n")
        self.write("homelab-node-maintenance/spin-down-replicas")
        found = ma.local_windows(self.root)
        self.assertEqual(len(found), 4)
        self.assertTrue(found[0].startswith("timmy-offline: silence-id, parked-scales"))
        self.assertIn("timmy owes a finish", found[1])


class StateNamesInSyncTests(unittest.TestCase):
    """The state paths read here are the ones the maintenance scripts write."""

    def test_timmy_offline_names(self):
        text = (ROOT / "scripts/timmy-offline.sh").read_text()
        self.assertIn("STATE_DIR=$STATE_ROOT/homelab-timmy-offline\n", text)
        for name in ("silence-id", "parked-scales", "suspended-cronjobs"):
            self.assertIn(f"=$STATE_DIR/{name}\n", text)

    def test_node_maintenance_names(self):
        text = (ROOT / "scripts/node-maintenance.sh").read_text()
        self.assertIn(
            "MAINTENANCE_STATE_DIR=$STATE_ROOT/homelab-node-maintenance\n", text
        )
        self.assertIn("MAINTENANCE_LOCK_DIR=$MAINTENANCE_STATE_DIR/lock\n", text)
        self.assertIn('"$MAINTENANCE_LOCK_DIR/pid"', text)
        self.assertIn("printf '%s/%s.boot-id\\n' \"$MAINTENANCE_STATE_DIR\"", text)
        self.assertIn(
            "printf '%s/spin-down-replicas\\n' \"$MAINTENANCE_STATE_DIR\"", text
        )


STUB = """#!/bin/sh
case "$*" in
  *amtool*) [ -n "$STUB_SILENCES_FAIL" ] && { echo "connection refused" >&2; exit 1; }
            printf '%s' "$STUB_SILENCES" ;;
  *get*) printf '%s' "$STUB_OBJECT" ;;
  *) exit 99 ;;
esac
"""


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.state = tmp / "state"
        self.state.mkdir()
        kubectl = tmp / "kubectl"
        kubectl.write_text(STUB)
        kubectl.chmod(kubectl.stat().st_mode | stat.S_IXUSR)
        self.env = {
            "PATH": os.environ["PATH"],
            "KUBECTL": str(kubectl),
            "XDG_STATE_HOME": str(self.state),
            "STUB_SILENCES": "[]",
            "STUB_OBJECT": json.dumps(deployment()),
        }

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args, **env):
        out = subprocess.run(
            [sys.executable, "-I", str(SCRIPT), "--json", *args],
            capture_output=True,
            text=True,
            env={**self.env, **env},
            timeout=30,
        )
        return out.returncode, json.loads(out.stdout)

    def test_no_window_exits_1(self):
        code, out = self.cli("deployment/viking/openviking")
        self.assertEqual((code, out["windows"], out["unreadable"]), (1, [], []))

    def test_rollout_window_exits_0(self):
        code, out = self.cli(
            "deployment/viking/openviking",
            STUB_OBJECT=json.dumps(deployment(updated=1)),
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            out["windows"], ["rollout: deployment/viking/openviking (1/2 pods updated)"]
        )

    def test_silence_window_exits_0(self):
        s = silence(matcher("alertname", ".+", regex=True))
        code, out = self.cli(STUB_SILENCES=json.dumps([s]))
        self.assertEqual(code, 0)
        self.assertTrue(out["windows"][0].startswith("silence: s1 until"))

    def test_unreadable_alertmanager_without_a_window_exits_2(self):
        code, out = self.cli("viking", STUB_SILENCES_FAIL="1")
        self.assertEqual(code, 2)
        self.assertIn("connection refused", out["unreadable"][0])

    def test_local_window_wins_over_an_unreadable_cluster(self):
        (self.state / "homelab-timmy-offline").mkdir()
        (self.state / "homelab-timmy-offline/silence-id").write_text("abc\n")
        code, out = self.cli("viking", STUB_SILENCES_FAIL="1")
        self.assertEqual(code, 0)
        self.assertEqual(len(out["unreadable"]), 1)

    def test_bad_target_is_a_usage_error(self):
        out = subprocess.run(
            [sys.executable, "-I", str(SCRIPT), "viking/openviking"],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=30,
        )
        self.assertEqual(out.returncode, 2)
        self.assertIn("kind/namespace/name", out.stderr)


if __name__ == "__main__":
    unittest.main()
