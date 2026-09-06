# Backup alerts (IMPR-1118)

Alerting so a dead backup cannot stay silent. Two backup failures ran
undetected for months:

- **BUG-1014** — equal-risk postgres backup failed every night for 5 months:
  `pg_dump` succeeded but the upload step failed, so the Job failed.
- **BUG-1086** — coach postgres had **no CronJob at all**, so there was no Job
  to fail.

A job-failure alert catches BUG-1014 but not BUG-1086 (a missing CronJob never
fails). A freshness check against a **declared** list of databases catches
both. This is why the alerting has two tiers.

## Declared backup list

The source of truth for "what SHOULD be backed up" is the `DECLARED` config
list in `grafana/backup-freshness-exporter.py`, **not** the CronJobs that
happen to exist. A database that is declared but has no backup objects in R2
exports `+Inf` and fires the freshness alert — this is what catches BUG-1086.

| app         | schedule (UTC)     | backup CronJob today               | freshness threshold (2x) |
| ----------- | ------------------ | ---------------------------------- | ------------------------ |
| coach       | nightly 07:00      | `coach/postgres-backup`            | 48h                      |
| equal-risk  | nightly 05:00      | `equal-risk/postgres-backup`       | 48h                      |
| glossary    | nightly (intended) | **none** — alerts until one exists | 48h                      |
| omnipendium | nightly (intended) | **none** — alerts until one exists | 48h                      |

glossary and omnipendium each run a postgres workload (`glossary/postgres`,
`omnipendium/omnipendium-db`) with no backup job. They are declared on purpose:
`BackupFreshnessStale` will fire for both from the first exporter run and stay
firing until a backup job lands objects in R2. That is the BUG-1086 case doing
its job, not a false alarm — silence the alert by creating the backup, not by
removing the entry.

### R2 layout (verified 2026-09-06)

Bucket `homelab`. The postgres backup jobs upload date-partitioned objects:

```text
backups/cluster/homelab-k3s/rebuild-exports/<YYYY-MM-DD>/postgres/<app>/<app>-<ts>.pgdump
                                                                          <app>-<ts>.pgdump.sha256
                                                                          <app>-<ts>.manifest.json
```

There is no per-app prefix to list, so the exporter lists `rebuild-exports/`
once (recursive, a few dozen objects) and groups by the `postgres/<app>/` path
segment. Verified with the Cloudflare API: coach and equal-risk have objects
under this layout through 2026-09-06; glossary and omnipendium have none.

Other backup targets exist but are **not** in this declared list yet:

- **k3s datastore** (`kube-system/k3s-datastore-backup`) — SQLite → Garage S3
  (not R2), nightly. Would need a Garage (not R2) freshness check.
- **yt-dlp media** (`yt-dlp/yt-dlp-r2-backup`) — PVC → R2, weekly. Would need a
  14-day threshold.

## Two alert tiers

### Secondary — job failure (`backup.job.rules`)

kube-state-metrics signals on the backup CronJobs themselves. Cheap and fast,
but blind to a missing CronJob. Both rules match **by name** — every postgres
backup CronJob is called `postgres-backup` in its app's namespace, and its Jobs
are `postgres-backup-<id>`.

- `BackupCronJobFailed` —
  `kube_job_status_failed{job_name=~"(postgres-backup|k3s-datastore-backup|yt-dlp-r2-backup)-.*"} > 0`, restricted with
  `and on (namespace, job_name) (time() - kube_job_status_start_time < 86400)`
  to Jobs started in the last 24h. Failed Jobs are retained by
  `failedJobsHistoryLimit` (four are visible in Prometheus right now, the oldest
  from 2026-08-26), so without the window a long-recovered failure would fire
  forever.
- `BackupCronJobLastSuccessStale` —
  `time() - kube_cronjob_status_last_successful_time{cronjob=~"postgres-backup|k3s-datastore-backup"} > 172800`.
  Fires when a backup CronJob has not succeeded in 48h (2x nightly).

Why not a `backup: "true"` label? kube-state-metrics only exposes Kubernetes
labels on `kube_*_labels` series (as `label_backup`), and only when
`metricLabelsAllowlist` is configured in the chart values — it is not. A
`{backup="true"}` matcher on `kube_job_status_failed` would match nothing and
fail silently. Name matching works against the cluster as it is.

### Primary — freshness (`backup.freshness.rules`)

A freshness gauge exported by `grafana/backup-freshness-exporter.py`, which
lists R2 objects for each declared database and reports the age of the newest
backup. Catches both a failing job and a missing job.

- `BackupFreshnessStale` —
  `backup_freshness_seconds > backup_freshness_max_age_seconds`. The newest R2
  backup is older than 2x its interval (or absent → `+Inf`).
- `BackupFreshnessExporterDown` — failed listing, failed scrape, absent target
  or absent health metric. For a listing error (credentials or network), per-app
  series are withheld in this state so `BackupFreshnessStale` does not also
  fire four criticals for what is one exporter problem.
- `BackupFreshnessExporterStale` —
  `time() - backup_freshness_last_run_timestamp_seconds > 43200`. The exporter
  has not run in 12h (its refresh worker is stalled), even if the HTTP endpoint still responds.

## How the freshness gauge is produced

`grafana/backup-freshness-exporter.py`:

1. Runs one recursive `rclone lsjson` of
   `r2:homelab/backups/cluster/homelab-k3s/rebuild-exports/`.
2. For each `DECLARED` entry, takes the newest `ModTime` among nonempty `.pgdump` objects whose
   path contains `/postgres/<app>/`; checksum and manifest sidecars do not count.
3. Emits Prometheus text format:
   - `backup_freshness_seconds{app="..."}` — age of the newest backup (`+Inf`
     if none).
   - `backup_freshness_max_age_seconds{app="..."}` — 2x the declared interval.
   - `backup_freshness_up` — 1 if the listing succeeded.
   - `backup_freshness_last_run_timestamp_seconds` — unix time of the run.

### Credentials and deployment

The exporter reads `grafana/backup-freshness-r2-credentials`. Credentials are
provisioned separately from an owner-only JSON file, outside this repository:
`~/code/keys/homelab-backup-freshness-r2.json`. Its keys are the
`RCLONE_CONFIG_R2_*` environment variable names and its values are plain strings.
On 2026-09-06 the user authorized exporting the existing `yt-dlp/r2-credentials`
values to this file. The file is mode 600; the provisioning script never prints
values and sends the Secret through stdin with server-side apply. Treat both the
local file and Kubernetes Secret as credential copies when rotating R2 access.

```bash
python3 grafana/provision-backup-credentials.py --dry-run
python3 grafana/provision-backup-credentials.py
bash grafana/deploy-backup-alerts.sh --dry-run
bash grafana/deploy-backup-alerts.sh
```

An optional positional path selects a different credential JSON file. Reprovision
and redeploy after rotation so the pod reloads its environment. The existing R2
credentials have backup write access; this exporter only lists objects. A separate
read-only R2 key would reduce its access and can be supplied through the same file.

### Scheduling and scrape health

`manifests/backup-freshness-exporter.yaml` runs a Deployment with an HTTP metrics
endpoint and ServiceMonitor. No node-exporter hostPath or persistent volume is
needed. An init container copies the existing rclone 1.68 binary into an emptyDir;
the Python container refreshes R2 every five minutes, with a 120-second subprocess
deadline. It runs without a Kubernetes API token, as non-root, with read-only root
filesystems and bounded resources. HTTP probes check process availability, not R2
availability, so a credential outage remains visible instead of restart-looping.

The refresh worker atomically replaces its sample after each completed attempt.
Failed listings withhold database samples and report `backup_freshness_up 0`.
The HTTP handler computes ages from object timestamps, so age keeps increasing
between refreshes; the last-run timestamp changes only after a listing attempt.
The Down rule covers failed listing, failed scrape, missing target and missing
health metric. The Stale rule separately detects a serving process whose refresh
worker has not completed an attempt for 12 hours.

### Routing

`manifests/backup-alert-routing.yaml` routes `alertgroup=backup` through the same
`alertmanager-slack-webhook` Secret and `#cron-homelab` channel as storage alerts,
including resolved notifications. `deploy-backup-alerts.sh` applies this route and
its namespace matcher strategy directly, without any Helm upgrade. The main
Grafana deploy script invokes this standalone script too.

### Verification

```bash
python3 grafana/test-backup-freshness-exporter.py
uv run --with pyyaml python grafana/test-backup-alerts.py /path/to/promtool
```

The exporter tests cover absent backups, sidecars, empty dumps, failed listings,
malformed data, timeouts and a frozen refresh sample. Prometheus rule tests cover
production-duration missing-target and stale-worker detection. Live drill evidence
and remaining manual verification are recorded in `backup-alert-drill-2026-09-06.md`.
