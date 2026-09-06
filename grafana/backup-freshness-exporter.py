#!/usr/bin/env python3
"""Backup freshness exporter (IMPR-1118).

Lists the Cloudflare R2 backup tree once and emits a Prometheus text-format
gauge of how old the newest backup is for each DECLARED database.

The DECLARED list below is the source of truth for "what SHOULD be backed up".
It is deliberately NOT inferred from the CronJobs that happen to exist: a
missing CronJob (BUG-1086 — coach postgres had none) produces no Job to fail
and no kube-state-metrics signal, so only a check against a declared list can
catch it. A declared database with no objects in R2 exports +Inf and fires the
freshness alert.

R2 layout (verified against the live bucket 2026-09-06): the postgres backup
jobs (boto3, `S3_PROVIDER_*` env from each app's `r2-backup-credentials`
secret) upload date-partitioned objects to

    homelab/backups/cluster/homelab-k3s/rebuild-exports/<YYYY-MM-DD>/postgres/<app>/<app>-<ts>.pgdump

so there is no per-app prefix to list. This script lists the whole
`rebuild-exports/` subtree once (a few dozen objects) and groups by the
`postgres/<app>/` path segment.

R2 access is via rclone with a remote named `r2`, configured from the
environment (RCLONE_CONFIG_R2_*). The `yt-dlp/r2-credentials` secret already
carries exactly those keys for the weekly media backup; copy it into the
namespace that runs this exporter, or export the same variables on a host.

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
# Add a database here when it SHOULD have a backup — not when it gains one;
# a declared database with no backup job is precisely the case this exists to
# surface. Remove an entry only when the backup is deliberately retired.
# `interval_hours` drives the freshness threshold (2x interval). `segment` is
# the `<kind>/<app>/` path segment the backup writes under each date folder.
#
# As of 2026-09-06 only coach and equal-risk have a backup CronJob; glossary
# and omnipendium run postgres with no job at all, so they alert until one is
# created (grafana/backup-alerts.md).
DECLARED = [
    {"app": "coach", "segment": "postgres/coach/", "interval_hours": 24},
    {"app": "equal-risk", "segment": "postgres/equal-risk/", "interval_hours": 24},
    {"app": "glossary", "segment": "postgres/glossary/", "interval_hours": 24},
    {"app": "omnipendium", "segment": "postgres/omnipendium/", "interval_hours": 24},
]

R2_REMOTE = "r2"
R2_BUCKET = "homelab"
BACKUP_ROOT = "backups/cluster/homelab-k3s/rebuild-exports/"


def _parse_rfc3339(value: str) -> float:
    """RFC3339 timestamp (rclone lsjson ModTime) -> unix seconds."""
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def list_backup_tree() -> list[dict]:
    """All file objects under BACKUP_ROOT, as rclone lsjson dicts (Path, ModTime, ...)."""
    target = f"{R2_REMOTE}:{R2_BUCKET}/{BACKUP_ROOT}"
    proc = subprocess.run(
        ["rclone", "lsjson", target, "--files-only", "--recursive", "--no-mimetype"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"rclone lsjson failed for {target!r}: {proc.stderr.strip()}"
        )
    return json.loads(proc.stdout or "[]")


def newest_modtime(objects: list[dict], segment: str) -> float | None:
    """Newest ModTime (unix seconds) among objects whose path contains `/<segment>`, or None."""
    needle = f"/{segment}"
    times = [_parse_rfc3339(obj["ModTime"]) for obj in objects if needle in obj["Path"]]
    return max(times) if times else None


def main() -> int:
    now = time.time()
    lines: list[str] = []
    up = 1

    try:
        objects = list_backup_tree()
    except Exception as exc:  # noqa: BLE001 — any listing failure is a failure
        # Emit only the exporter-health series: per-app +Inf here would fire
        # four false BackupFreshnessStale criticals on top of ExporterDown.
        print(f"# WARNING: {exc}", file=sys.stderr)
        up = 0
        objects = None

    if objects is not None:
        for entry in DECLARED:
            app = entry["app"]
            max_age = entry["interval_hours"] * 3600 * 2  # 2x interval
            newest = newest_modtime(objects, entry["segment"])
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
