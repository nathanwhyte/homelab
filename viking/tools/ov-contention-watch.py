#!/usr/bin/env python3
"""Soak watcher for OpenViking lock contention (BUG-1220).

Polls the viking namespace once a minute and reports lock contention as one line
per event. Read-only: kubectl get/logs/exec only. The exec runs a python3 one-liner
in the ov-exporter container that reads its own :9210/metrics; the openviking
container log is read with --timestamps and --since-time, and lines already seen
are skipped, so no line is counted twice.

Usage:
  ov-contention-watch.py [--hours H | --target-uptime DUR] [--log PATH] [--wake-wait-ms MS]

  --hours H          run for H hours (float, default 6). The old bare positional
                     hours argument is gone; a positional value is an argparse error.
  --target-uptime D  run until OV has been Ready for D in total (72h, 90m, 1h30m,
                     1.5h), summed across pod changes. Exclusive with --hours.
                     See "Target-uptime mode" below.
  --log PATH         append every event line to PATH; stdout then carries only the
                     actionable events. Without it, every event goes to stdout.
  --wake-wait-ms MS  with --log, a WAIT line whose max wait is at least MS goes to
                     stdout too (default 60000).

Events (each line starts with the UTC time, HH:MMZ):
  POD      the openviking pod changed. The line names the restart trigger from
           `kubectl get deploy openviking -o json`: rev= (the newest ReplicaSet
           revision), restartedAt= and last-restart= when those annotations exist,
           and managed= (manager, operation, time of the newest managedFields entry).
  HOLD     a live lease crossed 600/1800/2700 s (once per lease per threshold)
  WAIT     lock waits, session-lock timeouts or extraction outcomes seen in the OV
           log this poll
  BEAT     periodic summary (every BEAT_EVERY polls), so silence is not mistaken
           for health
  WATCH ERROR <probe>: <reason>   a probe failed; the run continues
  WATCH start / WATCH end         bracket the run; WATCH end is an actionable line

Target-uptime mode (BUG-1220 split-window rule: a planned restart splits the soak
into segments instead of resetting it). Accounting rule, from observations alone: a
poll is successful when the pods probe returned, and covers when a non-terminating
openviking pod is Ready; for each covering pod the poll records its Ready
lastTransitionTime exactly as the API prints it. The time between two consecutive
successful polls counts as uptime iff both cover and some pod is Ready in both with an
identical transition string, so a pod that stayed Ready bridges failed probes between
them. Otherwise the gap is not counted: a segment closes at the earlier poll, and the
next covering poll opens a new one. A segment starts at the poll that first observed
coverage and ends at its last covering poll. A start therefore omits the time between
the pod becoming Ready and that poll: at most one poll interval plus probe latency
when the previous probe succeeded (an outage whose probes kept succeeding omits no
more), and more only when probes had failed before it; Ready time before the watcher
started is omitted too. The closing SEGMENT line reports the time from its end to the
poll (or the close) that observed the end as unobserved=. Durations and the target
are measured on a clock that keeps counting while the host sleeps (CLOCK_MONOTONIC on
macOS, CLOCK_BOOTTIME on Linux, time.monotonic() otherwise), so a gap bridged across a
host sleep counts in full. Wall-clock UTC appears in printed stamps (start=/end= and
the HH:MMZ line prefix) and in the log cursor, which seeds its first --since-time from
it; it plays no part in durations or the target. The accounting compares API
timestamps only with each other, never with the local clock. Known limit: if a node controller marks a pod
NotReady and no successful poll sees it, and kubelet later restores the original
Ready lastTransitionTime, the identical string bridges that gap and it counts. Which pod the metrics
and log reads sample does not affect the sum. A pod handoff with a pod continuous
across it is not a boundary; it is a log-only `POD ready a -> b` line.
Events, all actionable:
  SEGMENT <i> pod=<a,b> start=<UTC> end=<UTC> dur=<d> cum=<d> remaining=<d> unobserved=<d>
          bridged=<d> (live=<n> oldest=<s>s | locks=unknown)[ trigger][ note]
           a segment closed: no covering pod at a poll (OV DOWN), the target was
           reached, the run was interrupted or failed, or two covering polls shared
           no pod with an identical transition (note: Ready lost between polls).
           pod= lists the pods that covered it. bridged= is the part of dur that came
           from counted gaps longer than 2 x POLL (120 s) between two consecutive
           successful polls: a host sleep or a long run of failed probes. A pod's
           unchanged Ready transition is the only evidence for that time, and
           nothing was checked during it. It is never more than dur. WATCH end
           carries the total as `bridged <d>`. live= counts the labelled
           openviking_s3_ovlock_live_age_seconds series at the last metrics poll in
           the segment (the exporter keeps the top 3 locks held >= 900 s, so it is
           not the lease count); oldest= is the exporter's oldest live lock age;
           locks=unknown if no metrics read with poll_ok=1 came in the segment (a
           bad read neither sets nor replaces it). trigger is the restart
           trigger of the POD description, when the deploy fetch worked.
  OV DOWN <reason>          the first successful poll with no covering pod
  OV BACK <pods> ready_since=<API string> down_for=<d>
                            the next successful poll with a covering pod;
                            ready_since is display only. Deployment readyReplicas is
                            not consulted.

POD is the --hours event; in target mode the pod change is the log-only `POD ready`
line, or the trigger on a SEGMENT line.

Probe failures: in target mode each `WATCH ERROR <probe>` line passes one per-probe
gate (probes: pods, metrics, logs, deploy). The first line of a streak goes to stdout
and the rest to the log. A streak ends only when that probe runs and reports success;
a poll in which it did not run leaves it unchanged. Pod-probe errors, and the deploy
fetch made at OV DOWN, are log-only while OV is down; other deploy errors go through
the gate like the rest. At the target the run prints a final SEGMENT and a WATCH end
line (segments, total uptime, total bridged) and exits 0. On an interrupt (Ctrl-C,
SIGTERM) or an error the open segment's SEGMENT and a WATCH end line are written,
marked `incomplete (interrupted)` or `incomplete (error: ...)`; an interrupt exits 130
and an error propagates. From the WATCH start line on, a single interrupt ends with a
WATCH end line marked incomplete and each closed segment counted once; the marking is
taken from the exception in flight, so it does not depend on one assignment
surviving, and a SEGMENT or WATCH end write that the interrupt cut is retried once.
The price is repeats: a SEGMENT or WATCH end line whose interrupt landed after its
write is written again, in the log and on stdout (a second wake), and then the first
WATCH end may lack the incomplete mark. An interrupt that lands while the final
target-reached SEGMENT is being written exits 130 and is marked incomplete; its
uptime figure shows whether the target had been met. What stays open: an interrupt
at a statement boundary between the finalizer's own protected blocks can still leave
no WATCH end (line-event injection finds eight such boundaries), and a second
interrupt inside the finalizer can leave the ledger incomplete.

A semantic-DAG "Failed to dispatch directory" ERROR carries the lock-timeout text
plus a LockAcquisitionError traceback line. Both count as dag_miss, never as a
session-lock timeout.

Actionable (stdout with --log): POD (--hours), HOLD, WATCH ERROR, a WAIT carrying a session-lock
timeout, a failed extraction outcome or "Memory extraction failed", or a max wait of
at least --wake-wait-ms. Routine WAIT and BEAT lines go only to the log.

Monitor recipe (a long soak that wakes the session only for actionable events):
  Monitor: ov-contention-watch.py --hours 72 --log /path/to/ov-contention.log
  Split-window soak: ov-contention-watch.py --target-uptime 72h --log /path/...
  At the end, read /path/to/ov-contention.log for the full event history.

Exporter caveat: the labelled openviking_s3_ovlock_live_age_seconds series only
appears once a lock has been held 15 min, so HOLD and the peak lease age see nothing
shorter. Short contention shows up only as WAIT lines from the log.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import signal
import subprocess
import sys
import time
from typing import NamedTuple

NS = "viking"
POLL = 60
# A counted gap longer than this many seconds is "bridged": nothing was checked in it.
BRIDGE_AFTER = 2 * POLL
BEAT_EVERY = 30
THRESHOLDS = (600, 1800, 2700)
DEFAULT_HOURS = 6.0
DEFAULT_WAKE_WAIT_MS = 60000

METRICS_PY = (
    "import urllib.request;"
    "print(urllib.request.urlopen('http://localhost:9210/metrics',timeout=20).read().decode())"
)
SAMPLE = re.compile(r"^([a-zA-Z_:][\w:]*)(\{[^}]*\})?\s+(\S+)$")
LABEL = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')
LOCK_PATH = re.compile(r'lock_paths=\["([^"]+)"')
WAIT_MS = re.compile(r"wait_ms=(\d+)")
# A wait shorter than this is ordinary contention: counted for BEAT, not its own event.
WAIT_EVENT_MS = 5000
# kubectl --timestamps: RFC3339 with nanoseconds and the zone the kubelet reports,
# which on this cluster is a local offset such as -05:00, not Z.
TIMESTAMP = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d+)?(Z|[+-]\d\d:\d\d)$")
# The first line of a log record (any of the OV log formats starts with a date);
# anything else after a dispatch error is its traceback.
RECORD_START = re.compile(r"^\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d")
# The traceback's final exception line (`[module.]LockAcquisitionError: …`), not
# a source frame such as `raise LockAcquisitionError(...)` that names it earlier.
DAG_TRACE_END = re.compile(r"^\s*(?:[\w.]+\.)?LockAcquisitionError(?::|$)")
OUTCOME = re.compile(r"ov-extract-outcome (\{.*\})")

# Target-uptime mode: `--target-uptime` accepts 72h, 90m, 1h30m, 1.5h, 45s.
DURATION = re.compile(r"(\d+(?:\.\d+)?)([hms])")
UNIT_SECONDS = {"h": 3600, "m": 60, "s": 1}
ISO_SECOND = "%Y-%m-%dT%H:%M:%SZ"

REVISION = "deployment.kubernetes.io/revision"
RESTARTED_AT = "kubectl.kubernetes.io/restartedAt"
LAST_RESTART_NOTE = "ov-lock-watchdog.nathanwhyte.dev/last-restart"


class Sink:
    """Routes event lines: all to the log file, actionable ones to stdout.

    Without a log path everything goes to stdout, as before --log existed.
    """

    def __init__(self, log_path=None):
        self.log_path = log_path

    def emit(self, line, wake=True):
        stamped = f"{dt.datetime.now(dt.UTC):%H:%MZ} {line}"
        if self.log_path:
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(stamped + "\n")
        if wake or not self.log_path:
            print(stamped, flush=True)

    def ok(self, probe):
        """A probe ran and succeeded; only StreakSink acts on it."""


class LogOnly:
    """A sink that sends everything to the log, for probes during an outage."""

    def __init__(self, sink):
        self.sink = sink

    def emit(self, line, wake=True):
        self.sink.emit(line, wake=False)

    def ok(self, probe):
        self.sink.ok(probe)


class StreakSink:
    """--target-uptime: one gate for every `WATCH ERROR <probe>` line.

    The first error line of a probe's streak keeps its wake flag; the rest of the
    streak go to the log only. A streak ends only when that probe reports `ok`,
    that is, runs and succeeds; a poll in which the probe did not run leaves it
    unchanged. The gate also collapses an error line's whitespace to single spaces,
    so only target mode changes the text. A line already sent log-only (`LogOnly`) neither opens nor extends a
    streak, so an error during an outage cannot hide the first one after it.
    """

    def __init__(self, sink):
        self.sink = sink
        self.streak = set()

    def emit(self, line, wake=True):
        if line.startswith("WATCH ERROR "):
            line = one_line(line)  # one error, one line, one wake
        if wake and line.startswith("WATCH ERROR "):
            probe = line.split()[2].rstrip(":")
            if probe in self.streak:
                wake = False
            self.streak.add(probe)
        self.sink.emit(line, wake=wake)

    def ok(self, probe):
        self.streak.discard(probe)
        self.sink.ok(probe)


def one_line(text):
    """Collapse whitespace and newlines: one error is one line, one wake."""
    return " ".join(str(text).split())


def run(sink, probe, args, timeout=60):
    try:
        p = subprocess.run(
            args, check=False, capture_output=True, text=True, timeout=timeout
        )
    except Exception as exc:  # noqa: BLE001 - every probe failure is reported
        sink.emit(f"WATCH ERROR {probe}: {exc}")
        return None
    if p.returncode != 0:
        sink.emit(f"WATCH ERROR {probe}: exit {p.returncode}: {p.stderr.strip()[:200]}")
        return None
    return p.stdout


def pod_name(sink):
    s = run(
        sink,
        "pod",
        [
            "kubectl",
            "-n",
            NS,
            "get",
            "pods",
            "-l",
            "app=openviking",
            "--field-selector=status.phase=Running",
            "-o",
            "jsonpath={.items[*].metadata.name}",
        ],
    )
    if s is None:
        return None
    names = s.split()
    if len(names) != 1:
        sink.emit(f"WATCH ERROR pod: expected one running openviking pod, got {names}")
        return None
    return names[0]


def restart_trigger(sink):
    """Name what restarted the pod, from the Deployment; "" if it cannot be read."""
    s = run(
        sink,
        "deploy",
        [
            "kubectl",
            "-n",
            NS,
            "get",
            "deploy",
            "openviking",
            "-o",
            "json",
            # kubectl strips managedFields from get output unless asked.
            "--show-managed-fields=true",
        ],
    )
    if s is None:
        return ""
    try:
        d = json.loads(s)
        meta = d.get("metadata") or {}
        ann = meta.get("annotations") or {}
        tmpl_ann = (
            ((d.get("spec") or {}).get("template") or {}).get("metadata") or {}
        ).get("annotations") or {}
        parts = [f"rev={ann.get(REVISION, '?')}"]
        for label, key in (
            ("restartedAt", RESTARTED_AT),
            ("last-restart", LAST_RESTART_NOTE),
        ):
            val = tmpl_ann.get(key) or ann.get(key)
            if val:
                parts.append(f"{label}={val}")
        fields = [f for f in meta.get("managedFields") or [] if f.get("time")]
        if fields:
            # Timestamps are fixed-width UTC ISO strings, so they sort as text.
            f = max(fields, key=lambda f: f["time"])
            parts.append(f"managed={f.get('manager')}/{f.get('operation')}@{f['time']}")
        sink.ok("deploy")
        return " " + " ".join(parts)
    except (ValueError, TypeError, AttributeError, KeyError) as exc:
        sink.emit(f"WATCH ERROR deploy: unparsable deployment: {exc}")
        return ""


def metrics(sink, pod):
    s = run(
        sink,
        "metrics",
        [
            "kubectl",
            "-n",
            NS,
            "exec",
            pod,
            "-c",
            "ov-exporter",
            "--",
            "python3",
            "-c",
            METRICS_PY,
        ],
    )
    if s is None:
        return None
    m, live = {}, []
    for line in s.splitlines():
        hit = SAMPLE.match(line)
        if not hit:
            continue
        name, labels, val = hit.groups()
        try:
            v = float(val)
        except ValueError:
            continue
        if name == "openviking_s3_ovlock_live_age_seconds":
            live.append((dict(LABEL.findall(labels or "")), v))
        elif name == "openviking_queue_pending" and labels:
            q = dict(LABEL.findall(labels)).get("queue")
            m[f"q.{q}"] = v
        else:
            m[name] = v
    if m.get("openviking_s3_ovlock_poll_ok") != 1:
        sink.emit(
            f"WATCH ERROR metrics: exporter poll_ok={m.get('openviking_s3_ovlock_poll_ok')}"
        )
    else:
        sink.ok("metrics")
    return m, live


def short(uri):
    return re.sub(r"^.*?/user/", "user/", uri)


def utc_second(stamp):
    """A kubectl timestamp as a UTC `%Y-%m-%dT%H:%M:%SZ` second; None if it is not one."""
    hit = TIMESTAMP.match(stamp)
    if not hit:
        return None
    base, zone = hit.groups()
    t = dt.datetime.strptime(base, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.UTC)
    if zone != "Z":
        sign = 1 if zone[0] == "+" else -1
        t -= sign * dt.timedelta(hours=int(zone[1:3]), minutes=int(zone[4:6]))
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


class LogCursor:
    """Where the next log read starts, so no line is counted twice.

    --since-time has one-second resolution and the read happens after the cursor
    was taken, so a bare "since the last poll" replays lines. Each line is read
    with its kubectl timestamp; the cursor is the second of the newest one, and
    lines already seen within that second are skipped. A dag-miss traceback that
    lands in the next read is still matched through `dag_open`.
    """

    def __init__(self):
        self.since = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.seen = set()
        self.dag_open = False

    def reset_pod(self):
        self.seen = set()
        self.dag_open = False

    def fresh(self, text):
        """The lines of `text` not read before; advances the cursor."""
        lines = []
        newest = self.since
        for raw in text.splitlines():
            stamp, _, rest = raw.partition(" ")
            second = utc_second(stamp)
            if second is None:
                lines.append(raw)
                continue
            if raw in self.seen:
                continue
            self.seen.add(raw)
            lines.append(rest)
            newest = max(newest, second)
        self.since = newest
        self.seen = {x for x in self.seen if utc_second(x.partition(" ")[0]) >= newest}
        return lines


def log_events(sink, pod, cursor):
    s = run(
        sink,
        "logs",
        [
            "kubectl",
            "-n",
            NS,
            "logs",
            pod,
            "-c",
            "openviking",
            "--timestamps",
            f"--since-time={cursor.since}",
        ],
        timeout=90,
    )
    if s is None:
        return None
    waits, timeouts, outcomes, failed = {}, 0, {}, 0
    # Semantic-DAG directory dispatch gives up on a lock within ms; it logs the
    # ERROR line (carrying the timeout text) plus one traceback line. Counted
    # apart so they cannot hide a session-lock timeout.
    # While a dispatch error's traceback is open, its header, frames and final
    # LockAcquisitionError line are all part of the dag miss, wherever the poll
    # boundary falls; a new log record closes it.
    dag_miss = 0
    clean = True
    for line in cursor.fresh(s):
        if "Failed to dispatch directory" in line:
            dag_miss += 1
            cursor.dag_open = True
            continue
        if cursor.dag_open:
            if RECORD_START.match(line):
                cursor.dag_open = False
            else:
                if DAG_TRACE_END.match(line):
                    cursor.dag_open = False
                continue
        if "manager.rs:988" in line:
            p = LOCK_PATH.search(line)
            w = WAIT_MS.search(line)
            if not p or not w:
                sink.emit(f"WATCH ERROR logs: unparsed wait line: {line[:160]}")
                clean = False
                continue
            k = short(p.group(1))
            cnt, mx = waits.get(k, (0, 0))
            waits[k] = (cnt + 1, max(mx, int(w.group(1))))
        elif "lock acquire timed out" in line or "LockAcquisitionError" in line:
            timeouts += 1
        if "Memory extraction failed" in line:
            failed += 1
        o = OUTCOME.search(line)
        if o:
            try:
                d = json.loads(o.group(1))
            except ValueError:
                continue
            k = f"{d.get('event')}:{d.get('failure_class', '-')}"
            outcomes[k] = outcomes.get(k, 0) + 1
    if clean:
        sink.ok("logs")
    return waits, timeouts, dag_miss, outcomes, failed


def wait_is_actionable(poll_max, timeouts, outcomes, failed, wake_wait_ms):
    return bool(
        poll_max >= wake_wait_ms
        or timeouts
        or failed
        or any(k.startswith("failed:") for k in outcomes)
    )


def parse_duration(text):
    """Seconds in `72h`, `90m`, `1h30m`, `1.5h`: h, m, s at most once each, in that
    order, positive. Raises ValueError for text of any other form."""
    parts = DURATION.findall(text)
    if not parts or "".join(n + u for n, u in parts) != text:
        raise ValueError(f"not a duration: {text!r}")
    units = [u for _, u in parts]
    if units != sorted(set(units), key="hms".index):
        raise ValueError(f"units must be h, m, s once each, in order: {text!r}")
    secs = sum(float(n) * UNIT_SECONDS[u] for n, u in parts)
    if secs <= 0:
        raise ValueError(f"duration must be positive: {text!r}")
    return secs


def fmt_dur(seconds):
    s = round(seconds)
    return f"{s // 3600}h{s % 3600 // 60:02d}m{s % 60:02d}s"


def iso(t):
    return t.strftime(ISO_SECOND)


def mono():
    """Seconds on a clock that keeps counting while the host sleeps.

    time.monotonic() on macOS is mach_absolute_time(), which stops during sleep, so
    macOS uses CLOCK_MONOTONIC (continuous there) and Linux CLOCK_BOOTTIME when the
    time module has it; anything else falls back to time.monotonic().
    """
    if sys.platform == "darwin" and hasattr(time, "CLOCK_MONOTONIC"):
        return time.clock_gettime(time.CLOCK_MONOTONIC)
    if hasattr(time, "CLOCK_BOOTTIME"):
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    return time.monotonic()


class Instant(NamedTuple):
    """A poll time: `wall` (UTC, printed as start=/end=) and `mono` (seconds on the
    sleep-counting clock of `mono()`, the only thing durations and the target are
    measured on)."""

    wall: dt.datetime
    mono: float


def instant():
    return Instant(dt.datetime.fromtimestamp(time.time(), dt.UTC), mono())


def ready_state(sink):
    """What the openviking pods say, or None for a failed probe.

    ("ready", [(name, transition), ...]) lists the Ready, non-terminating pods with
    their Ready lastTransitionTime exactly as the API prints it; ("down", reason)
    means no non-terminating pod is Ready (a Ready pod being deleted counts as down).
    """
    s = run(
        sink,
        "pods",
        ["kubectl", "-n", NS, "get", "pods", "-l", "app=openviking", "-o", "json"],
    )
    if s is None:
        return None
    try:
        items = json.loads(s).get("items") or []
    except (ValueError, AttributeError) as exc:
        sink.emit(f"WATCH ERROR pods: unparsable pod list: {exc}")
        return None
    sink.ok("pods")
    ready, why = [], []
    for p in items:
        name = (p.get("metadata") or {}).get("name", "?")
        status = p.get("status") or {}
        cond = next(
            (c for c in status.get("conditions") or [] if c.get("type") == "Ready"),
            {},
        )
        if (p.get("metadata") or {}).get("deletionTimestamp"):
            why.append(f"{name} terminating")
        elif status.get("phase") == "Running" and cond.get("status") == "True":
            ready.append((name, cond.get("lastTransitionTime") or ""))
        else:
            detail = cond.get("reason") or status.get("reason") or ""
            why.append(
                f"{name} {status.get('phase', '?')} Ready={cond.get('status', '?')}"
                + (f" ({detail})" if detail else "")
            )
    if ready:
        return ("ready", ready)
    return ("down", "; ".join(why[:3]) or "no openviking pods")


class Ledger(NamedTuple):
    """The tracker's accounting state. It is replaced with one attribute store, so an
    interrupt between statements cannot leave the ledger itself half-updated."""

    closed: float = 0.0  # seconds in closed segments
    segments: int = 0
    start: Instant | None = None  # the open segment: first covering poll
    last: Instant | None = None  # ... and its latest covering poll
    pods: tuple = ()  # pods that covered it
    locks: tuple | None = None  # (live series, oldest age) at the last metrics poll
    pending: str | None = None  # a closed segment's line not yet known to be written
    bridged: float = 0.0  # seconds of bridged gaps in closed segments
    seg_bridged: float = 0.0  # ... and in the open segment


class ReadyTracker:
    """Sums OV Ready uptime for --target-uptime from observations alone.

    A poll is successful when the pods probe returned, and covers when a non-
    terminating pod is Ready. The time between two consecutive successful polls
    counts iff both cover and some pod is Ready in both with an identical Ready
    lastTransitionTime string, so failed probes between them are bridged. Otherwise
    the gap is not counted: a segment closes at the earlier poll, and a new one
    starts at the next covering poll. The accounting compares API timestamps only
    with each other. `now` is an Instant: durations and the target use its monotonic
    reading, and its wall reading is only printed as start=/end=. `locks` is set only
    from an exporter read with poll_ok=1.
    """

    def __init__(self, target, sink):
        self.target = target
        self.sink = sink
        self.ledger = Ledger()
        self.prev = {}  # pod -> transition string at the last successful poll
        self.sample = None
        self.down = False
        self.down_since = None

    closed = property(lambda self: self.ledger.closed)
    bridged = property(lambda self: self.ledger.bridged)
    segments = property(lambda self: self.ledger.segments)
    start = property(lambda self: self.ledger.start)
    last_ready = property(lambda self: self.ledger.last)

    @property
    def locks(self):
        return self.ledger.locks

    @locks.setter
    def locks(self, value):
        self.ledger = self.ledger._replace(locks=value)

    def total(self):
        lg = self.ledger
        open_secs = lg.last.mono - lg.start.mono if lg.start else 0
        return lg.closed + open_secs

    def reached(self):
        return self.total() >= self.target

    def open(self, cover, now):
        self.ledger = self.ledger._replace(
            start=now, last=now, pods=tuple(sorted(cover)), locks=None
        )

    def close(self, now, note=""):
        # Build the line, then replace the ledger (segment closed, line pending) in
        # one store before any output, which an interrupt can cut short: the
        # finalizer then sees the segment closed once and writes the line itself.
        lg = self.ledger
        secs = lg.last.mono - lg.start.mono
        cum = lg.closed + secs
        held = (
            f"live={lg.locks[0]} oldest={lg.locks[1]:.0f}s"
            if lg.locks
            else "locks=unknown"
        )
        line = (
            f"SEGMENT {lg.segments + 1} pod={','.join(lg.pods)} "
            f"start={iso(lg.start.wall)} end={iso(lg.last.wall)} "
            f"dur={fmt_dur(secs)} cum={fmt_dur(cum)} "
            f"remaining={fmt_dur(max(0, self.target - cum))} "
            f"unobserved={fmt_dur(max(0, now.mono - lg.last.mono))} "
            f"bridged={fmt_dur(lg.seg_bridged)} "
            f"{held}{note}"
        )
        self.ledger = lg._replace(
            closed=cum,
            segments=lg.segments + 1,
            start=None,
            last=None,
            pods=(),
            locks=None,
            pending=line,
            bridged=lg.bridged + lg.seg_bridged,
            seg_bridged=0.0,
        )
        self.flush()

    def flush(self):
        """Write a closed segment's line if it is not yet known to be written.

        An interrupt between the write and the clearing writes the line again, in the
        log and on stdout (a second wake); the count and the uptime are never
        repeated.
        """
        line = self.ledger.pending
        if line:
            self.sink.emit(line)
            self.ledger = self.ledger._replace(pending=None)

    def poll(self, now):
        """Probe once; return the Ready pod to sample metrics and logs from, or None."""
        st = ready_state(LogOnly(self.sink) if self.down else self.sink)
        if st is None:
            return None
        if st[0] == "down":
            self.prev = {}
            if not self.down:
                self.down = True
                self.down_since = now
                self.sink.emit(f"OV DOWN {st[1]}")
                if self.start:
                    # OV is down, so a failing deploy fetch here is log-only.
                    self.close(now, restart_trigger(LogOnly(self.sink)))
                self.sample = None
            return None
        cover = dict(st[1])
        if self.down:
            self.down = False
            self.open(cover, now)
            self.sink.emit(
                f"OV BACK {','.join(sorted(cover))} "
                f"ready_since={min(cover.values())} "
                f"down_for={fmt_dur(now.mono - self.down_since.mono)}"
            )
        elif not self.start:
            self.open(cover, now)
        elif any(self.prev.get(n) == t for n, t in cover.items()):
            lg = self.ledger
            pods = tuple(sorted(set(lg.pods) | set(cover)))
            if set(cover) != set(self.prev):
                self.sink.emit(
                    f"POD ready {','.join(sorted(self.prev))} -> "
                    f"{','.join(sorted(cover))}" + restart_trigger(self.sink),
                    wake=False,
                )
            gap = now.mono - lg.last.mono
            self.ledger = lg._replace(
                last=now,
                pods=pods,
                seg_bridged=lg.seg_bridged + (gap if gap > BRIDGE_AFTER else 0.0),
            )
        else:
            self.close(now, restart_trigger(self.sink) + " (Ready lost between polls)")
            self.open(cover, now)
        self.prev = cover
        if self.sample not in cover:
            self.sample = min(cover)
        return self.sample

    def finish(self, now, note=""):
        self.flush()
        if self.start:
            self.close(now, note)


def watch(hours, sink, wake_wait_ms=DEFAULT_WAKE_WAIT_MS, target_uptime=None):
    """Poll until `hours` pass, or, with target_uptime (seconds), until OV's summed
    Ready uptime reaches it."""
    end = None if target_uptime else time.time() + hours * 3600
    if target_uptime:
        sink = StreakSink(sink)
    tracker = ReadyTracker(target_uptime, sink) if target_uptime else None
    seen_pod, crossed = None, set()
    cursor = LogCursor()
    peak, tot_waits, max_wait, tot_outcomes, n = 0.0, 0, 0, {}, 0
    tot_dag_miss = 0
    window = f"target uptime {fmt_dur(target_uptime)}" if tracker else f"{hours:g} h"
    done = False
    status = ""

    def end_line(status):
        uptime = ""
        if tracker:
            uptime = (
                f"{status + ', ' if status else ''}{tracker.segments} segments, "
                f"uptime {fmt_dur(tracker.closed)} of {fmt_dur(target_uptime)}, "
                f"bridged {fmt_dur(tracker.bridged)}, "
            )
        return (
            f"WATCH end: {uptime}peak lease {peak:.0f}s, waits {tot_waits} "
            f"(max {max_wait / 1000:.1f}s), dag_miss {tot_dag_miss}, "
            f"outcomes {tot_outcomes or '{}'}"
        )

    try:
        sink.emit(
            f"WATCH start: {window}, poll {POLL}s, hold thresholds {THRESHOLDS}, "
            f"wait event >= {WAIT_EVENT_MS} ms, wake wait >= {wake_wait_ms} ms",
            wake=False,
        )
        while tracker or time.time() < end:
            n += 1
            got = None
            if tracker:
                pod = tracker.poll(instant())
                # The poll that meets the target still reads its metrics and logs.
                done = tracker.reached()
            else:
                pod = pod_name(sink)
            if pod and pod != seen_pod:
                if seen_pod and not tracker:
                    sink.emit(
                        f"POD changed {seen_pod} -> {pod} (all leases released)"
                        + restart_trigger(sink)
                    )
                seen_pod = pod
                cursor.reset_pod()
            if pod:
                got = metrics(sink, pod)
                if got:
                    m, live = got
                    # An exporter read with poll_ok != 1 is an error, not lock state;
                    # HOLD and BEAT still see it, as in --hours mode.
                    if tracker and m.get("openviking_s3_ovlock_poll_ok") == 1:
                        tracker.locks = (
                            len(live),
                            m.get("openviking_s3_ovlock_oldest_live_age_seconds", 0),
                        )
                    # A lease is its path plus its owner: a replacement holder, or a
                    # lock that was released and taken again, alerts afresh.
                    leases = {
                        (labels.get("key") or labels.get("uri"), labels.get("owner"))
                        for labels, _ in live
                    }
                    crossed = {c for c in crossed if c[0] in leases}
                    for labels, age in live:
                        peak = max(peak, age)
                        key = labels.get("key") or labels.get("uri")
                        lease = (key, labels.get("owner"))
                        for t in THRESHOLDS:
                            if age >= t and (lease, t) not in crossed:
                                crossed.add((lease, t))
                                sink.emit(
                                    f"HOLD >= {t}s: age={age:.0f}s type={labels.get('lock_type')} "
                                    f"owner={labels.get('owner', '')[:8]} uri={short(labels.get('uri', key))}"
                                )
                ev = log_events(sink, pod, cursor)
                if ev is not None:
                    waits, timeouts, dag_miss, outcomes, failed = ev
                    tot_dag_miss += dag_miss
                    n_waits = sum(c for c, _ in waits.values())
                    poll_max = max((mx for _, mx in waits.values()), default=0)
                    tot_waits += n_waits
                    max_wait = max(max_wait, poll_max)
                    for k, v in outcomes.items():
                        tot_outcomes[k] = tot_outcomes.get(k, 0) + v
                    actionable = wait_is_actionable(
                        poll_max, timeouts, outcomes, failed, wake_wait_ms
                    )
                    if (
                        poll_max >= WAIT_EVENT_MS
                        or timeouts
                        or outcomes
                        or failed
                        or actionable
                    ):
                        top = ", ".join(
                            f"{k} x{c} max={mx / 1000:.0f}s"
                            for k, (c, mx) in sorted(
                                waits.items(), key=lambda kv: -kv[1][1]
                            )[:3]
                        )
                        sink.emit(
                            f"WAIT waits={n_waits} max={poll_max / 1000:.1f}s timeouts={timeouts} "
                            f"dag_miss={dag_miss} extract_failed={failed} "
                            f"outcomes={outcomes or '{}'}"
                            + (f" top: {top}" if top else ""),
                            wake=actionable,
                        )
                if n % BEAT_EVERY == 1 and got:
                    m, live = got
                    sink.emit(
                        f"BEAT live={len(live)} oldest={m.get('openviking_s3_ovlock_oldest_live_age_seconds', 0):.0f}s "
                        f"peak={peak:.0f}s waits_total={tot_waits} max_wait={max_wait / 1000:.1f}s "
                        f"dag_miss={tot_dag_miss} outcomes_total={tot_outcomes or '{}'} "
                        f"queue sem={m.get('q.semantic', 0):.0f} nodes={m.get('q.semantic_nodes', 0):.0f} "
                        f"commit={m.get('q.sessioncommit', 0):.0f} total={m.get('q.total', 0):.0f}",
                        wake=False,
                    )
            if done:
                break
            time.sleep(POLL)
        if tracker:
            # Inside the try: an interrupt during this write is an incomplete run
            # (exit 130), even though the target was met.
            tracker.finish(instant(), " target reached")
    except BaseException as exc:  # SystemExit too, so the ledger is written
        status = outcome(exc)
        raise
    finally:
        # --target-uptime ends with a WATCH end line; a single interrupt anywhere from
        # the WATCH start line on does not stop it, and the line is retried once if
        # the first write is cut. --hours keeps its old shape (no line after an
        # interrupt or error). The target-reached SEGMENT is written in the try body.
        try:
            current = sys.exception()
            if current is not None and not status:
                status = outcome(current)
            if tracker and tracker.start:
                tracker.finish(instant(), f" {status}")
        except BaseException as exc:
            status = status or outcome(exc)
            raise
        finally:
            failure = None
            try:
                if tracker:
                    tracker.flush()  # one retry of a line cut off above
            except BaseException as exc:  # noqa: BLE001 - re-raised below
                status = status or outcome(exc)
                failure = exc
            if tracker or not status:
                for _attempt in range(2 if tracker else 1):
                    try:
                        sink.emit(end_line(status))
                        break
                    except BaseException as exc:  # noqa: BLE001 - re-raised below
                        status = status or outcome(exc)
                        failure = failure or exc
            if failure is not None:
                raise failure


def outcome(exc):
    if isinstance(exc, KeyboardInterrupt):
        return "incomplete (interrupted)"
    return f"incomplete (error: {str(exc) or type(exc).__name__})"


def duration_arg(text):
    try:
        return parse_duration(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def on_sigterm(signum, frame):
    raise KeyboardInterrupt


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--hours", type=float)
    ap.add_argument(
        "--target-uptime",
        type=duration_arg,
        help="run until OV's summed Ready uptime reaches this (72h, 90m, 1h30m)",
    )
    ap.add_argument(
        "--log", help="append every event line here; stdout gets actionable ones"
    )
    ap.add_argument(
        "--wake-wait-ms",
        type=int,
        default=DEFAULT_WAKE_WAIT_MS,
        help="with --log, WAIT lines with max wait at least this go to stdout",
    )
    args = ap.parse_args(argv)
    if args.target_uptime and args.hours is not None:
        ap.error("--hours and --target-uptime are mutually exclusive")
    if args.target_uptime:
        # SIGTERM (a stopped Monitor) takes the same path as Ctrl-C.
        signal.signal(signal.SIGTERM, on_sigterm)
        try:
            watch(
                None,
                Sink(args.log),
                args.wake_wait_ms,
                target_uptime=args.target_uptime,
            )
        except KeyboardInterrupt:
            return 130
    else:
        hours = DEFAULT_HOURS if args.hours is None else args.hours
        watch(hours, Sink(args.log), args.wake_wait_ms)
    return 0


if __name__ == "__main__":
    sys.exit(main())
