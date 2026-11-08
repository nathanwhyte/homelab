#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""replica-policy.py — compare longhorn/replica-policy.yaml with live Longhorn (IMPR-1478).

    uv run longhorn/scripts/replica-policy.py [--check] [--only NS/PVC ...]
    uv run longhorn/scripts/replica-policy.py --apply [--only NS/PVC ...] [--yes]

--check (the default) is read-only. For each policy row it reads the Volume CR,
its replicas and its PV, and prints one line per row. A row fails when the
Volume CR's replica count, disk or node selector, healthy replica set, or
recurring-job labels differ from the policy. The PV's provision-time replica
count is printed for context only. Bound Longhorn volumes with no policy row
also fail, and so does a node listed under node_tags that lacks its tag.

--apply patches spec.numberOfReplicas, and nothing else, on each row whose count
differs, one volume at a time, waiting for robustness `healthy` before the next.
Selector and node-tag changes are made by hand after a `kubectl diff`.

Exit 0: no failing row. Exit 1: at least one failing row. Exit 2: usage or
kubectl error.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

NS = "longhorn-system"
POLICY = Path(__file__).resolve().parents[1] / "replica-policy.yaml"
DEFAULT_GROUP = "recurring-job-group.longhorn.io/default"
BACKUP_JOB = "recurring-job.longhorn.io/backup-daily"
BACKUP_MODES = {"daily", "manual", "none"}


@dataclass
class Drift:
    pvc: str
    volume: str = "-"
    want: int = 0
    have: int | None = None
    pv_replicas: str = "-"
    nodes: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def _set(value) -> set[str]:
    return set(value or [])


def _recurring_labels(volume: dict) -> dict[str, str]:
    labels = volume.get("metadata", {}).get("labels") or {}
    return {
        k: v
        for k, v in labels.items()
        if k.startswith("recurring-job.longhorn.io/")
        or k.startswith("recurring-job-group.longhorn.io/")
    }


def label_problems(row: dict, volume: dict, exempt_label: str) -> list[str]:
    """What is wrong with a Volume CR's recurring-job labels for the row's backup mode."""
    labels = {k for k, v in _recurring_labels(volume).items() if v == "enabled"}
    mode = row["backup"]
    if mode == "daily":
        if DEFAULT_GROUP in labels or BACKUP_JOB in labels:
            return []
        return ["backup: daily, but neither `default` group nor backup-daily label"]
    problems = []
    if exempt_label not in labels:
        problems.append(f"backup: {mode}, but no {exempt_label}")
    for bad in (DEFAULT_GROUP, BACKUP_JOB):
        if bad in labels:
            problems.append(f"backup: {mode}, but carries {bad.split('/', 1)[1]}")
    return problems


def drift_row(
    row: dict,
    volume: dict | None,
    pv: dict | None,
    replicas: list[dict],
    *,
    exempt_label: str,
    enforce_labels: bool,
) -> Drift:
    """Turn one policy row and its live documents into a drift row (the test seam)."""
    d = Drift(pvc=row["pvc"], want=int(row["replicas"]))
    if pv is not None:
        attrs = (pv.get("spec", {}).get("csi") or {}).get("volumeAttributes") or {}
        d.pv_replicas = str(attrs.get("numberOfReplicas", "-"))
    if volume is None:
        d.problems.append("no Longhorn volume bound to this PVC")
        return d
    spec, status = volume.get("spec", {}), volume.get("status", {})
    d.volume = volume["metadata"]["name"]
    d.have = int(spec.get("numberOfReplicas", 0))
    if d.have != d.want:
        d.problems.append(f"numberOfReplicas {d.have}, policy {d.want}")
    for key, field_name in (
        ("disk_selector", "diskSelector"),
        ("node_selector", "nodeSelector"),
    ):
        if key in row and _set(spec.get(field_name)) != _set(row[key]):
            d.problems.append(
                f"{field_name} {sorted(_set(spec.get(field_name)))}, policy {sorted(_set(row[key]))}"
            )

    attached = status.get("state") == "attached"
    healthy = [
        r
        for r in replicas
        if r.get("spec", {}).get("nodeID")
        and not r.get("spec", {}).get("failedAt")
        and (not attached or r.get("status", {}).get("currentState") == "running")
    ]
    failed = [r for r in replicas if r.get("spec", {}).get("failedAt")]
    d.nodes = sorted(r["spec"]["nodeID"] for r in healthy)
    if len(healthy) != d.want:
        state = "running" if attached else "present (detached)"
        d.problems.append(f"{len(healthy)} healthy replica(s) {state}, policy {d.want}")
    if len(set(d.nodes)) != len(d.nodes):
        d.problems.append(f"replicas share a node: {d.nodes}")
    if "allowed_nodes" in row:
        stray = sorted(set(d.nodes) - _set(row["allowed_nodes"]))
        if stray:
            d.problems.append(
                f"replica on {stray}, allowed {sorted(row['allowed_nodes'])}"
            )
    if failed:
        d.problems.append(
            f"{len(failed)} failed replica(s) left: {sorted(r['metadata']['name'] for r in failed)}"
        )

    lp = label_problems(row, volume, exempt_label)
    (d.problems if enforce_labels else d.notes).extend(lp)
    if d.pv_replicas not in ("-", str(d.want)):
        d.notes.append(f"PV provisioned with {d.pv_replicas}")
    return d


def node_tag_problems(node_tags: dict, nodes: list[dict]) -> list[str]:
    tags = {n["metadata"]["name"]: _set(n.get("spec", {}).get("tags")) for n in nodes}
    problems = []
    for tag, names in (node_tags or {}).items():
        for name in names:
            if name not in tags:
                problems.append(f"node {name} (tag {tag}) is not a Longhorn node")
            elif tag not in tags[name]:
                problems.append(f"node {name} lacks Longhorn tag `{tag}`")
    return problems


def validate_policy(policy: dict) -> list[str]:
    errors = []
    seen = set()
    if policy.get("backup_labels") not in ("report", "enforce"):
        errors.append("backup_labels must be report or enforce")
    for row in policy.get("volumes") or []:
        pvc = row.get("pvc", "?")
        if "/" not in pvc:
            errors.append(f"{pvc}: pvc must be namespace/name")
        if pvc in seen:
            errors.append(f"{pvc}: duplicate row")
        seen.add(pvc)
        if row.get("backup") not in BACKUP_MODES:
            errors.append(f"{pvc}: backup must be one of {sorted(BACKUP_MODES)}")
        if not isinstance(row.get("replicas"), int) or row["replicas"] < 1:
            errors.append(f"{pvc}: replicas must be a positive integer")
    return errors


# --- live reads -----------------------------------------------------------


def kubectl_json(*args: str) -> dict:
    out = subprocess.run(
        ["kubectl", *args, "-o", "json"], capture_output=True, text=True
    )
    if out.returncode != 0:
        sys.exit(f"kubectl {' '.join(args)} failed: {out.stderr.strip()}")
    return json.loads(out.stdout)


def load_live() -> tuple[dict, dict, dict, list[dict]]:
    volumes = kubectl_json("-n", NS, "get", "volumes.longhorn.io")["items"]
    replicas = kubectl_json("-n", NS, "get", "replicas.longhorn.io")["items"]
    pvs = kubectl_json("get", "pv")["items"]
    nodes = kubectl_json("-n", NS, "get", "nodes.longhorn.io")["items"]
    by_pvc = {}
    for v in volumes:
        ks = v.get("status", {}).get("kubernetesStatus") or {}
        if ks.get("namespace") and ks.get("pvcName"):
            by_pvc[f"{ks['namespace']}/{ks['pvcName']}"] = v
    reps: dict[str, list[dict]] = {}
    for r in replicas:
        reps.setdefault(r["spec"]["volumeName"], []).append(r)
    pv_by_name = {p["metadata"]["name"]: p for p in pvs}
    return by_pvc, reps, pv_by_name, nodes


def evaluate(policy: dict, only: list[str]) -> tuple[list[Drift], list[str]]:
    by_pvc, reps, pvs, nodes = load_live()
    enforce = policy["backup_labels"] == "enforce"
    rows = [r for r in policy["volumes"] if not only or r["pvc"] in only]
    drifts = []
    for row in rows:
        vol = by_pvc.get(row["pvc"])
        name = vol["metadata"]["name"] if vol else None
        drifts.append(
            drift_row(
                row,
                vol,
                pvs.get(name) if name else None,
                reps.get(name, []) if name else [],
                exempt_label=policy["exempt_label"],
                enforce_labels=enforce,
            )
        )
    extra = node_tag_problems(policy.get("node_tags"), nodes)
    if not only:
        listed = {r["pvc"] for r in policy["volumes"]}
        extra += [
            f"{pvc}: Longhorn volume has no policy row"
            for pvc in sorted(set(by_pvc) - listed)
        ]
    return drifts, extra


def print_report(drifts: list[Drift], extra: list[str]) -> None:
    width = max((len(d.pvc) for d in drifts), default=10)
    for d in drifts:
        mark = "ok  " if d.ok else "FAIL"
        have = "-" if d.have is None else d.have
        line = f"{mark} {d.pvc:<{width}}  want={d.want} cr={have} pv={d.pv_replicas} nodes={','.join(d.nodes) or '-'}"
        print(line)
        for p in d.problems:
            print(f"       ✗ {p}")
        for n in d.notes:
            print(f"       · {n}")
    for e in extra:
        print(f"FAIL {e}")
    failing = sum(not d.ok for d in drifts) + len(extra)
    print(f"\n{len(drifts)} rows, {failing} failing")


def wait_healthy(volume: str, timeout: int) -> bool:
    """Wait until the attached volume is healthy with every wanted replica in RW mode.

    Robustness alone can still read `healthy` in the seconds before Longhorn
    notices the raised count, so the engine's replica mode map must also hold
    numberOfReplicas entries, all RW (a rebuilding replica is WO).
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(15)
        v = kubectl_json("-n", NS, "get", "volumes.longhorn.io", volume)
        want = int(v["spec"]["numberOfReplicas"])
        if v.get("status", {}).get("robustness") != "healthy":
            continue
        engines = kubectl_json(
            "-n", NS, "get", "engines.longhorn.io", "-l", f"longhornvolume={volume}"
        )["items"]
        modes = [
            m
            for e in engines
            for m in ((e.get("status") or {}).get("replicaModeMap") or {}).values()
        ]
        if len(modes) >= want and all(m == "RW" for m in modes):
            return True
    return False


def apply(policy: dict, only: list[str], yes: bool, timeout: int) -> int:
    drifts, _ = evaluate(policy, only)
    todo = [d for d in drifts if d.have is not None and d.have != d.want]
    if not todo:
        print("no replica count to change")
        return 0
    by_pvc, _, _, _ = load_live()
    for d in todo:
        if by_pvc[d.pvc].get("status", {}).get("state") != "attached":
            print(
                f"{d.pvc}: detached; a raised count cannot rebuild until it attaches, skipping"
            )
            continue
        print(f"{d.pvc} ({d.volume}): numberOfReplicas {d.have} -> {d.want}")
        if not yes and input("  patch? [y/N] ").strip().lower() != "y":
            print("  skipped")
            continue
        patch = json.dumps({"spec": {"numberOfReplicas": d.want}})
        out = subprocess.run(
            [
                "kubectl",
                "-n",
                NS,
                "patch",
                "volumes.longhorn.io",
                d.volume,
                "--type",
                "merge",
                "-p",
                patch,
            ],
            capture_output=True,
            text=True,
        )
        if out.returncode != 0:
            print(f"  patch failed: {out.stderr.strip()}")
            return 2
        print(f"  patched; waiting up to {timeout}s for healthy")
        if not wait_healthy(d.volume, timeout):
            print("  not healthy in time; stopping before the next volume")
            return 1
        print("  healthy")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="report drift (default)")
    mode.add_argument(
        "--apply", action="store_true", help="patch numberOfReplicas where it differs"
    )
    mode.add_argument(
        "--daily-json",
        action="store_true",
        help="print the backup: daily PVCs as JSON for the backup-freshness exporter",
    )
    ap.add_argument("--only", action="append", default=[], metavar="NS/PVC")
    ap.add_argument("--policy", type=Path, default=POLICY)
    ap.add_argument(
        "--yes", action="store_true", help="with --apply, do not prompt per volume"
    )
    ap.add_argument(
        "--timeout",
        type=int,
        default=3600,
        help="seconds to wait for healthy per volume",
    )
    args = ap.parse_args(argv)

    policy = yaml.safe_load(args.policy.read_text())
    errors = validate_policy(policy)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 2
    if args.daily_json:
        daily = sorted(r["pvc"] for r in policy["volumes"] if r["backup"] == "daily")
        print(json.dumps(daily, indent=2))
        return 0
    unknown = set(args.only) - {r["pvc"] for r in policy["volumes"]}
    if unknown:
        print(f"--only names no policy row: {sorted(unknown)}", file=sys.stderr)
        return 2
    if args.apply:
        return apply(policy, args.only, args.yes, args.timeout)
    drifts, extra = evaluate(policy, args.only)
    print_report(drifts, extra)
    return 0 if all(d.ok for d in drifts) and not extra else 1


if __name__ == "__main__":
    sys.exit(main())
