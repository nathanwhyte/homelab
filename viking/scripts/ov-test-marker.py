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

Exit 0: done (or, for show, read). Exit 2: kubectl failed, a concurrent
change, or any other failure. Exit 3: refused, the mark belongs to another
session. The exit code reports the cluster action alone: a closed stdout or
stderr loses that stream's text, noted on stderr when stderr still works.
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


LOST = set()  # names of the streams whose reader has gone


def open_streams():
    """Give a descriptor closed before start (sys.stdout or sys.stderr None) a
    /dev/null stream, so that nothing, argparse included, falls back from a
    missing stderr to stdout."""
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            LOST.add(name)
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
            except OSError:
                pass


def emit(text, stream="stdout"):
    """Print a line and flush it now; a closed or broken stream loses the
    text and nothing else."""
    if stream in LOST:
        return
    out = getattr(sys, stream, None)
    if out is None:  # open_streams() did not run (imported) or could not open
        LOST.add(stream)
        return
    try:
        print(text, file=out)
        out.flush()
    except (OSError, ValueError):
        LOST.add(stream)


def finish(code):
    """Exit with `code` and nothing else. Both streams are flushed here, where
    a failure loses only their text, and os._exit skips the interpreter's
    flush at exit, the one place a stream could still turn the code into 120."""
    for name in ("stdout", "stderr"):
        try:
            getattr(sys, name).flush()  # None (AttributeError) is caught too
        except Exception:  # noqa: BLE001
            pass
    os._exit(code)


def kubectl(*args):
    try:
        proc = subprocess.run(
            ["kubectl", "-n", NAMESPACE, f"--request-timeout={REQUEST_TIMEOUT}", *args],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:  # kubectl missing or not executable
        raise MarkerError(f"kubectl {args[0]}: {exc.strerror or exc}") from None
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
    """The mark as a dict, or the raw text when it is anything else. Total: any
    failure, a RecursionError on deep JSON included, reads as unreadable, so
    `clear --force` can always remove it."""
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except Exception:  # noqa: BLE001
        return str(raw)
    return value if isinstance(value, dict) else str(raw)


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


def printable(text):
    """Text safe to print as one line on any stdout: anyone with kubectl can
    write the annotation, so a newline, control character or lone surrogate in
    it is escaped, and so is anything the stdout encoding cannot carry."""
    text = "".join(c if c.isprintable() else ascii(c)[1:-1] for c in text)
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        return text.encode(encoding, "backslashreplace").decode(encoding)
    except Exception:  # noqa: BLE001 — unknown, or raises whatever the handler (undefined, idna)
        return text.encode("ascii", "backslashreplace").decode("ascii")


def describe(mark, now=None):
    """One printable line for a mark, as `show` and ov-test-drift.py print it."""
    return printable(_describe(mark, now))


def _describe(mark, now):
    if mark is None:
        return "in use: not marked"
    if isinstance(mark, str):
        return f"in use: unreadable mark {mark[:120]!r}"
    now = now or dt.datetime.now(dt.timezone.utc)
    parts = [
        str(mark.get("item", "?")),
        f"session {mark.get('session', '?')}",
        f"host {mark.get('host', '?')}",
        f"since {local_time(mark.get('since', '?'))} ({age(mark.get('since'), now)})",
    ]
    if mark.get("note"):
        parts.append(f"note {mark['note']!r}")
    return "in use: " + ", ".join(parts)


def local_time(since):
    """The stored UTC start as local time with its zone, for a person to read,
    or the stored text when it is not an ISO timestamp with an offset."""
    try:
        start = dt.datetime.fromisoformat(since.replace("Z", "+00:00"))
        if start.tzinfo is None:
            return str(since)
        return start.astimezone().strftime("%Y-%m-%d %H:%M %Z")
    except (AttributeError, TypeError, ValueError, OverflowError, OSError):
        return str(since)


def host_name():
    if sys.platform == "darwin":
        try:
            proc = subprocess.run(
                ["scutil", "--get", "ComputerName"],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            proc = None
        if proc and proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    return platform.node().split(".")[0] or "unknown"


def held_by_other(mark, session):
    if mark is None:
        return False
    return not (isinstance(mark, dict) and mark.get("session") == session)


def cmd_show(_args):
    mark, _ = read_deployment()
    emit(describe(mark))
    return 0


def cmd_set(args):
    mark, version = read_deployment()
    if held_by_other(mark, args.session) and not args.force:
        emit(f"refused, {describe(mark)}; pass --force to take it", "stderr")
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
    emit(describe(value))
    return 0


def cmd_clear(args):
    mark, version = read_deployment()
    if mark is None:
        emit("in use: not marked (nothing to clear)")
        return 0
    if held_by_other(mark, args.session) and not args.force:
        emit(f"refused, {describe(mark)}; pass --force to clear it", "stderr")
        return EXIT_REFUSED
    kubectl(
        "annotate",
        "deployment",
        DEPLOYMENT,
        f"--resource-version={version}",
        f"{ANNOTATION}-",
    )
    emit("in use: cleared")
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
    # The exit codes are the contract, so nothing may escape as a traceback's 1.
    try:
        code = args.run(args)
    except MarkerError as exc:
        code, message = EXIT_KUBECTL, f"ov-test-marker: {exc}"
    except Exception as exc:  # noqa: BLE001
        code = EXIT_KUBECTL
        message = f"ov-test-marker: failed ({type(exc).__name__}: {str(exc)[:200]})"
    else:
        message = None
    if message is not None:
        emit(printable(message), "stderr")
    if "stdout" in LOST:
        emit("ov-test-marker: stdout closed, output lost", "stderr")
    return code


if __name__ == "__main__":
    open_streams()
    try:
        exit_code = main()
    except SystemExit as exc:  # argparse: 0 for --help, 2 for a usage error
        exit_code = 0 if exc.code in (None, 0) else EXIT_KUBECTL
    except Exception:  # noqa: BLE001 — main() maps failures itself; a backstop
        exit_code = EXIT_KUBECTL
    finish(exit_code)
