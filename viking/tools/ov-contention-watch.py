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
  WATCH start / WATCH end         bracket the run; WATCH end always reaches stdout

Target-uptime mode (BUG-1220 split-window rule: a planned restart splits the soak
into segments instead of resetting it). Ready uptime is the time a Ready, non-
terminating openviking pod is serving; each segment starts at that pod's Ready
lastTransitionTime (never before the watcher started or the previous segment's end)
and ends at the last poll that saw it Ready. Events, all actionable:
  SEGMENT <i> pod=<name> start=<UTC> end=<UTC> dur=<d> cum=<d> remaining=<d>
          live=<n> oldest=<s>s [rev=...]
           a Ready interval closed (pod change, OV stopped, or target reached);
           live/oldest are the lease count and oldest age at the last poll before
           the stop (locks=unknown if no metrics poll succeeded)
  OV DOWN <reason>          OV stopped being Ready; one line per outage
  OV BACK <pod> ...         a fresh Ready transition: a new pod name that is Ready,
                            or the same pod whose Ready lastTransitionTime is after
                            the last poll that saw it Ready. Deployment readyReplicas
                            is never consulted (it can be stale).
Probe failures during an outage go to the log only. While OV is up, the first failed
probe of a streak is a WATCH ERROR on stdout and the rest of the streak go to the
log; probe failures do not end a segment. At the target the run prints a final
SEGMENT and a WATCH end line (segments, total uptime) and exits 0.

A semantic-DAG "Failed to dispatch directory" ERROR carries the lock-timeout text
plus a LockAcquisitionError traceback line. Both count as dag_miss, never as a
session-lock timeout.

Actionable (stdout with --log): POD, HOLD, WATCH ERROR, a WAIT carrying a session-lock
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
import subprocess
import sys
import time

NS = "viking"
POLL = 60
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


class LogOnly:
    """A sink that sends everything to the log, for probes during an outage."""

    def __init__(self, sink):
        self.sink = sink

    def emit(self, line, wake=True):
        self.sink.emit(line, wake=False)


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
    """Name what restarted the pod, from the Deployment. Never raises."""
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
    return waits, timeouts, dag_miss, outcomes, failed


def wait_is_actionable(poll_max, timeouts, outcomes, failed, wake_wait_ms):
    return bool(
        poll_max >= wake_wait_ms
        or timeouts
        or failed
        or any(k.startswith("failed:") for k in outcomes)
    )


def parse_duration(text):
    """Seconds in `72h`, `90m`, `1h30m`; raises ValueError for anything else."""
    parts = DURATION.findall(text)
    if not parts or "".join(n + u for n, u in parts) != text:
        raise ValueError(f"not a duration: {text!r}")
    secs = sum(float(n) * UNIT_SECONDS[u] for n, u in parts)
    if secs <= 0:
        raise ValueError(f"duration must be positive: {text!r}")
    return secs


def fmt_dur(seconds):
    s = round(seconds)
    return f"{s // 3600}h{s % 3600 // 60:02d}m{s % 60:02d}s"


def iso(t):
    return t.strftime(ISO_SECOND)


def now_utc():
    return dt.datetime.fromtimestamp(time.time(), dt.UTC)


def parse_ts(stamp):
    """A kubectl `...Z` timestamp as an aware UTC datetime; None if it is not one."""
    second = utc_second(stamp or "")
    if second is None:
        return None
    return dt.datetime.strptime(second, ISO_SECOND).replace(tzinfo=dt.UTC)


def ready_state(sink):
    """What the openviking pods say: ("ready", name, since), ("down", reason) or None.

    Ready means phase Running, Ready condition True and no deletionTimestamp; `since`
    is that condition's lastTransitionTime. None is a failed probe (already reported
    through `sink`).
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
            ready.append((name, parse_ts(cond.get("lastTransitionTime"))))
        else:
            detail = cond.get("reason") or status.get("reason") or ""
            why.append(
                f"{name} {status.get('phase', '?')} Ready={cond.get('status', '?')}"
                + (f" ({detail})" if detail else "")
            )
    if len(ready) > 1:
        sink.emit(
            f"WATCH ERROR pods: expected one Ready openviking pod, got {[n for n, _ in ready]}"
        )
        return None
    if ready:
        return ("ready", *ready[0])
    return ("down", "; ".join(why[:3]) or "no openviking pods")


class ReadyTracker:
    """Sums Ready uptime across pods for --target-uptime and reports the edges.

    Time is the injected wall clock (`now` arguments), so a replay is a plain sum of
    segment end minus start.
    """

    def __init__(self, target, sink, started):
        self.target = target
        self.sink = sink
        self.floor = started  # a segment never starts before this
        self.closed = 0.0
        self.segments = 0
        self.pod = self.start = self.last_ready = None
        self.down = False
        self.down_pod = self.down_cutoff = self.down_since = None
        self.fail_streak = 0
        self.locks = None  # (live leases, oldest age) at the last metrics poll

    def total(self):
        open_secs = (self.last_ready - self.start).total_seconds() if self.pod else 0
        return self.closed + open_secs

    def reached(self):
        return self.total() >= self.target

    def open(self, pod, since, now):
        self.pod = pod
        self.start = max(since or now, self.floor)
        self.last_ready = max(now, self.start)
        self.locks = None

    def close(self, note=""):
        end = self.last_ready
        secs = (end - self.start).total_seconds()
        self.closed += secs
        self.segments += 1
        cum = self.closed
        locks = (
            f"live={self.locks[0]} oldest={self.locks[1]:.0f}s"
            if self.locks
            else "locks=unknown"
        )
        self.sink.emit(
            f"SEGMENT {self.segments} pod={self.pod} "
            f"start={iso(self.start)} end={iso(end)} "
            f"dur={fmt_dur(secs)} cum={fmt_dur(cum)} "
            f"remaining={fmt_dur(max(0, self.target - cum))} {locks}{note}"
        )
        self.floor = end
        self.pod = self.start = self.last_ready = None

    def poll(self, now):
        """Probe once; return the Ready pod to inspect, or None."""
        quiet = self.down or self.fail_streak > 0
        st = ready_state(LogOnly(self.sink) if quiet else self.sink)
        if st is None:
            self.fail_streak += 1
            return None
        self.fail_streak = 0
        if st[0] == "down":
            if not self.down:
                self.down = True
                self.down_pod = self.pod
                self.down_cutoff = self.last_ready or self.floor
                self.down_since = now
                self.sink.emit(f"OV DOWN {st[1]}")
                if self.pod:
                    self.close(restart_trigger(LogOnly(self.sink)))
            return None
        _, name, since = st
        if self.down:
            # Only a Ready transition after the stop counts; a Ready condition that
            # predates it (or a stale readyReplicas) is not a recovery.
            if (
                name == self.down_pod
                and since is not None
                and since <= self.down_cutoff
            ):
                return None
            self.down = False
            self.open(name, since, now)
            self.sink.emit(
                f"OV BACK {name} ready_since={iso(self.start)} "
                f"down_for={fmt_dur((now - self.down_since).total_seconds())}"
            )
            return name
        if self.pod is None:
            self.open(name, since, now)
        elif name != self.pod:
            self.close(restart_trigger(self.sink))
            self.open(name, since, now)
        self.last_ready = max(now, self.start)
        return name

    def finish(self):
        if self.pod:
            self.close(" target reached" if self.reached() else "")


def watch(hours, sink, wake_wait_ms=DEFAULT_WAKE_WAIT_MS, target_uptime=None):
    """Poll until `hours` pass, or, with target_uptime (seconds), until OV's summed
    Ready uptime reaches it."""
    end = None if target_uptime else time.time() + hours * 3600
    tracker = ReadyTracker(target_uptime, sink, now_utc()) if target_uptime else None
    seen_pod, crossed = None, set()
    cursor = LogCursor()
    peak, tot_waits, max_wait, tot_outcomes, n = 0.0, 0, 0, {}, 0
    tot_dag_miss = 0
    window = f"target uptime {fmt_dur(target_uptime)}" if tracker else f"{hours:g} h"
    sink.emit(
        f"WATCH start: {window}, poll {POLL}s, hold thresholds {THRESHOLDS}, "
        f"wait event >= {WAIT_EVENT_MS} ms, wake wait >= {wake_wait_ms} ms",
        wake=False,
    )
    while tracker or time.time() < end:
        n += 1
        got = None
        if tracker:
            pod = tracker.poll(now_utc())
            if tracker.reached():
                break
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
                if tracker:
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
        time.sleep(POLL)
    uptime = ""
    if tracker:
        tracker.finish()
        uptime = (
            f"{tracker.segments} segments, uptime {fmt_dur(tracker.closed)} "
            f"of {fmt_dur(target_uptime)}, "
        )
    summary = (
        f"WATCH end: {uptime}peak lease {peak:.0f}s, waits {tot_waits} (max {max_wait / 1000:.1f}s), "
        f"dag_miss {tot_dag_miss}, outcomes {tot_outcomes or '{}'}"
    )
    sink.emit(summary)


def duration_arg(text):
    try:
        return parse_duration(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


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
        watch(None, Sink(args.log), args.wake_wait_ms, target_uptime=args.target_uptime)
    else:
        hours = DEFAULT_HOURS if args.hours is None else args.hours
        watch(hours, Sink(args.log), args.wake_wait_ms)
    return 0


if __name__ == "__main__":
    sys.exit(main())
