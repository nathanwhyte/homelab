#!/usr/bin/env python3
"""maintenance-active.py — is planned maintenance under way? (IDEA-1150)

A session watch on homelab services asks this before it alerts, so a planned
outage goes to the watch's log instead of waking the session:

    maintenance-active.py [--json] [TARGET ...]

TARGET is kind/namespace/name (deployment/viking/openviking) or a bare
namespace (viking). Exit 0 means every target is covered by a window and
prints the windows; with no TARGET, it means a cluster-wide window is active.
Exit 1 means some target is not covered. Exit 2 means some target is not
covered and a source could not be read, so the watch treats its failure as it
would without this check.

Every signal is one its producer writes on purpose and reads with the same
test, never an inference from cluster state:

- timmy-offline: timmy-offline.sh's state under
  ${XDG_STATE_HOME:-~/.local/state}/homelab-timmy-offline/: silence-id,
  parked-scales or suspended-cronjobs non-empty (the script's own -s test).
  timmy is the only control plane, so this covers every target.
- node-maintenance: node-maintenance.sh's state beside it: a <node>.boot-id
  whose first line is non-empty (load_cycle_boot_id), a lock/pid naming a live
  process (acquire_maintenance_lock treats any other lock as stale), or a
  non-empty spin-down-replicas. Covers every target: a cycle drains a node and
  reschedules pods across the cluster.
- silence: an active Alertmanager silence whose matchers are only
  alertname=~".+" (or ".*"), the form timmy-offline.sh creates, which covers
  every target, plus at most one namespace="<ns>" equality matcher, which
  narrows it to targets in that namespace. Any other matcher, including a regex
  or negated namespace matcher, makes it not count: it silences particular
  alerts, which says nothing about whether a service is down on purpose.
- rollout: a deployment/<ns>/<name> target whose Progressing condition, as
  the Deployment controller set it, says a rollout is under way
  (ReplicaSetUpdated, NewReplicaSetCreated, FoundNewReplicaSet). It ends when
  the rollout completes (NewReplicaSetAvailable) or fails
  (ProgressDeadlineExceeded), so a crash after or instead of a rollout is never
  covered. Generation lag alone is not a rollout: annotation and other
  non-template edits bump the generation too. Covers that target only; other
  kinds get no rollout check.

The state files exist only on the Mac that ran the tool; on the other Mac only
the silence and rollout checks apply. Alertmanager runs on timmy, so during a
timmy outage the silence is unreadable from anywhere and the state files are
the only signal. Read-only: nothing here changes the cluster or the files.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

KUBECTL = os.environ.get("KUBECTL", "kubectl")
AM_NAMESPACE = "grafana"
AM_POD = "alertmanager-prom-alertmanager-0"
CALL_TIMEOUT_SECONDS = 20
ROLLING_REASONS = {"ReplicaSetUpdated", "NewReplicaSetCreated", "FoundNewReplicaSet"}
ALL = "*"  # a window's scope: ALL, a namespace, or one target string


class Unreadable(Exception):
    """A source that could not be read; the message names it."""


def state_root(env=os.environ) -> Path:
    return Path(env.get("XDG_STATE_HOME") or Path.home() / ".local/state")


def absent_or_raise(path: Path, e: OSError) -> None:
    """Every filesystem error but absence is Unreadable. Only raw os calls go
    through here: pathlib predicates such as is_file() return False on a
    permission error, which would read as "no window"."""
    if isinstance(e, (FileNotFoundError, NotADirectoryError)):
        return None
    raise Unreadable(f"{path}: {e}") from None


def read_state(path: Path) -> str | None:
    """A regular file's text (stat follows symlinks, like the producers' -s),
    None when it does not exist or is not a regular file; Unreadable otherwise."""
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):
            return None
        with open(path, errors="replace") as fh:
            return fh.read()
    except OSError as e:
        return absent_or_raise(path, e)


def list_dir(path: Path) -> list[str]:
    try:
        return sorted(os.listdir(path))
    except OSError as e:
        absent_or_raise(path, e)
        return []


def pid_alive(text: str) -> bool:
    """acquire_maintenance_lock's test: digits only, and kill -0 succeeds."""
    pid = text.split("\n", 1)[0]
    if not pid.isdigit() or not pid.isascii():
        return False
    try:
        os.kill(int(pid), 0)
    except OSError:
        return False
    return True


def local_windows(root: Path) -> tuple[list[tuple[str, str]], list[str]]:
    """(scope, line) windows recorded on this Mac, and the paths that failed."""
    found, unreadable = [], []

    def state(path: Path) -> str | None:
        try:
            return read_state(path)
        except Unreadable as e:
            unreadable.append(str(e))
            return None

    offline = root / "homelab-timmy-offline"
    held = [
        name
        for name in ("silence-id", "parked-scales", "suspended-cronjobs")
        if state(offline / name)
    ]
    if held:
        found.append((ALL, f"timmy-offline: {', '.join(held)} in {offline}"))

    nm = root / "homelab-node-maintenance"
    try:
        names = list_dir(nm)
    except Unreadable as e:
        unreadable.append(str(e))
        names = []
    for name in names:
        if not name.endswith(".boot-id"):
            continue
        text = state(nm / name)
        if text and text.split("\n", 1)[0]:
            node = name.removesuffix(".boot-id")
            found.append((ALL, f"node-maintenance: {node} owes a finish ({nm / name})"))
    lock = state(nm / "lock" / "pid")
    if lock and pid_alive(lock):
        found.append((ALL, f"node-maintenance: pid {lock.strip()} holds {nm / 'lock'}"))
    if state(nm / "spin-down-replicas"):
        found.append(
            (ALL, f"node-maintenance: services spun down ({nm / 'spin-down-replicas'})")
        )
    return found, unreadable


def parse_target(target: str) -> tuple[str | None, str, str | None]:
    """(kind, namespace, name) from kind/namespace/name or a bare namespace."""
    parts = target.split("/")
    if len(parts) == 1 and parts[0]:
        return None, parts[0], None
    if len(parts) == 3 and all(parts):
        return parts[0].lower(), parts[1], parts[2]
    raise ValueError(f"target must be kind/namespace/name or a namespace: {target!r}")


def silence_scope(silence: dict) -> str | None:
    """ALL, a namespace, or None when the silence does not mark maintenance."""
    if silence.get("status", {}).get("state") != "active":
        return None
    namespaces, saw_match_all = set(), False
    for m in silence.get("matchers", []):
        regex, equal = m.get("isRegex", False), m.get("isEqual", True)
        if (
            m.get("name") == "alertname"
            and regex
            and equal
            and m.get("value") in (".+", ".*")
        ):
            saw_match_all = True
        elif m.get("name") == "namespace" and not regex and equal and m.get("value"):
            namespaces.add(m["value"])
        else:
            return None
    if not saw_match_all or len(namespaces) > 1:
        return None
    return namespaces.pop() if namespaces else ALL


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


def parse_silences(raw: str) -> list[dict]:
    """amtool's JSON, every field silence_scope reads checked for type; any
    other shape (empty output included) is Unreadable, never "no silence"."""
    try:
        silences = json.loads(raw)
    except ValueError:
        raise Unreadable(f"amtool printed non-JSON: {raw[:80]!r}") from None

    def bad(why: str) -> Unreadable:
        return Unreadable(f"amtool silence JSON: {why}")

    if not isinstance(silences, list):
        raise bad(f"{type(silences).__name__}, not a list")
    for s in silences:
        if not isinstance(s, dict):
            raise bad(f"entry {s!r} is not an object")
        status, matchers = s.get("status"), s.get("matchers")
        if not isinstance(status, dict) or not isinstance(status.get("state"), str):
            raise bad(f"silence {s.get('id')!r} has no status.state")
        if not isinstance(matchers, list) or not matchers:
            raise bad(f"silence {s.get('id')!r} has no matchers")
        for m in matchers:
            if not (
                isinstance(m, dict)
                and isinstance(m.get("name"), str)
                and isinstance(m.get("value"), str)
                and isinstance(m.get("isRegex"), bool)
                and isinstance(m.get("isEqual", True), bool)
            ):
                raise bad(f"silence {s.get('id')!r} has a malformed matcher {m!r}")
    return silences


def silence_windows() -> list[tuple[str, str]]:
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
    found = []
    for s in parse_silences(raw):
        scope = silence_scope(s)
        if scope is not None:
            where = "all targets" if scope == ALL else f"namespace {scope}"
            found.append(
                (
                    scope,
                    f"silence: {s.get('id')} on {where} until {s.get('endsAt')}"
                    f" ({str(s.get('comment', '')).strip()})",
                )
            )
    return found


def rollout_reason(obj: dict) -> str | None:
    """Why a Deployment is mid-rollout per its controller, or None."""
    for cond in obj.get("status", {}).get("conditions", []):
        if cond.get("type") == "Progressing":
            if cond.get("status") == "True" and cond.get("reason") in ROLLING_REASONS:
                return f"Progressing: {cond['reason']}"
            return None
    return None


def rollout_window(ns: str, name: str) -> list[tuple[str, str]]:
    target = f"deployment/{ns}/{name}"
    raw = run(
        [
            KUBECTL,
            "-n",
            ns,
            "get",
            f"deployment/{name}",
            "-o",
            "json",
            f"--request-timeout={CALL_TIMEOUT_SECONDS}s",
        ]
    )
    try:
        reason = rollout_reason(json.loads(raw))
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        raise Unreadable(f"{target}: unexpected object ({e})") from None
    return [(target, f"rollout: {target} ({reason})")] if reason else []


def covers(scope: str, target: tuple[str | None, str, str | None]) -> bool:
    kind, ns, name = target
    return scope in (ALL, ns) or (kind is not None and scope == f"{kind}/{ns}/{name}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("targets", nargs="*", metavar="TARGET")
    ap.add_argument("--json", action="store_true", help="print one JSON object")
    args = ap.parse_args(argv)
    try:
        targets = [parse_target(t) for t in args.targets]
    except ValueError as e:
        ap.error(str(e))

    windows, unreadable = local_windows(state_root())
    checks = [silence_windows] + [
        (lambda ns=ns, name=name: rollout_window(ns, name))
        for kind, ns, name in targets
        if kind == "deployment"
    ]
    for check in checks:
        try:
            windows += check()
        except Unreadable as e:
            unreadable.append(str(e))
        except Exception as e:  # any other failure is still "could not tell"
            unreadable.append(f"unexpected {type(e).__name__}: {e}")

    relevant = [
        line
        for scope, line in windows
        if scope == ALL or any(covers(scope, t) for t in targets)
    ]
    if targets:
        uncovered = [
            args.targets[i]
            for i, t in enumerate(targets)
            if not any(covers(scope, t) for scope, _ in windows)
        ]
    else:
        uncovered = [] if any(scope == ALL for scope, _ in windows) else ["(cluster)"]
    code = 0 if not uncovered else (2 if unreadable else 1)
    if args.json:
        print(
            json.dumps(
                {
                    "covered": not uncovered,
                    "windows": relevant,
                    "uncovered": uncovered,
                    "unreadable": unreadable,
                }
            )
        )
    else:
        for line in relevant:
            print(line)
        for line in unreadable:
            print(f"unreadable: {line}", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
