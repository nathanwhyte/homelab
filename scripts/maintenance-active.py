#!/usr/bin/env python3
"""maintenance-active.py — is planned maintenance under way? (IDEA-1150)

A session watch on homelab services asks this before it alerts, so a planned
outage goes to the watch's log instead of waking the session:

    maintenance-active.py [--json] [TARGET ...]

TARGET is kind/namespace/name (deployment/viking/openviking) or a bare
namespace (viking); a window covers a target through its namespace, since no
signal names a single object. Exit 0 means every target is covered by a window
and prints the windows; with no TARGET, it means a cluster-wide window is active.
Exit 1 means some target is not covered. Exit 2 means some target is not
covered and a source could not be read, so the watch treats its failure as it
would without this check. Exit 64 is a usage error or --help.

A window is a record a maintenance tool writes to say one is open, never an
inference from cluster state or from what a tool has left to undo:

- window files: "<end epoch seconds> <what>" in
  ${XDG_STATE_HOME:-~/.local/state}/homelab-timmy-offline/window (written by
  timmy-offline.sh down before it parks anything, removed when up completes) and
  homelab-node-maintenance/window (opened at a reboot cycle's first change,
  removed by finish or restore-services) and apt-window beside it (open while
  node-maintenance.sh apt runs, and for one more APT_TIMEOUT_SECONDS when it
  stops while a started remote upgrade has not been seen to end). One counts
  until its end, which bounds a cycle that was backed out or abandoned. A file
  that is not exactly one such line, or not a regular file, is unreadable. Covers every target: timmy is the
  only control plane, a cycle drains a node and reschedules pods across the
  cluster, and an upgrade can restart k3s or containerd.
- silence: an active Alertmanager silence whose matchers are only
  alertname=~".+" (or ".*"), the form timmy-offline.sh creates, which covers
  every target, plus at most one namespace="<ns>" equality matcher on a valid
  namespace name, which narrows it to targets in that namespace. Any other
  matcher, including a regex or negated namespace matcher, makes it not count:
  it silences particular alerts, which says nothing about whether a service is
  down on purpose.

A rollout is not a window. No Deployment field says one was planned: the
controller's Progressing reasons also fire for edits that change no pods
(FoundNewReplicaSet on a progressDeadlineSeconds change), so a watch handles a
failure during a rollout as any other until a rollout tool records one on
purpose (IDEA-1151).

The window files exist only on the Mac that ran the tool; on the other Mac only
the silence check applies. Alertmanager runs on timmy, so during a timmy
outage the silence is unreadable from anywhere and the window file is the only
signal (IDEA-1152). Read-only: nothing here changes the cluster or the files.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path

KUBECTL = os.environ.get("KUBECTL", "kubectl")
AM_NAMESPACE = "grafana"
AM_POD = "alertmanager-prom-alertmanager-0"
CALL_TIMEOUT_SECONDS = 20
WINDOW_FILES = (
    ("timmy-offline", "homelab-timmy-offline/window"),
    ("node-maintenance", "homelab-node-maintenance/window"),
    ("node-maintenance", "homelab-node-maintenance/apt-window"),
)
ALL = "*"  # a window's scope: ALL or a namespace
# A Kubernetes namespace name (RFC 1123 label), so no value can equal ALL.
NAMESPACE = re.compile(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?")
# Exactly what the producers' printf '%s %s\n' "$(($(date +%s) + n))" "<what>"
# writes: one line, a 10-digit epoch (2001-2286), a printable description.
WINDOW = re.compile(r"([1-9][0-9]{9}) ([^\x00-\x20\x7f][^\x00-\x1f\x7f]*)\n")
EX_USAGE = 64


class Unreadable(Exception):
    """A source that could not be read; the message names it."""


def state_root(env=os.environ) -> Path:
    return Path(env.get("XDG_STATE_HOME") or Path.home() / ".local/state")


def local_time(when: datetime) -> str:
    return when.astimezone().strftime("%Y-%m-%d %H:%M %Z %z")


def read_state(path: Path) -> str | None:
    """A file's text (stat follows symlinks), None when nothing is at the path,
    Unreadable on any other error or file type, a dangling symlink included. Raw os calls only: pathlib
    predicates such as is_file() return False on a permission error, which
    would read as "no window"."""
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):
            raise Unreadable(f"{path}: not a regular file")
        # newline="": a CR stays a CR, which the grammar rejects.
        with open(path, errors="replace", newline="") as fh:
            return fh.read()
    except (FileNotFoundError, NotADirectoryError):
        if os.path.lexists(path):
            raise Unreadable(f"{path}: a symlink to nothing") from None
        return None
    except OSError as e:
        raise Unreadable(f"{path}: {e}") from None


def parse_window(text: str, path: Path, now: float) -> str | None:
    """The window's description while it is open, None once its end has
    passed; any other content is Unreadable."""
    m = WINDOW.fullmatch(text)
    if not m:
        raise Unreadable(f"{path}: not one '<end epoch> <what>' line: {text[:80]!r}")
    end, what = int(m[1]), m[2].strip()
    if end <= now:
        return None
    return f"{what} until {local_time(datetime.fromtimestamp(end, timezone.utc))}"


def local_windows(
    root: Path, now: float | None = None
) -> tuple[list[tuple[str, str]], list[str]]:
    """(scope, line) windows recorded on this Mac, and the files that failed."""
    now = time.time() if now is None else now
    found, unreadable = [], []
    for tool, rel in WINDOW_FILES:
        path = root / rel
        try:
            text = read_state(path)
            what = parse_window(text, path, now) if text is not None else None
        except Unreadable as e:
            unreadable.append(str(e))
            continue
        if what:
            found.append((ALL, f"{tool}: {what} ({path})"))
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
        elif (
            m.get("name") == "namespace"
            and not regex
            and equal
            and NAMESPACE.fullmatch(m.get("value", ""))
        ):
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


def ends_at(value: object) -> str:
    """Alertmanager's RFC 3339 end in local time, or as given if it is not one."""
    text = str(value)
    # fromisoformat takes at most microseconds; Go prints up to nanoseconds.
    trimmed = re.sub(r"(\.\d{6})\d+", r"\1", text).replace("Z", "+00:00")
    try:
        return local_time(datetime.fromisoformat(trimmed))
    except ValueError:
        return text


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
                    f"silence: {s.get('id')} on {where} until {ends_at(s.get('endsAt'))}"
                    f" ({str(s.get('comment', '')).strip()})",
                )
            )
    return found


def covers(scope: str, target: tuple[str | None, str, str | None]) -> bool:
    return scope in (ALL, target[1])


class Parser(argparse.ArgumentParser):
    def exit(self, status=0, message=None):
        # argparse exits only for --help (0) and usage errors (2), which here
        # would read as "covered" and "could not tell".
        super().exit(EX_USAGE, message)


def main(argv: list[str] | None = None) -> int:
    ap = Parser(description=__doc__.split("\n\n")[0])
    ap.add_argument("targets", nargs="*", metavar="TARGET")
    ap.add_argument("--json", action="store_true", help="print one JSON object")
    args = ap.parse_intermixed_args(argv)
    try:
        targets = [parse_target(t) for t in args.targets]
    except ValueError as e:
        ap.error(str(e))

    windows, unreadable = [], []
    for check in (lambda: local_windows(state_root()), lambda: (silence_windows(), [])):
        try:
            found, failed = check()
            windows += found
            unreadable += failed
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
