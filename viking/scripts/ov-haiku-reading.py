#!/usr/bin/env python3
"""Print one Haiku-share reading of the OpenViking VLM router (IMPR-1311), stdlib only.

Reads LiteLLM's Prometheus series for one explicit window and prints the figures that
compendium IMPR-1311 § Haiku over time records: calls by route, Haiku share, failovers
by reason, Haiku spend, the routing threshold left, and gemma failures, then a
per-bucket share table. Every figure is an ``increase()`` over the same
``--until - --since`` span evaluated at ``--until``, so no number comes from a
different window than the rest (IMPR-1331), and counter resets at a router restart
are absorbed. Both ends are converted to UTC before any arithmetic, so a window that
crosses a DST change is measured in elapsed time.

Without ``--prom-url`` it opens its own ``kubectl -n grafana port-forward
svc/prom-prometheus`` on a free local port and closes it on exit. Read-only: it only
issues Prometheus queries.

    ov-haiku-reading.py --since 2026-10-15T08:28 --until 2026-10-15T22:28
    ov-haiku-reading.py --since 2026-10-16T08:00 --until now --bucket 6h

The window must lie inside Prometheus's retention (3 days on 2026-10-16): a ``--since``
older than its oldest sample is refused, because ``increase()`` would silently count
only what is left. A longer reading, such as IMPR-1311's one-month reading, is the sum
of readings taken inside retention.

Naive times are read in ``--tz`` (default America/Chicago). Spend is the router's own
tally (``litellm_spend_metric_total``), not the Claude Console's.

Missing data is never shown as zero. A window with no success samples at all is an
error, and a bucket with none prints ``no data``. For every counter, each label's
series are checked one by one before they are summed: if any series has samples but
no ``increase()`` value (a NaN, or a new pod's series whose first sample is the
window's last), that label prints ``unknown``, and so do the totals and shares it
feeds. Only a label with no samples at all counts as zero. Both ends are truncated to
whole seconds, and times print with their zone, so the two 01:00 buckets of a DST
fall-back stay distinct.
"""

import argparse
import collections
import json
import math
import re
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

SUCCESS = "litellm_deployment_success_responses_total"
FALLBACKS = "litellm_deployment_successful_fallbacks_total"
FAILURES = "litellm_deployment_failure_responses_total"
SPEND = "litellm_spend_metric_total"
BUDGET_LEFT = 'litellm_provider_remaining_budget_metric{api_provider="anthropic"}'
# Prometheus's own oldest retained sample. Homelab retention is short (3d on
# 2026-10-16), and increase() over a window reaching past it silently counts only what
# is left, so such a window is refused instead.
OLDEST = "prometheus_tsdb_lowest_timestamp_seconds"

# api_provider label -> the route name the compendium entry uses.
ROUTES = {"openai": "gemma", "anthropic": "Haiku"}
REASONS = {"429": "gemma full", "408": "gemma past 90 s"}

FORWARD_RE = re.compile(r"Forwarding from 127\.0\.0\.1:(\d+) ")


def _get(url):
    """Single HTTP choke point; the tests replace it."""
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.load(resp)


def _query(prom, path, **params):
    body = _get(f"{prom}/api/v1/{path}?{urllib.parse.urlencode(params)}")
    if body.get("status") != "success":
        raise RuntimeError(f"Prometheus {path} failed: {body.get('error', body)}")
    return body["data"]["result"]


def instant(prom, expr, at):
    return _query(prom, "query", query=expr, time=at.timestamp())


def _num(text):
    """A Prometheus sample value, with NaN as None (unknown)."""
    v = float(text)
    return None if math.isnan(v) else v


def by_label(result, label):
    """Instant-vector result -> {label value: float or None}."""
    return {s["metric"].get(label, ""): _num(s["value"][1]) for s in result}


def parse_time(text, tz):
    t = datetime.now(UTC) if text == "now" else datetime.fromisoformat(text)
    if t.tzinfo is None:
        t = t.replace(tzinfo=tz)
    return whole_seconds(t)


def whole_seconds(t):
    """UTC, truncated to the second: the span is whole seconds, so the ends must be."""
    return t.astimezone(UTC).replace(microsecond=0)


def parse_bucket(text):
    m = re.fullmatch(r"(\d+)([hd])", text)
    if not m or int(m.group(1)) == 0:
        raise argparse.ArgumentTypeError("bucket must look like 1h, 6h or 1d")
    n = int(m.group(1))
    return timedelta(hours=n) if m.group(2) == "h" else timedelta(days=n)


def counter_queries(metric, label, seconds):
    """The three queries that decide each label's increase: the summed increase, the
    number of series with samples, and the number with an ``increase()`` value."""
    rng = f"{metric}[{seconds}s]"
    return (
        f"sum by ({label}) (increase({rng}))",
        f"count by ({label}) (last_over_time({rng}))",
        f"count by ({label}) (increase({rng}))",
    )


def reconcile(total, sampled, counted):
    """{label: increase or None}. The check is per series, before the sum: a label is
    unknown when any of its series has samples but no ``increase()`` value (a single
    sample, as after a router restart). A label with no samples at all is absent."""
    out = {}
    for label in sorted(set(sampled) | set(total)):
        complete = (
            sampled.get(label) is not None and counted.get(label) == sampled[label]
        )
        out[label] = total.get(label) if complete else None
    return out


def counter(prom, metric, label, seconds, at):
    total, sampled, counted = (
        by_label(instant(prom, q, at), label)
        for q in counter_queries(metric, label, seconds)
    )
    return reconcile(total, sampled, counted)


def collect(prom, since, until, bucket):
    since, until = whole_seconds(since), whole_seconds(until)
    span = int((until - since).total_seconds())
    if span <= 0:
        raise ValueError("--until must be after --since")

    oldest = by_label(instant(prom, f"max({OLDEST})", until), "").get("")
    if oldest is not None and since.timestamp() < oldest:
        kept = datetime.fromtimestamp(oldest, UTC).isoformat()
        raise RuntimeError(
            f"--since is older than Prometheus's oldest sample ({kept}); retention has"
            " already dropped the start of the window, so its totals would be short."
            " Take readings inside the retention period and add them up."
        )

    calls = counter(prom, SUCCESS, "api_provider", span, until)
    if not calls:
        raise RuntimeError(
            f"no {SUCCESS} samples in the window; is litellm-vlm being scraped?"
        )
    reading = {
        "since": since.isoformat(),
        "until": until.isoformat(),
        "span_s": span,
        "calls": calls,
        "failovers": counter(prom, FALLBACKS, "exception_status", span, until),
        "failures": counter(prom, FAILURES, "api_provider", span, until),
        "spend": counter(prom, SPEND, "api_provider", span, until),
        "budget_left": by_label(instant(prom, f"max({BUDGET_LEFT})", until), "").get(
            ""
        ),
        "oldest_sample": oldest,
        "buckets": [],
        "remainder_s": 0,
    }

    # Buckets cover (since + (k-1)*bucket, since + k*bucket]; a trailing remainder
    # shorter than one bucket is left out of the table (the totals still cover it).
    step = int(bucket.total_seconds())
    count = span // step
    if count:
        ends = [since + bucket * k for k in range(1, count + 1)]

        def per_bucket(expr):
            result = _query(
                prom,
                "query_range",
                query=expr,
                start=ends[0].timestamp(),
                end=ends[-1].timestamp(),
                step=step,
            )
            points = collections.defaultdict(dict)
            for series in result:
                provider = series["metric"].get("api_provider", "")
                for ts, value in series["values"]:
                    points[round(float(ts))][provider] = _num(value)
            return points

        total, sampled, counted = (
            per_bucket(q) for q in counter_queries(SUCCESS, "api_provider", step)
        )
        buckets = []
        for end in ends:
            ts = round(end.timestamp())
            calls = reconcile(
                total.get(ts, {}), sampled.get(ts, {}), counted.get(ts, {})
            )
            buckets.append({"end": end.isoformat(), "calls": calls or None})
        reading["buckets"] = buckets
        reading["remainder_s"] = span - step * count
    return reading


def _n(x):
    return "unknown" if x is None else f"{round(x):,}"


def _total(values):
    return None if None in values else sum(values)


def _share(part, total):
    if part is None or total is None:
        return "unknown"
    return f"{100 * part / total:.0f}%" if total else "n/a"


def render(reading, tz):
    since = datetime.fromisoformat(reading["since"])
    until = datetime.fromisoformat(reading["until"])

    def local(t):
        return t.astimezone(tz)

    def utc(t):
        return t.astimezone(UTC).strftime("%H:%MZ")

    calls = reading["calls"]
    total = _total(calls.values())
    haiku = calls.get("anthropic", 0.0)
    routes = ", ".join(
        f"{_n(v)} on {ROUTES.get(k, k)}"
        for k, v in sorted(calls.items(), key=lambda kv: -(kv[1] or 0))
    )
    failovers = ", ".join(
        f"{_n(v)} × `{k}`" + (f" ({REASONS[k]})" if k in REASONS else "")
        for k, v in sorted(reading["failovers"].items())
    )
    fails = ", ".join(
        f"{_n(v)} on {ROUTES.get(k, k)}" for k, v in sorted(reading["failures"].items())
    )
    spend = reading["spend"].get("anthropic")
    left = reading["budget_left"]
    lines = [
        (
            f"_Window: {local(since):%Y-%m-%d %H:%M %Z} to"
            f" {local(until):%Y-%m-%d %H:%M %Z}"
            f" ({utc(since)} to {utc(until)}), {reading['span_s'] / 3600:.1f} h._"
        ),
        "",
        "| Measure | Value |",
        "| --- | --- |",
        f"| VLM calls | {_n(total)}: {routes} (Haiku {_share(haiku, total)}) |",
        f"| Failovers | {failovers or 'none recorded'} |",
        (
            "| Haiku spend in window | "
            + (
                f"${spend:.2f} (router tally, not the Console) |"
                if spend is not None
                else (
                    "unknown (incomplete spend series) |"
                    if "anthropic" in reading["spend"]
                    else "no Haiku spend samples |"
                )
            )
        ),
        "| Threshold left at window end | "
        + (f"${left:.2f} |" if left is not None else "unknown |"),
        f"| Failed responses | {fails or 'none recorded'} |",
    ]
    if reading["oldest_sample"] is None:
        lines += [
            "",
            (
                f"_Warning: Prometheus did not report `{OLDEST}`, so the window was"
                " not checked against retention; totals may be short._"
            ),
        ]
    if reading["buckets"]:
        lines += ["", "| Bucket ending | Calls | Haiku |", "| --- | --- | --- |"]
        for b in reading["buckets"]:
            end = f"{local(datetime.fromisoformat(b['end'])):%m-%d %H:%M %Z}"
            if b["calls"] is None:
                lines.append(f"| {end} | no data | no data |")
                continue
            n = _total(b["calls"].values())
            h = b["calls"].get("anthropic", 0.0)
            lines.append(f"| {end} | {_n(n)} | {_share(h, n)} |")
        if reading["remainder_s"]:
            lines.append("")
            lines.append(
                f"_The last {reading['remainder_s'] // 60} min are in the totals"
                " but not in a bucket._"
            )
    return "\n".join(lines)


class PortForward:
    """``kubectl port-forward`` to Prometheus on a free local port.

    A reader thread drains kubectl's output for the whole run, so a chatty child never
    blocks on a full pipe, and readiness waits at most ``ready_timeout`` seconds.
    """

    CMD = ("kubectl", "-n", "grafana", "port-forward", "svc/prom-prometheus", ":9090")

    def __init__(self, cmd=None, ready_timeout=20.0, stop_timeout=5.0):
        self.cmd = list(cmd or self.CMD)
        self.ready_timeout = ready_timeout
        self.stop_timeout = stop_timeout

    def _new_event(self):
        """The readiness event; a seam the tests override."""
        return threading.Event()

    def __enter__(self):
        self.url = None
        self.tail = collections.deque(maxlen=5)
        self.ready = self._new_event()
        self.reader = None
        self.proc = subprocess.Popen(
            self.cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
        try:  # Ctrl-C anywhere in startup: __exit__ will not run, so clean up here
            self.reader = threading.Thread(target=self._drain, daemon=True)
            self.reader.start()
            self.ready.wait(self.ready_timeout)
        except BaseException:
            self._stop()
            raise
        if self.url is None:
            self._stop()
            said = " | ".join(self.tail) or "no output"
            raise RuntimeError(
                f"kubectl port-forward not ready within {self.ready_timeout:g} s: {said}"
            )
        return self.url

    def _drain(self):
        for line in self.proc.stdout:
            if self.url is None:
                m = FORWARD_RE.search(line)
                if m:
                    self.url = f"http://127.0.0.1:{m.group(1)}"
                    self.ready.set()
                else:
                    self.tail.append(line.strip())
        self.ready.set()  # EOF: wake a waiter that is still waiting

    def _stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=self.stop_timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        if self.reader is not None and self.reader.ident is not None:
            self.reader.join(timeout=self.stop_timeout)
        self.proc.stdout.close()

    def __exit__(self, *exc):
        self._stop()
        return False


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--since", required=True, help="window start (ISO time)")
    ap.add_argument("--until", default="now", help="window end (ISO time or now)")
    ap.add_argument("--tz", default="America/Chicago", help="zone for naive times")
    ap.add_argument(
        "--bucket",
        type=parse_bucket,
        help="table bucket (default 1h up to 48 h, else 1d)",
    )
    ap.add_argument("--prom-url", help="Prometheus base URL; skips the port-forward")
    ap.add_argument("--json", action="store_true", help="print the raw reading")
    args = ap.parse_args(argv)

    tz = ZoneInfo(args.tz)
    since, until = parse_time(args.since, tz), parse_time(args.until, tz)
    bucket = args.bucket
    if bucket is None:
        bucket = (
            timedelta(hours=1)
            if until - since <= timedelta(hours=48)
            else timedelta(days=1)
        )
    try:
        if args.prom_url:
            reading = collect(args.prom_url.rstrip("/"), since, until, bucket)
        else:
            with PortForward() as prom:
                reading = collect(prom, since, until, bucket)
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"ov-haiku-reading: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(reading, indent=2) if args.json else render(reading, tz))
    return 0


if __name__ == "__main__":
    sys.exit(main())
