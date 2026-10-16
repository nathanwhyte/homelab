#!/usr/bin/env python3
"""Print one Haiku-share reading of the OpenViking VLM router (IMPR-1311), stdlib only.

Reads LiteLLM's Prometheus series for one explicit window and prints the figures that
compendium IMPR-1311 § Haiku over time records: calls by route, Haiku share, failovers
by reason, Haiku spend, the routing threshold left, and gemma failures, then a
per-bucket share table. Every figure is an ``increase()`` over the same
``--until - --since`` span evaluated at ``--until``, so no number comes from a
different window than the rest (IMPR-1331), and counter resets at a router restart
are absorbed.

Without ``--prom-url`` it opens its own ``kubectl -n grafana port-forward
svc/prom-prometheus`` on a free local port and closes it on exit. Read-only: it only
issues Prometheus queries.

    ov-haiku-reading.py --since 2026-10-15T08:28 --until 2026-10-15T22:28
    ov-haiku-reading.py --since 2026-10-14T13:36 --until now --bucket 1d

Naive times are read in ``--tz`` (default America/Chicago). Spend is the router's own
tally (``litellm_spend_metric_total``), not the Claude Console's.
"""

import argparse
import json
import math
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

SUCCESS = "litellm_deployment_success_responses_total"
FALLBACKS = "litellm_deployment_successful_fallbacks_total"
FAILURES = "litellm_deployment_failure_responses_total"
SPEND = "litellm_spend_metric_total"
BUDGET_LEFT = 'litellm_provider_remaining_budget_metric{api_provider="anthropic"}'

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


def by_label(result, label):
    """Instant-vector result -> {label value: float}, NaN dropped."""
    out = {}
    for series in result:
        value = float(series["value"][1])
        if not math.isnan(value):
            out[series["metric"].get(label, "")] = value
    return out


def parse_time(text, tz):
    if text == "now":
        return datetime.now(UTC)
    t = datetime.fromisoformat(text)
    return t if t.tzinfo else t.replace(tzinfo=tz)


def parse_bucket(text):
    m = re.fullmatch(r"(\d+)([hd])", text)
    if not m:
        raise argparse.ArgumentTypeError("bucket must look like 1h, 6h or 1d")
    n = int(m.group(1))
    return timedelta(hours=n) if m.group(2) == "h" else timedelta(days=n)


def collect(prom, since, until, bucket):
    span = int((until - since).total_seconds())
    if span <= 0:
        raise ValueError("--until must be after --since")

    def sum_by(label, expr):
        return f"sum by ({label}) (increase({expr}[{span}s]))"

    reading = {
        "since": since.isoformat(),
        "until": until.isoformat(),
        "span_s": span,
        "calls": by_label(
            instant(prom, sum_by("api_provider", SUCCESS), until), "api_provider"
        ),
        "failovers": by_label(
            instant(prom, sum_by("exception_status", FALLBACKS), until),
            "exception_status",
        ),
        "failures": by_label(
            instant(prom, sum_by("api_provider", FAILURES), until), "api_provider"
        ),
        "spend": by_label(
            instant(prom, sum_by("api_provider", SPEND), until), "api_provider"
        ),
        "budget_left": None,
        "buckets": [],
    }
    left = by_label(instant(prom, f"max({BUDGET_LEFT})", until), "")
    reading["budget_left"] = left.get("")

    # Buckets end at since + k*bucket; a trailing remainder shorter than one bucket is
    # left out of the table (the totals above still cover it).
    step = int(bucket.total_seconds())
    if step <= span:
        end = since + timedelta(seconds=step * (span // step))
        result = _query(
            prom,
            "query_range",
            query=f"sum by (api_provider) (increase({SUCCESS}[{step}s]))",
            start=(since + bucket).timestamp(),
            end=end.timestamp(),
            step=step,
        )
        rows = {}
        for series in result:
            provider = series["metric"].get("api_provider", "")
            for ts, value in series["values"]:
                v = float(value)
                rows.setdefault(float(ts), {})[provider] = 0.0 if math.isnan(v) else v
        reading["buckets"] = [
            {"end": datetime.fromtimestamp(ts, UTC).isoformat(), "calls": c}
            for ts, c in sorted(rows.items())
        ]
        reading["remainder_s"] = span - step * (span // step)
    return reading


def _n(x):
    return f"{round(x):,}"


def _share(part, total):
    return f"{100 * part / total:.0f}%" if total else "n/a"


def render(reading, tz):
    since = datetime.fromisoformat(reading["since"])
    until = datetime.fromisoformat(reading["until"])

    def local(t):
        return t.astimezone(tz)

    def utc(t):
        return t.astimezone(UTC).strftime("%H:%MZ")

    calls = reading["calls"]
    total = sum(calls.values())
    haiku = calls.get("anthropic", 0.0)
    routes = ", ".join(
        f"{_n(v)} on {ROUTES.get(k, k)}"
        for k, v in sorted(calls.items(), key=lambda kv: -kv[1])
    )
    failovers = ", ".join(
        f"{_n(v)} × `{k}`" + (f" ({REASONS[k]})" if k in REASONS else "")
        for k, v in sorted(reading["failovers"].items())
    )
    fails = ", ".join(
        f"{_n(v)} on {ROUTES.get(k, k)}" for k, v in sorted(reading["failures"].items())
    )
    left = reading["budget_left"]
    lines = [
        (
            f"_Window: {local(since):%Y-%m-%d %H:%M} to {local(until):%Y-%m-%d %H:%M %Z}"
            f" ({utc(since)} to {utc(until)}), {reading['span_s'] / 3600:.1f} h._"
        ),
        "",
        "| Measure | Value |",
        "| --- | --- |",
        f"| VLM calls | {_n(total)}: {routes or 'none'} (Haiku {_share(haiku, total)}) |",
        f"| Failovers | {failovers or 'none'} |",
        (
            f"| Haiku spend in window | ${reading['spend'].get('anthropic', 0.0):.2f}"
            " (router tally, not the Console) |"
        ),
        "| Threshold left at window end | "
        + (f"${left:.2f} |" if left is not None else "n/a |"),
        f"| Failed responses | {fails or 'none'} |",
    ]
    if reading["buckets"]:
        lines += ["", f"| Bucket ending ({local(until):%Z}) | Calls | Haiku |"]
        lines.append("| --- | --- | --- |")
        for b in reading["buckets"]:
            end = local(datetime.fromisoformat(b["end"]))
            n = sum(b["calls"].values())
            h = b["calls"].get("anthropic", 0.0)
            lines.append(f"| {end:%m-%d %H:%M} | {_n(n)} | {_share(h, n)} |")
        if reading.get("remainder_s"):
            lines.append("")
            lines.append(
                f"_The last {reading['remainder_s'] // 60} min are in the totals"
                " but not in a bucket._"
            )
    return "\n".join(lines)


class PortForward:
    """``kubectl port-forward`` to Prometheus on a free local port."""

    def __enter__(self):
        self.proc = subprocess.Popen(
            [
                "kubectl",
                "-n",
                "grafana",
                "port-forward",
                "svc/prom-prometheus",
                ":9090",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for line in self.proc.stdout:
            m = FORWARD_RE.search(line)
            if m:
                return f"http://127.0.0.1:{m.group(1)}"
        self.proc.wait()
        raise RuntimeError("kubectl port-forward exited before forwarding")

    def __exit__(self, *exc):
        self.proc.terminate()
        self.proc.wait(timeout=10)


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
    bucket = args.bucket or (
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
