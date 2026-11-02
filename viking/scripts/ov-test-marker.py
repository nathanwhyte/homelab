#!/usr/bin/env python3
"""Mark ov-test as in use by an experiment, show the mark, or clear it.

IMPR-1456. On 2026-11-01 one session planned a canary on ov-test while another
was mid-experiment there, and nothing in the cluster said so. The mark is an
annotation on Deployment/openviking-test naming the item, the session, the host
and the start time, so it reads the same from either machine; ov-test-drift.py
prints it.

The annotation sits on the Deployment's own metadata, not its pod template, so
setting or clearing it never rolls the pod, and a client-side `kubectl apply`
of viking/manifests/test keeps it, since the manifest never declared it.

  ov-test-marker.py show
  ov-test-marker.py set --item BUG-1285 [--note "sanitizer canary"]
  ov-test-marker.py clear

`set` and `clear` refuse a mark another session holds unless --force is given,
and pass the Deployment's resourceVersion so a concurrent change fails instead
of being overwritten. The session defaults to $CLAUDE_CODE_SESSION_ID.

Exit 0: done (or, for show, read). Exit 2: kubectl failed or a concurrent
change. Exit 3: refused, the mark belongs to another session.
"""

import argparse
import datetime as dt
import json
import os
import platform
import subprocess
import sys

NAMESPACE = "viking"
DEPLOYMENT = "openviking-test"
ANNOTATION = "homelab.nathanwhyte.dev/ov-test-in-use"
REQUEST_TIMEOUT = "15s"
EXIT_KUBECTL = 2
EXIT_REFUSED = 3


class MarkerError(Exception):
    """kubectl failed, or returned something that is not the Deployment."""


def kubectl(*args):
    proc = subprocess.run(
        ["kubectl", "-n", NAMESPACE, f"--request-timeout={REQUEST_TIMEOUT}", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise MarkerError(f"kubectl {args[0]}: {proc.stderr.strip()[:300]}")
    return proc.stdout


def read_deployment():
    """The mark (a dict, a raw string when unparseable, or None) and the
    Deployment's resourceVersion."""
    try:
        obj = json.loads(kubectl("get", "deployment", DEPLOYMENT, "-o", "json"))
        meta = obj["metadata"]
        version = meta["resourceVersion"]
        raw = (meta.get("annotations") or {}).get(ANNOTATION)
    except MarkerError:
        raise
    except (ValueError, KeyError, TypeError, AttributeError):
        raise MarkerError(f"kubectl get: not a Deployment ({DEPLOYMENT})") from None
    return parse(raw), version


def parse(raw):
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return raw
    return value if isinstance(value, dict) else raw


def age(since, now):
    try:
        start = dt.datetime.fromisoformat(since.replace("Z", "+00:00"))
        seconds = int((now - start).total_seconds())
    except (AttributeError, TypeError, ValueError):
        return "age unknown"
    if seconds < 0:
        return "start in the future"
    hours, rest = divmod(seconds, 3600)
    return f"{hours}h{rest // 60:02d}m ago"


def describe(mark, now=None):
    """One line for a mark, as `show` and ov-test-drift.py print it."""
    if mark is None:
        return "in use: not marked"
    if isinstance(mark, str):
        return f"in use: unreadable mark {mark[:120]!r}"
    now = now or dt.datetime.now(dt.timezone.utc)
    parts = [
        str(mark.get("item", "?")),
        f"session {mark.get('session', '?')}",
        f"host {mark.get('host', '?')}",
        f"since {mark.get('since', '?')} ({age(mark.get('since'), now)})",
    ]
    if mark.get("note"):
        parts.append(f"note {mark['note']!r}")
    return "in use: " + ", ".join(parts)


def host_name():
    if sys.platform == "darwin":
        proc = subprocess.run(
            ["scutil", "--get", "ComputerName"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    return platform.node().split(".")[0] or "unknown"


def held_by_other(mark, session):
    if mark is None:
        return False
    return not (isinstance(mark, dict) and mark.get("session") == session)


def cmd_show(_args):
    mark, _ = read_deployment()
    print(describe(mark))
    return 0


def cmd_set(args):
    mark, version = read_deployment()
    if held_by_other(mark, args.session) and not args.force:
        print(f"refused, {describe(mark)}; pass --force to take it", file=sys.stderr)
        return EXIT_REFUSED
    since = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(mark, dict) and mark.get("session") == args.session:
        since = mark.get("since", since)  # a refresh keeps the start time
    value = {
        "item": args.item,
        "session": args.session,
        "host": host_name(),
        "since": since,
    }
    if args.note:
        value["note"] = args.note
    payload = json.dumps(value, separators=(",", ":"))
    kubectl(
        "annotate",
        "deployment",
        DEPLOYMENT,
        f"--resource-version={version}",
        "--overwrite",
        f"{ANNOTATION}={payload}",
    )
    print(describe(value))
    return 0


def cmd_clear(args):
    mark, version = read_deployment()
    if mark is None:
        print("in use: not marked (nothing to clear)")
        return 0
    if held_by_other(mark, args.session) and not args.force:
        print(f"refused, {describe(mark)}; pass --force to clear it", file=sys.stderr)
        return EXIT_REFUSED
    kubectl(
        "annotate",
        "deployment",
        DEPLOYMENT,
        f"--resource-version={version}",
        f"{ANNOTATION}-",
    )
    print("in use: cleared")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("show").set_defaults(run=cmd_show)
    session = os.environ.get("CLAUDE_CODE_SESSION_ID")
    for name, run in (("set", cmd_set), ("clear", cmd_clear)):
        p = sub.add_parser(name)
        p.add_argument("--session", default=session, required=session is None)
        p.add_argument("--force", action="store_true")
        p.set_defaults(run=run)
        if name == "set":
            p.add_argument("--item", required=True)
            p.add_argument("--note")
    args = parser.parse_args(argv)
    try:
        return args.run(args)
    except MarkerError as exc:
        print(f"ov-test-marker: {exc}", file=sys.stderr)
        return EXIT_KUBECTL


if __name__ == "__main__":
    sys.exit(main())
