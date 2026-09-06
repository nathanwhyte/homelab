#!/usr/bin/env python3
"""Backup freshness exporter (IMPR-1118).

Lists Cloudflare R2 objects for each DECLARED database backup and emits a
Prometheus text-format gauge of how old the newest backup is.

The DECLARED list below is the source of truth for "what SHOULD be backed up".
It is deliberately NOT inferred from the CronJobs that happen to exist: a
missing CronJob (BUG-1086 — coach postgres had none) produces no Job to fail
and no kube-state-metrics signal, so only a check against a declared list can
catch it. A database with no objects in R2 exports +Inf and fires the
freshness alert.

R2 access is via rclone (the same tool the backup jobs use). The `r2` remote
is configured from the environment exactly like the `r2-credentials` secret
(RCLONE_CONFIG_R2_*), so this script runs unmodified in a pod that mounts that
secret, or on a host with the same env exported.

Output is Prometheus text format on stdout (or a file given as argv[1]),
suitable for a node-exporter textfile collector.
"""

from __future__ import annotations

import datetime
import json
import subprocess
import sys
import time

# ── DECLARED BACKUP LIST ──────────────────────────────────────────────────
# Single source of truth for which databases must be backed up and how often.
# Add a database here when it gains a backup; remove it only when the backup
# is deliberately retired. `interval_hours` drives the freshness threshold
# (2x interval). `prefix` is the R2 key prefix the backup uploads to.
DECLARED = [
    {"app": "coach",       "prefix": "homelab/backups/cluster/homelab-k3s/databases/coach/",       "interval_hours": 24},
    {"app": "equal-risk",  "prefix": "homelab/backups/cluster/homelab-k3s/databases/equal-risk/",  "interval_hours": 24},
    {"app": "glossary",    "prefix": "homelab/backups/cluster/homelab-k3s/databases/glossary/",    "interval_hours": 24},
    {"app": "omnipendium", "prefix": "homelab/backups/cluster/homelab-k3s/databases/omnipendium/", "interval_hours": 24},
]

R2_REMOTE = "r2"


def _parse_rfc3339(value: str) -> float:
    """RFC3339 timestamp (rclone lsjson ModTime) -> unix seconds."""
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def newest_modtime(prefix: str) -> float | None:
    """Newest object Last-Modified (unix seconds) under `prefix`, or None if empty."""
    proc = subprocess.run(
        [
            "rclone", "lsjson", f"{R2_REMOTE}:{prefix}",
            "--files-only", "--recursive", "--no-mimetype",
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"rclone lsjson failed for {prefix!r}: {proc.stderr.strip()}")
    objects = json.loads(proc.stdout or "[]")
    if not objects:
        return None
    return max(_parse_rfc3339(obj["ModTime"]) for obj in objects)


def main() -> int:
    now = time.time()
    lines: list[str] = []
    up = 1

    for entry in DECLARED:
        app = entry["app"]
        max_age = entry["interval_hours"] * 3600 * 2  # 2x interval
        try:
            newest = newest_modtime(entry["prefix"])
        except Exception as exc:  # noqa: BLE001 — any listing failure is a failure
            print(f"# WARNING: {app}: {exc}", file=sys.stderr)
            up = 0
            newest = None
        age = "+Inf" if newest is None else f"{now - newest:.3f}"
        lines.append(f'backup_freshness_seconds{{app="{app}"}} {age}')
        lines.append(f'backup_freshness_max_age_seconds{{app="{app}"}} {max_age}')

    lines.append(f"backup_freshness_up {up}")
    lines.append(f"backup_freshness_last_run_timestamp_seconds {now:.3f}")

    header = (
        "# HELP backup_freshness_seconds Age in seconds of the newest R2 backup object per declared database.\n"
        "# TYPE backup_freshness_seconds gauge\n"
        "# HELP backup_freshness_max_age_seconds Freshness threshold (2x the declared interval) per database.\n"
        "# TYPE backup_freshness_max_age_seconds gauge\n"
        "# HELP backup_freshness_up 1 if the freshness check completed successfully, 0 otherwise.\n"
        "# TYPE backup_freshness_up gauge\n"
        "# HELP backup_freshness_last_run_timestamp_seconds Unix time of the last exporter run.\n"
        "# TYPE backup_freshness_last_run_timestamp_seconds gauge\n"
    )

    output = header + "\n".join(lines) + "\n"
    if len(sys.argv) > 1:
        with open(sys.argv[1], "w") as fh:
            fh.write(output)
    else:
        sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
