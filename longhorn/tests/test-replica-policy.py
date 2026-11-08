#!/usr/bin/env python3
"""Hermetic tests for longhorn/scripts/replica-policy.py's drift seam (IMPR-1478).

Shapes are trimmed from the live Volume, Replica and PV documents read
2026-11-08. Run: uv run --no-project --with pyyaml python3 longhorn/tests/test-replica-policy.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_scripts" / "lib"))
from git_env import scrub_environ  # noqa: E402

scrub_environ()

import importlib.util  # noqa: E402
import unittest  # noqa: E402

import yaml  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "replica_policy", ROOT / "longhorn/scripts/replica-policy.py"
)
rp = importlib.util.module_from_spec(spec)
# dataclasses resolve annotations through sys.modules
sys.modules["replica_policy"] = rp
spec.loader.exec_module(rp)

EXEMPT = "recurring-job-group.longhorn.io/manual"


def volume(n=2, disk=("ssd",), node=(), state="attached", labels=None, name="pvc-1"):
    return {
        "metadata": {
            "name": name,
            "labels": {"recurring-job-group.longhorn.io/default": "enabled"}
            if labels is None
            else labels,
        },
        "spec": {
            "numberOfReplicas": n,
            "diskSelector": list(disk),
            "nodeSelector": list(node),
        },
        "status": {"state": state, "robustness": "healthy"},
    }


def replica(node, state="running", failed=""):
    return {
        "metadata": {"name": f"pvc-1-r-{node or 'none'}"},
        "spec": {"volumeName": "pvc-1", "nodeID": node, "failedAt": failed},
        "status": {"currentState": state},
    }


def pv(n):
    return {"spec": {"csi": {"volumeAttributes": {"numberOfReplicas": str(n)}}}}


ROW = {"pvc": "ns/data", "replicas": 2, "disk_selector": ["ssd"], "backup": "daily"}


def drift(row=ROW, vol=None, p=None, reps=None, enforce=True):
    return rp.drift_row(
        row,
        volume() if vol is None else vol,
        pv(2) if p is None else p,
        [replica("timmy"), replica("manu")] if reps is None else reps,
        exempt_label=EXEMPT,
        enforce_labels=enforce,
    )


class DriftRow(unittest.TestCase):
    def test_matching_volume_passes(self):
        d = drift()
        self.assertTrue(d.ok, d.problems)
        self.assertEqual(d.nodes, ["manu", "timmy"])

    def test_cr_below_policy_fails_and_pv_is_only_a_note(self):
        # Hand reduction: media's CR went 2 -> 1 while its PV kept 2 (BUG-1101).
        d = drift(vol=volume(n=1), reps=[replica("wemby")])
        self.assertFalse(d.ok)
        self.assertIn("numberOfReplicas 1, policy 2", d.problems)
        self.assertFalse(any("PV" in p for p in d.problems))

    def test_cr_raised_above_pv_passes_with_a_note(self):
        # grafana-storage after the raise: CR 2, PV provisioned with 1.
        d = drift(p=pv(1))
        self.assertTrue(d.ok, d.problems)
        self.assertIn("PV provisioned with 1", d.notes)

    def test_two_replicas_on_one_node_fail(self):
        d = drift(reps=[replica("timmy"), replica("timmy")])
        self.assertTrue(any("share a node" in p for p in d.problems))

    def test_replica_outside_allowed_nodes_fails(self):
        row = dict(ROW, allowed_nodes=["timmy", "manu"])
        d = drift(row=row, reps=[replica("timmy"), replica("wemby")])
        self.assertTrue(any("replica on ['wemby']" in p for p in d.problems))

    def test_failed_leftover_is_reported_and_not_counted(self):
        # coach/postgres-data on 2026-11-08: two running plus a failed one on timmy.
        reps = [
            replica("wemby"),
            replica("manu"),
            replica("timmy", "stopped", "2026-11-08T07:03:05Z"),
        ]
        d = drift(reps=reps)
        self.assertEqual(d.nodes, ["manu", "wemby"])
        self.assertTrue(any("failed replica" in p for p in d.problems))

    def test_rebuilding_or_stopped_replica_on_attached_volume_is_not_healthy(self):
        d = drift(reps=[replica("timmy"), replica("manu", "stopped")])
        self.assertIn("1 healthy replica(s) running, policy 2", d.problems)

    def test_unscheduled_replica_is_ignored(self):
        # garage/data-garage-0: a best-effort replica with no node yet.
        row = dict(ROW, replicas=1)
        d = drift(
            row=row,
            vol=volume(n=1),
            p=pv(1),
            reps=[replica("manu"), replica("", "stopped")],
        )
        self.assertTrue(d.ok, d.problems)

    def test_detached_volume_counts_stopped_replicas(self):
        d = drift(
            vol=volume(state="detached"),
            reps=[replica("wemby", "stopped"), replica("timmy", "stopped")],
        )
        self.assertTrue(d.ok, d.problems)

    def test_selector_mismatch_fails(self):
        row = dict(ROW, node_selector=["stateful"])
        d = drift(row=row, vol=volume(disk=("nvme",)))
        self.assertIn("diskSelector ['nvme'], policy ['ssd']", d.problems)
        self.assertIn("nodeSelector [], policy ['stateful']", d.problems)

    def test_missing_volume_fails(self):
        d = rp.drift_row(ROW, None, None, [], exempt_label=EXEMPT, enforce_labels=True)
        self.assertEqual(d.problems, ["no Longhorn volume bound to this PVC"])


class Labels(unittest.TestCase):
    def test_daily_needs_default_or_job_label(self):
        self.assertEqual(rp.label_problems(ROW, volume(), EXEMPT), [])
        job = volume(labels={"recurring-job.longhorn.io/backup-daily": "enabled"})
        self.assertEqual(rp.label_problems(ROW, job, EXEMPT), [])
        # copyparty today: only its own snapshot job, so out of `default`.
        snap = volume(
            labels={"recurring-job.longhorn.io/copyparty-files-snap-daily": "enabled"}
        )
        self.assertEqual(len(rp.label_problems(ROW, snap, EXEMPT)), 1)

    def test_exempt_row_needs_exempt_label_and_no_default(self):
        row = dict(ROW, backup="manual")
        good = volume(
            labels={
                EXEMPT: "enabled",
                "recurring-job.longhorn.io/prometheus-filesystem-trim-weekly": "enabled",
            }
        )
        self.assertEqual(rp.label_problems(row, good, EXEMPT), [])
        both = volume(
            labels={
                EXEMPT: "enabled",
                "recurring-job-group.longhorn.io/default": "enabled",
            }
        )
        self.assertEqual(
            rp.label_problems(row, both, EXEMPT),
            ["backup: manual, but carries default"],
        )
        self.assertEqual(len(rp.label_problems(row, volume(), EXEMPT)), 2)

    def test_report_mode_turns_label_problems_into_notes(self):
        row = dict(ROW, backup="none")
        d = drift(row=row, enforce=False)
        self.assertTrue(d.ok, d.problems)
        self.assertTrue(d.notes)
        self.assertFalse(drift(row=row, enforce=True).ok)


class PolicyFile(unittest.TestCase):
    def test_repo_policy_is_valid(self):
        policy = yaml.safe_load((ROOT / "longhorn/replica-policy.yaml").read_text())
        self.assertEqual(rp.validate_policy(policy), [])

    def test_validation_catches_bad_rows(self):
        bad = {
            "backup_labels": "maybe",
            "volumes": [
                {"pvc": "nons", "replicas": 0, "backup": "weekly"},
                {"pvc": "a/b", "replicas": 1, "backup": "none"},
                {"pvc": "a/b", "replicas": 1, "backup": "none"},
            ],
        }
        errors = rp.validate_policy(bad)
        self.assertEqual(len(errors), 5, errors)

    def test_node_tags(self):
        nodes = [
            {"metadata": {"name": "timmy"}, "spec": {"tags": ["ethernet", "stateful"]}},
            {"metadata": {"name": "manu"}, "spec": {"tags": ["ethernet"]}},
        ]
        self.assertEqual(
            rp.node_tag_problems({"stateful": ["timmy", "manu"]}, nodes),
            ["node manu lacks Longhorn tag `stateful`"],
        )


if __name__ == "__main__":
    unittest.main()
