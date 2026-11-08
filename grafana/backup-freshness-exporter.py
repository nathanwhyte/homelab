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

Longhorn source (IMPR-1478): when LONGHORN_DAILY names a JSON list of
"<namespace>/<pvc>" strings (generated from longhorn/replica-policy.yaml's
`backup: daily` rows by `replica-policy.py --daily-json`), the exporter also
reads backups.longhorn.io and volumes.longhorn.io through the in-cluster API and
emits backup_freshness_seconds{app="longhorn/<namespace>/<pvc>"} per row. The
per-volume Backup CRs are the signal, not the recurring Job's exit status:
Longhorn 1.12.1's recurring-job binary can exit 0 when one volume's backup
fails. A Backup names only its Longhorn volume (`backup-volume` label), so the
row joins through the Volume whose status.kubernetesStatus carries the PVC.

Output is Prometheus text format on stdout (or a file given as argv[1]).
With --serve, expose /metrics on port 9811 and refresh the listing every 5m.
"""

from __future__ import annotations

import datetime
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import ssl
import subprocess
import sys
import threading
import time
import urllib.request

# ── DECLARED BACKUP LIST ──────────────────────────────────────────────────
# Single source of truth for which databases must be backed up and how often.
# `interval_hours` drives the freshness threshold (2x interval). `segment` is
# the `<kind>/<app>/` path segment the backup writes under each date folder.
#
# WHEN TO ADD an entry: the database is meant to be backed up *now*. That
# includes a database whose backup is supposed to be running but is not —
# catching exactly that is the point (BUG-1086: coach had no CronJob, so there
# was no failing Job and no kube-state-metrics signal to notice). Never infer
# this list from the CronJobs that happen to exist.
#
# WHEN NOT TO ADD: a backup that is merely *planned*. A declared database with
# no objects in R2 exports +Inf and fires a critical alert that can never
# resolve, because nothing is coming to clear it. Two of those ran for hours
# on 2026-09-06 and taught the reader to ignore the backup alert group — the
# exact failure IMPR-1118 was built to prevent (BUG-1104). Declare it in the
# same change that ships its CronJob and credentials, not before.
#
# Deliberately NOT declared:
#   glossary     — never backed up by design; its data is rebuildable. Do not
#                  add it back on the assumption that any postgres deserves a
#                  backup, which is how it got here the first time.
#   omnipendium  — backup is planned, not shipped (TASK-1117). Add it as part
#                  of that work, alongside the CronJob and the
#                  `r2-backup-credentials` secret.
DECLARED = [
    {"app": "coach", "segment": "postgres/coach/", "interval_hours": 24},
    {"app": "equal-risk", "segment": "postgres/equal-risk/", "interval_hours": 24},
]

R2_REMOTE = "r2"
R2_BUCKET = "homelab"
BACKUP_ROOT = "backups/cluster/homelab-k3s/rebuild-exports/"

# Longhorn source. The daily RecurringJob runs once a day; 36h leaves room for a
# slow run without waiting a full second day to notice a missed one.
LONGHORN_DAILY = os.environ.get("LONGHORN_DAILY", "")
LONGHORN_MAX_AGE_SECONDS = 36 * 3600
LONGHORN_NS = "longhorn-system"
K8S_API = "https://kubernetes.default.svc"
SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"


def _parse_rfc3339(value: str) -> float:
    """RFC3339 timestamp (rclone lsjson ModTime) -> unix seconds."""
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def list_backup_tree() -> list[dict]:
    """All file objects under BACKUP_ROOT, as rclone lsjson dicts (Path, ModTime, ...)."""
    target = f"{R2_REMOTE}:{R2_BUCKET}/{BACKUP_ROOT}"
    proc = subprocess.run(
        [
            "rclone",
            "lsjson",
            target,
            "--files-only",
            "--recursive",
            "--no-mimetype",
            "--retries",
            "1",
            "--low-level-retries",
            "1",
            "--contimeout",
            "10s",
            "--timeout",
            "30s",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"rclone lsjson failed for {target!r}: {proc.stderr.strip()}"
        )
    objects = json.loads(proc.stdout)
    if not isinstance(objects, list) or not all(
        isinstance(obj, dict) for obj in objects
    ):
        raise ValueError("Expected an object listing array")
    for obj in objects:
        if not isinstance(obj.get("Path"), str) or not isinstance(
            obj.get("Size"), (int, float)
        ):
            raise ValueError("Invalid object metadata")
        _parse_rfc3339(obj["ModTime"])
    return objects


def newest_modtime(objects: list[dict], segment: str) -> float | None:
    """Newest ModTime (unix seconds) among objects whose path contains `/<segment>`, or None."""
    needle = f"/{segment}"
    # A newer checksum/manifest must not hide an old or absent database dump.
    times = [
        _parse_rfc3339(obj["ModTime"])
        for obj in objects
        if needle in "/" + obj["Path"]
        and obj["Path"].endswith(".pgdump")
        and obj.get("Size", 0) > 0
    ]
    return max(times) if times else None


def load_longhorn_daily(path: str) -> list[str]:
    """The declared daily Longhorn PVCs, as "<namespace>/<pvc>" strings."""
    with open(path) as fh:
        rows = json.load(fh)
    if not isinstance(rows, list) or not all(
        isinstance(r, str) and r.count("/") == 1 for r in rows
    ):
        raise ValueError("LONGHORN_DAILY must be a JSON list of namespace/pvc strings")
    return rows


def k8s_list(path: str) -> list[dict]:
    """GET a list from the API server with the pod's service-account token."""
    with open(f"{SA_DIR}/token") as fh:
        token = fh.read().strip()
    ctx = ssl.create_default_context(cafile=f"{SA_DIR}/ca.crt")
    req = urllib.request.Request(
        f"{K8S_API}{path}", headers={"Authorization": f"Bearer {token}"}
    )
    with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
        body = json.load(resp)
    items = body.get("items")
    if not isinstance(items, list):
        raise ValueError(f"no items list at {path}")
    return items


def longhorn_freshness(
    daily: list[str], volumes: list[dict], backups: list[dict]
) -> tuple[dict[str, float | None], int]:
    """Newest Completed backup time per daily PVC, and the count of unmatched backups.

    The join is two steps: the PVC row matches the Volume whose
    status.kubernetesStatus names it, and that Volume's name matches a Backup's
    `backup-volume` label. A Backup of a volume that no longer exists (a deleted
    claim's retained backups, or a recreated claim's old volume) is counted as
    unmatched and never stands in for the current volume.
    """
    # A Released volume left by a recreated claim on a Retain class still names
    # the claim; the Bound one is the current volume whatever the list order.
    by_pvc: dict[str, str] = {}
    bound: set[str] = set()
    for v in volumes:
        ks = (v.get("status") or {}).get("kubernetesStatus") or {}
        if ks.get("namespace") and ks.get("pvcName"):
            key = f"{ks['namespace']}/{ks['pvcName']}"
            is_bound = ks.get("pvStatus") == "Bound"
            if key not in by_pvc or (is_bound and key not in bound):
                by_pvc[key] = v["metadata"]["name"]
            if is_bound:
                bound.add(key)
    current = {v["metadata"]["name"] for v in volumes}
    completed: dict[str, list[float]] = {}
    unmatched = 0
    for b in backups:
        labels = (b.get("metadata") or {}).get("labels") or {}
        status = b.get("status") or {}
        vol = labels.get("backup-volume") or status.get("volumeName")
        if vol not in current:
            unmatched += 1
            continue
        created = status.get("snapshotCreatedAt")
        if status.get("state") == "Completed" and created:
            completed.setdefault(vol, []).append(_parse_rfc3339(created))
    newest = {}
    for pvc in daily:
        times = completed.get(by_pvc.get(pvc, ""), [])
        newest[f"longhorn/{pvc}"] = max(times) if times else None
    return newest, unmatched


def longhorn_target_available(targets: list[dict]) -> int:
    """1 when every BackupTarget reports available, else 0 (none counts as 0)."""
    return int(
        bool(targets)
        and all((t.get("status") or {}).get("available") is True for t in targets)
    )


def collect_longhorn(path: str):
    daily = load_longhorn_daily(path)
    base = f"/apis/longhorn.io/v1beta2/namespaces/{LONGHORN_NS}"
    newest, unmatched = longhorn_freshness(
        daily, k8s_list(f"{base}/volumes"), k8s_list(f"{base}/backups")
    )
    available = longhorn_target_available(k8s_list(f"{base}/backuptargets"))
    return newest, {"unmatched": unmatched, "target_available": available}


def collect():
    """Validate the complete listing before publishing a successful sample.

    Each source (the R2 dump listing, the Longhorn CRs) is all-or-nothing on
    its own, and one failing does not withhold the other's rows; either failing
    sets ok to False, which backup_freshness_up reports.

    Returns (newest, last_run, longhorn, ok): newest maps app to its newest
    backup time, or is None when every source failed; longhorn is the Longhorn
    source's extra gauges, or None when it failed or is not configured.
    """
    newest: dict[str, float | None] = {}
    succeeded = 0
    ok = True
    try:
        objects = list_backup_tree()
        newest.update(
            {
                entry["app"]: newest_modtime(objects, entry["segment"])
                for entry in DECLARED
            }
        )
        succeeded += 1
    except Exception as exc:
        # Do not log provider output, which may contain credential details.
        print(
            f"Backup listing failed: {type(exc).__name__}", file=sys.stderr, flush=True
        )
        ok = False
    longhorn = None
    if LONGHORN_DAILY:
        try:
            lh_newest, longhorn = collect_longhorn(LONGHORN_DAILY)
            newest.update(lh_newest)
            succeeded += 1
        except Exception as exc:
            print(
                f"Longhorn listing failed: {type(exc).__name__}",
                file=sys.stderr,
                flush=True,
            )
            ok = False
    return (newest if succeeded else None), time.time(), longhorn, ok


def render(newest, last_run, longhorn=None, ok=None, now=None):
    now = time.time() if now is None else now
    lines: list[str] = []
    if newest is not None:
        max_ages = {
            entry["app"]: entry["interval_hours"] * 3600 * 2  # 2x interval
            for entry in DECLARED
        }
        for app, timestamp in newest.items():
            max_age = max_ages.get(app, LONGHORN_MAX_AGE_SECONDS)
            age = "+Inf" if timestamp is None else f"{max(0, now - timestamp):.3f}"
            lines.append(f'backup_freshness_seconds{{app="{app}"}} {age}')
            lines.append(f'backup_freshness_max_age_seconds{{app="{app}"}} {max_age}')
    if longhorn is not None:
        lines.append(f"backup_freshness_unmatched_backups {longhorn['unmatched']}")
        lines.append(
            f"backup_freshness_longhorn_target_available {longhorn['target_available']}"
        )

    up = newest is not None if ok is None else ok
    lines.append(f"backup_freshness_up {int(up)}")
    lines.append(f"backup_freshness_last_run_timestamp_seconds {last_run:.3f}")

    header = (
        "# HELP backup_freshness_seconds Age in seconds of the newest backup per declared database or Longhorn volume.\n"
        "# TYPE backup_freshness_seconds gauge\n"
        "# HELP backup_freshness_max_age_seconds Freshness threshold per declared database (2x its interval) or Longhorn volume (36h).\n"
        "# TYPE backup_freshness_max_age_seconds gauge\n"
        "# HELP backup_freshness_unmatched_backups Longhorn backups whose volume no longer exists.\n"
        "# TYPE backup_freshness_unmatched_backups gauge\n"
        "# HELP backup_freshness_longhorn_target_available 1 if every Longhorn BackupTarget reports available.\n"
        "# TYPE backup_freshness_longhorn_target_available gauge\n"
        "# HELP backup_freshness_up 1 if the freshness check completed successfully, 0 otherwise.\n"
        "# TYPE backup_freshness_up gauge\n"
        "# HELP backup_freshness_last_run_timestamp_seconds Unix time of the last exporter run.\n"
        "# TYPE backup_freshness_last_run_timestamp_seconds gauge\n"
    )

    return header + "\n".join(lines) + "\n"


def _refresh_once(state, lock):
    """Run one collect() cycle and swap it into `state` atomically under `lock`.

    Split out of `serve()`'s refresh loop so a single iteration can be driven
    directly in tests without waiting on `time.sleep(300)`.
    """
    sample = collect()
    with lock:
        state[:] = sample


def _make_handler(state, lock):
    """Build a Handler bound to the given state/lock, for `serve()` and tests."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in ("/metrics", "/healthz"):
                self.send_error(404)
                return
            with lock:
                body = render(*state).encode() if self.path == "/metrics" else b"ok\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    return Handler


def serve():
    state = [None, 0.0]
    lock = threading.Lock()

    def refresh():
        while True:
            _refresh_once(state, lock)
            time.sleep(300)

    threading.Thread(target=refresh, daemon=True).start()
    ThreadingHTTPServer(
        ("0.0.0.0", int(os.environ.get("PORT", "9811"))), _make_handler(state, lock)
    ).serve_forever()


def main() -> int:
    if sys.argv[1:] == ["--serve"]:
        serve()
        return 0
    output = render(*collect())
    if len(sys.argv) > 1:
        with open(sys.argv[1], "w") as fh:
            fh.write(output)
    else:
        sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
