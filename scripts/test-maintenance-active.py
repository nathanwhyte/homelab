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


MATCH_ALL = matcher("alertname", ".+", regex=True)


def silence(*matchers, state="active", sid="s1"):
    return {
        "id": sid,
        "status": {"state": state},
        "endsAt": "2026-10-23T00:00:00Z",
        "comment": "planned",
        "matchers": list(matchers),
    }


def deployment(
    generation=2, observed=2, reason="NewReplicaSetAvailable", status="True"
):
    conditions = [
        {"type": "Available", "status": "True", "reason": "MinimumReplicasAvailable"}
    ]
    if reason:
        conditions.append({"type": "Progressing", "status": status, "reason": reason})
    return {
        "kind": "Deployment",
        "metadata": {"generation": generation},
        "status": {"observedGeneration": observed, "conditions": conditions},
    }


class SilenceTests(unittest.TestCase):
    def test_timmy_offline_silence_covers_everything(self):
        self.assertEqual(ma.silence_scope(silence(MATCH_ALL)), ma.ALL)
        self.assertEqual(
            ma.silence_scope(silence(matcher("alertname", ".*", regex=True))), ma.ALL
        )

    def test_namespace_equality_narrows_it(self):
        s = silence(MATCH_ALL, matcher("namespace", "viking"))
        self.assertEqual(ma.silence_scope(s), "viking")

    def test_regex_or_negated_namespace_does_not_count(self):
        # Round 1 P2: Python's regex engine is not Alertmanager's (Go RE2).
        for m in (
            matcher("namespace", "[[:alpha:]]+", regex=True, equal=False),
            matcher("namespace", "(?<ns>viking)", regex=True),
            matcher("namespace", "viking", equal=False),
        ):
            self.assertIsNone(ma.silence_scope(silence(MATCH_ALL, m)), m)

    def test_match_all_on_an_optional_label_does_not_count(self):
        # Round 1 P2: job=~".+" fails every alert without a job label.
        self.assertIsNone(ma.silence_scope(silence(matcher("job", ".+", regex=True))))
        self.assertIsNone(
            ma.silence_scope(silence(MATCH_ALL, matcher("job", ".+", regex=True)))
        )

    def test_one_alert_name_is_not_maintenance(self):
        s = silence(
            matcher("alertname", "KubeJobFailed"), matcher("namespace", "viking")
        )
        self.assertIsNone(ma.silence_scope(s))

    def test_namespace_alone_or_two_namespaces_do_not_count(self):
        self.assertIsNone(ma.silence_scope(silence(matcher("namespace", "viking"))))
        s = silence(
            MATCH_ALL, matcher("namespace", "viking"), matcher("namespace", "garage")
        )
        self.assertIsNone(ma.silence_scope(s))

    def test_only_active_silences_count(self):
        for state in ("expired", "pending", None):
            s = silence(MATCH_ALL, state=state)
            self.assertIsNone(ma.silence_scope(s))


class RolloutTests(unittest.TestCase):
    def test_rolling_reasons(self):
        for reason in (
            "ReplicaSetUpdated",
            "NewReplicaSetCreated",
            "FoundNewReplicaSet",
        ):
            self.assertEqual(
                ma.rollout_reason(deployment(reason=reason)), f"Progressing: {reason}"
            )

    def test_generation_lag_alone_is_not_a_rollout(self):
        # Round 2 P2: annotation and other non-template edits bump generation.
        for reason, status in (
            ("ProgressDeadlineExceeded", "False"),
            ("NewReplicaSetAvailable", "True"),
        ):
            d = deployment(generation=3, observed=2, reason=reason, status=status)
            self.assertIsNone(ma.rollout_reason(d))

    def test_completed_rollout_with_pods_down_is_not_covered(self):
        # Round 1 P2: availability falling after a finished rollout is an outage.
        self.assertIsNone(ma.rollout_reason(deployment()))

    def test_failed_rollout_is_not_covered(self):
        d = deployment(reason="ProgressDeadlineExceeded", status="False")
        self.assertIsNone(ma.rollout_reason(d))

    def test_no_progressing_condition(self):
        self.assertIsNone(ma.rollout_reason(deployment(reason=None)))


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

    def test_covers(self):
        t = ("deployment", "viking", "openviking")
        self.assertTrue(ma.covers(ma.ALL, t))
        self.assertTrue(ma.covers("viking", t))
        self.assertTrue(ma.covers("deployment/viking/openviking", t))
        self.assertFalse(ma.covers("garage", t))
        self.assertFalse(ma.covers("deployment/viking/other", t))
        self.assertFalse(
            ma.covers("deployment/viking/openviking", (None, "viking", None))
        )


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
        return path

    def lines(self):
        found, unreadable = ma.local_windows(self.root)
        self.assertEqual(unreadable, [])
        return [line for _, line in found]

    def test_nothing_recorded(self):
        self.assertEqual(self.lines(), [])

    def test_empty_state_files_do_not_count(self):
        self.write("homelab-timmy-offline/silence-id", "")
        self.write("homelab-timmy-offline/parked-scales", "")
        self.write("homelab-node-maintenance/timmy.boot-id", "")  # round 1 P2
        self.write("homelab-node-maintenance/wemby.boot-id", "\nlater\n")
        self.write("homelab-node-maintenance/reboot-timmy.log")  # a log is history
        self.assertEqual(self.lines(), [])

    def test_dead_lock_owner_does_not_count(self):
        # Round 1 P2: node-maintenance.sh itself treats this lock as stale.
        proc = subprocess.run(
            [sys.executable, "-c", "import os; print(os.getpid())"],
            capture_output=True,
            text=True,
            check=True,
        )
        self.write("homelab-node-maintenance/lock/pid", proc.stdout)
        self.write("homelab-node-maintenance/lock2/pid", "not-a-pid\n")
        self.assertEqual(self.lines(), [])

    def test_live_lock_owner_counts(self):
        self.write("homelab-node-maintenance/lock/pid", f"{os.getpid()}\n")
        self.assertEqual(len(self.lines()), 1)

    def test_timmy_offline_and_node_cycle(self):
        self.write("homelab-timmy-offline/silence-id", "abc\n")
        self.write(
            "homelab-timmy-offline/parked-scales", "statefulset/garage/garage\t3\n"
        )
        self.write("homelab-node-maintenance/timmy.boot-id", "boot\n")
        self.write(
            "homelab-node-maintenance/spin-down-replicas", "viking/openviking 1\n"
        )
        found = self.lines()
        self.assertEqual(len(found), 3)
        self.assertTrue(found[0].startswith("timmy-offline: silence-id, parked-scales"))
        self.assertIn("timmy owes a finish", found[1])

    @unittest.skipIf(os.geteuid() == 0, "root reads mode-000 files")
    def test_unreadable_state_is_reported_not_treated_as_absent(self):
        # Round 1 P2.
        path = self.write("homelab-timmy-offline/silence-id", "abc\n")
        path.chmod(0)
        try:
            found, unreadable = ma.local_windows(self.root)
        finally:
            path.chmod(0o600)
        self.assertEqual(found, [])
        self.assertEqual(len(unreadable), 1)
        self.assertIn("silence-id", unreadable[0])

    @unittest.skipIf(os.geteuid() == 0, "root reads mode-000 directories")
    def test_unreadable_state_directories_are_reported(self):
        # Round 2 P2: pathlib's is_file() hid a mode-000 parent as "no file".
        for rel in (
            "homelab-timmy-offline",
            "homelab-node-maintenance/lock",
            "homelab-node-maintenance",
        ):
            with self.subTest(rel=rel):
                self.write("homelab-timmy-offline/silence-id", "abc\n")
                self.write("homelab-node-maintenance/lock/pid", f"{os.getpid()}\n")
                d = self.root / rel
                d.chmod(0)
                try:
                    found, unreadable = ma.local_windows(self.root)
                finally:
                    d.chmod(0o700)
                self.assertTrue(unreadable, rel)
                self.assertTrue(all(rel in u for u in unreadable), unreadable)

    @unittest.skipIf(os.geteuid() == 0, "root reads mode-000 directories")
    def test_cli_exits_2_on_an_unreadable_state_directory(self):
        d = self.root / "homelab-node-maintenance"
        d.mkdir()
        d.chmod(0)
        try:
            out = subprocess.run(
                [sys.executable, "-I", str(SCRIPT), "--json", "viking"],
                capture_output=True,
                text=True,
                env={
                    "PATH": os.environ["PATH"],
                    "XDG_STATE_HOME": str(self.root),
                    "KUBECTL": "/usr/bin/true",
                },
                timeout=30,
            )
        finally:
            d.chmod(0o700)
        self.assertEqual(out.returncode, 2, out.stdout)


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
  *deployment/broken*) echo "not found" >&2; exit 1 ;;
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

    def rolling(self):
        return json.dumps(deployment(reason="ReplicaSetUpdated"))

    def test_no_window_exits_1(self):
        code, out = self.cli("deployment/viking/openviking")
        self.assertEqual((code, out["windows"], out["unreadable"]), (1, [], []))

    def test_rollout_window_exits_0(self):
        code, out = self.cli("deployment/viking/openviking", STUB_OBJECT=self.rolling())
        self.assertEqual(code, 0)
        self.assertEqual(
            out["windows"],
            ["rollout: deployment/viking/openviking (Progressing: ReplicaSetUpdated)"],
        )

    def test_one_rollout_does_not_cover_another_target(self):
        # Round 1 P2: coverage is per target, and every target must be covered.
        code, out = self.cli(
            "deployment/viking/openviking", "garage", STUB_OBJECT=self.rolling()
        )
        self.assertEqual((code, out["uncovered"]), (1, ["garage"]))
        code, out = self.cli(
            "deployment/viking/openviking",
            "deployment/viking/broken",
            STUB_OBJECT=self.rolling(),
        )
        self.assertEqual((code, out["uncovered"]), (2, ["deployment/viking/broken"]))

    def test_namespace_silence_covers_only_its_namespace(self):
        s = silence(MATCH_ALL, matcher("namespace", "viking"))
        code, out = self.cli("viking", "garage", STUB_SILENCES=json.dumps([s]))
        self.assertEqual((code, out["uncovered"]), (1, ["garage"]))
        code, out = self.cli(
            "deployment/viking/openviking", STUB_SILENCES=json.dumps([s])
        )
        self.assertEqual(code, 0)
        self.assertTrue(out["windows"][0].startswith("silence: s1 on namespace viking"))

    def test_cluster_silence_with_no_targets(self):
        code, out = self.cli(STUB_SILENCES=json.dumps([silence(MATCH_ALL)]))
        self.assertEqual(code, 0)
        self.assertTrue(out["windows"][0].startswith("silence: s1 on all targets"))
        s = silence(MATCH_ALL, matcher("namespace", "viking"))
        code, _ = self.cli(STUB_SILENCES=json.dumps([s]))
        self.assertEqual(code, 1)

    def test_unreadable_alertmanager_without_a_window_exits_2(self):
        code, out = self.cli("viking", STUB_SILENCES_FAIL="1")
        self.assertEqual(code, 2)
        self.assertIn("connection refused", out["unreadable"][0])

    def test_malformed_silence_list_is_unreadable(self):
        code, out = self.cli("viking", STUB_SILENCES='{"not": "a list"}')
        self.assertEqual(code, 2)

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
