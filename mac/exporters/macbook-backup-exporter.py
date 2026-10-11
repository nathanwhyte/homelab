#!/usr/bin/env python3
"""Prometheus exporter for macbook-backup freshness.

Publishes one metric: the mtime of the marker that `bin/macbook-backup.sh`
stamps only when EVERY source uploaded cleanly. Its age is therefore a direct
"is the backup actually working" signal rather than "did the job start" —
which is the signal BUG-1212 needed and did not have, after the mirror failed
every source for 47 days with nothing watching.

Reads a user-owned marker, so it needs no root and runs as a user LaunchAgent.
It replaces mac/exporters/mac-gpu-exporter.py, which wrapped `powermetrics`
(and so required root) and exported metrics nothing graphs; those were retired
along with it. macmon covers Mac hardware telemetry on :9113.

Metrics:
    macbook_backup_last_success_timestamp_seconds   marker mtime; 0 if absent

Usage:
    python3 macbook-backup-exporter.py                       # :9114, every 15s
    python3 macbook-backup-exporter.py --backup-marker /tmp/last-success
"""

import argparse
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# The marker's default location for the user macbook-backup.sh runs as.
# $HOME is not usable: a system LaunchDaemon would resolve it to /var/root.
# This runs as a user agent, so it could, but the path is explicit anyway.
DEFAULT_MARKER = "/Users/noot/Library/Application Support/macbook-backup/last-success"
DEFAULT_PORT = 9114
DEFAULT_INTERVAL = 15

_lock = threading.Lock()
_metrics = {"macbook_backup_last_success_timestamp_seconds": 0}

_HELP = {
    "macbook_backup_last_success_timestamp_seconds": (
        "gauge",
        "Unix mtime of the macbook-backup last-success stamp; 0 if absent.",
    ),
}


def sample_once(marker: str) -> None:
    """Read the marker's mtime; 0 when it is missing or unreadable.

    0 rather than a sentinel such as -1, so that "no clean run on record" and
    "the last one went stale" are one expression for an alert:

        time() - macbook_backup_last_success_timestamp_seconds > 86400
    """
    try:
        ts = os.stat(marker).st_mtime
    except OSError:
        ts = 0
    with _lock:
        _metrics["macbook_backup_last_success_timestamp_seconds"] = ts


def sample_loop(interval: int, marker: str) -> None:
    while True:
        sample_once(marker)
        time.sleep(interval)


def format_metrics() -> str:
    with _lock:
        snapshot = dict(_metrics)
    lines = []
    for name, value in snapshot.items():
        mtype, helptext = _HELP[name]
        lines.append(f"# HELP {name} {helptext}")
        lines.append(f"# TYPE {name} {mtype}")
        lines.append(f"{name} {value}")
    return "\n".join(lines) + "\n"


class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metrics":
            body = format_metrics().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok\n")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # suppress access log


def main():
    parser = argparse.ArgumentParser(
        description="Prometheus exporter for macbook-backup freshness"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Listen port (default {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL,
        help=f"Sample interval seconds (default {DEFAULT_INTERVAL})",
    )
    parser.add_argument(
        "--backup-marker",
        default=DEFAULT_MARKER,
        help="macbook-backup last-success stamp to report on",
    )
    args = parser.parse_args()

    print(
        f"macbook-backup-exporter on :{args.port}, sampling {args.backup_marker} "
        f"every {args.interval}s",
        flush=True,
    )

    sampler = threading.Thread(
        target=sample_loop, args=(args.interval, args.backup_marker), daemon=True
    )
    sampler.start()

    # 0.0.0.0 rather than loopback: Alloy runs in a container and reaches this
    # as host.docker.internal (see mac/alloy/config.alloy). Same binding the
    # exporter it replaces used.
    server = ThreadingHTTPServer(("0.0.0.0", args.port), MetricsHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
