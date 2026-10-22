#!/usr/bin/env python3
"""maintenance-active.py — is planned maintenance under way? (IDEA-1150)

A session watch on homelab services asks this before it alerts, so a planned
outage goes to the watch's log instead of waking the session:

    maintenance-active.py [--json] [TARGET ...]

TARGET is kind/namespace/name (deployment/viking/openviking) or a bare
namespace (viking). Exit 0 prints one line per window that covers the targets,
exit 1 means none does, exit 2 means none was found but a source could not be
read (the watch then treats its failure as it would without this check).

Windows, in the order checked:

- timmy-offline: timmy-offline.sh's state under
  ${XDG_STATE_HOME:-~/.local/state}/homelab-timmy-offline/ (a recorded silence,
  parked workloads or suspended CronJobs). timmy is the only control plane, so
  this covers every target.
- node-maintenance: node-maintenance.sh's cycle state beside it (a <node>.boot-id
  owing a finish, a held lock, spun-down services). Covers every target: a cycle
  drains a node and reschedules its pods across the cluster.
- silence: an active Alertmanager silence whose every matcher either matches
  any value (alertname=~".+", as timmy-offline.sh sets) or is a namespace
  matcher that matches a target's namespace. A silence on one alert name says
  nothing about whether its service is down on purpose, so it does not count.
- rollout: a Deployment, StatefulSet or DaemonSet target whose rollout has not
  finished (a newer generation not yet observed, or pods not all updated and
  available).

The state files exist only on the Mac that ran the tool; on the other Mac only
the silence and rollout checks apply. Alertmanager runs on timmy, so during a
timmy outage the silence is unreadable from anywhere and the state files are
the only signal. Read-only: nothing here changes the cluster or the files.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

KUBECTL = os.environ.get("KUBECTL", "kubectl")
AM_NAMESPACE = "grafana"
AM_POD = "alertmanager-prom-alertmanager-0"
CALL_TIMEOUT_SECONDS = 20
ROLLOUT_KINDS = {"deployment", "statefulset", "daemonset"}


class Unreadable(Exception):
    """A source that could not be read; the message names it."""


def state_root(env=os.environ) -> Path:
    return Path(env.get("XDG_STATE_HOME") or Path.home() / ".local/state")


def nonempty(path: Path) -> bool:
    try:
        return path.stat().st_size > 0
    except OSError:
        return False


def local_windows(root: Path) -> list[str]:
    """Windows recorded by timmy-offline.sh and node-maintenance.sh on this Mac."""
    found = []
    offline = root / "homelab-timmy-offline"
    held = [
        name
        for name in ("silence-id", "parked-scales", "suspended-cronjobs")
        if nonempty(offline / name)
    ]
    if held:
        found.append(f"timmy-offline: {', '.join(held)} in {offline}")
    nm = root / "homelab-node-maintenance"
    for boot_id in sorted(nm.glob("*.boot-id")):
        found.append(f"node-maintenance: {boot_id.stem} owes a finish ({boot_id})")
    if nonempty(nm / "lock" / "pid"):
        found.append(f"node-maintenance: a cycle holds {nm / 'lock'}")
    if nonempty(nm / "spin-down-replicas"):
        found.append(
            f"node-maintenance: services spun down ({nm / 'spin-down-replicas'})"
        )
    return found


def parse_target(target: str) -> tuple[str | None, str, str | None]:
    """(kind, namespace, name) from kind/namespace/name or a bare namespace."""
    parts = target.split("/")
    if len(parts) == 1 and parts[0]:
        return None, parts[0], None
    if len(parts) == 3 and all(parts):
        return parts[0].lower(), parts[1], parts[2]
    raise ValueError(f"target must be kind/namespace/name or a namespace: {target!r}")


def matches(matcher: dict, value: str) -> bool:
    """Alertmanager matcher semantics: regexes are anchored, isEqual false negates."""
    if matcher.get("isRegex"):
        hit = re.fullmatch(matcher["value"], value) is not None
    else:
        hit = matcher["value"] == value
    return hit if matcher.get("isEqual", True) else not hit


def matches_anything(matcher: dict) -> bool:
    """True for a matcher no label value can fail, such as alertname=~".+"."""
    return (
        matcher.get("isRegex", False)
        and matcher.get("isEqual", True)
        and (matcher["value"] in (".+", ".*"))
    )


def silence_covers(silence: dict, namespaces: list[str]) -> bool:
    if silence.get("status", {}).get("state", "active") != "active":
        return False
    for m in silence.get("matchers", []):
        if matches_anything(m):
            continue
        if m.get("name") == "namespace" and namespaces:
            if all(matches(m, ns) for ns in namespaces):
                continue
        return False
    return True


def run(cmd: list[str]) -> str:
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=CALL_TIMEOUT_SECONDS
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Unreadable(f"{cmd[0]}: {e}") from None
    if out.returncode != 0:
        raise Unreadable(
            f"{' '.join(cmd[:4])}…: {out.stderr.strip() or out.returncode}"
        )
    return out.stdout


def active_silences() -> list[dict]:
    raw = run(
        [
            KUBECTL,
            "-n",
            AM_NAMESPACE,
            "exec",
            AM_POD,
            "-c",
            "alertmanager",
            "--",
            "amtool",
            "--alertmanager.url=http://localhost:9093",
            "-o",
            "json",
            "silence",
            "query",
        ]
    )
    try:
        silences = json.loads(raw or "[]")
    except ValueError:
        raise Unreadable(f"amtool printed non-JSON: {raw[:80]!r}") from None
    return silences if isinstance(silences, list) else []


def silence_windows(namespaces: list[str]) -> list[str]:
    return [
        f"silence: {s.get('id')} until {s.get('endsAt')} ({s.get('comment', '').strip()})"
        for s in active_silences()
        if silence_covers(s, namespaces)
    ]


def rollout_in_progress(obj: dict) -> str | None:
    """Why the object's rollout has not finished, or None when it has."""
    meta, spec, status = obj["metadata"], obj.get("spec", {}), obj.get("status", {})
    if status.get("observedGeneration", 0) < meta.get("generation", 0):
        return "a new generation is not yet observed"
    if obj["kind"] == "DaemonSet":
        want = status.get("desiredNumberScheduled", 0)
        updated = status.get("updatedNumberScheduled", 0)
        available = status.get("numberAvailable", 0)
    else:
        want = spec.get("replicas", 1)
        updated = status.get("updatedReplicas", 0)
        available = status.get("availableReplicas", 0)
    if updated < want:
        return f"{updated}/{want} pods updated"
    if obj["kind"] == "Deployment" and status.get("replicas", 0) > updated:
        return f"{status['replicas'] - updated} old pods still running"
    if available < want:
        return f"{available}/{want} pods available"
    return None


def rollout_window(kind: str, ns: str, name: str) -> list[str]:
    raw = run(
        [
            KUBECTL,
            "-n",
            ns,
            "get",
            f"{kind}/{name}",
            "-o",
            "json",
            f"--request-timeout={CALL_TIMEOUT_SECONDS}s",
        ]
    )
    try:
        reason = rollout_in_progress(json.loads(raw))
    except (ValueError, KeyError) as e:
        raise Unreadable(f"{kind}/{ns}/{name}: unexpected object ({e})") from None
    return [f"rollout: {kind}/{ns}/{name} ({reason})"] if reason else []


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("targets", nargs="*", metavar="TARGET")
    ap.add_argument("--json", action="store_true", help="print one JSON object")
    args = ap.parse_args(argv)
    try:
        targets = [parse_target(t) for t in args.targets]
    except ValueError as e:
        ap.error(str(e))
    namespaces = sorted({ns for _, ns, _ in targets})

    windows = local_windows(state_root())
    unreadable = []
    checks = [lambda: silence_windows(namespaces)] + [
        (lambda t=t: rollout_window(*t)) for t in targets if t[0] in ROLLOUT_KINDS
    ]
    for check in checks:
        try:
            windows += check()
        except Unreadable as e:
            unreadable.append(str(e))

    code = 0 if windows else (2 if unreadable else 1)
    if args.json:
        print(
            json.dumps(
                {"active": bool(windows), "windows": windows, "unreadable": unreadable}
            )
        )
    else:
        for line in windows:
            print(line)
        for line in unreadable:
            print(f"unreadable: {line}", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
