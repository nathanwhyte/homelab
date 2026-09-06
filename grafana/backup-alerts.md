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

| app         | schedule | freshness threshold (2x) | R2 prefix (under `homelab/backups/cluster/homelab-k3s/databases/`) |
| ----------- | -------- | ------------------------ | ------------------------------------------------------------------ |
| coach       | nightly  | 48h                      | `coach/`                                                           |
| equal-risk  | nightly  | 48h                      | `equal-risk/`                                                      |
| glossary    | nightly  | 48h                      | `glossary/`                                                        |
| omnipendium | nightly  | 48h                      | `omnipendium/`                                                     |

> The exact R2 prefixes must match the upload target of each database's backup
> job. Confirm them against the actual backup CronJobs before relying on this
> list — the list is the contract, the prefixes are the wiring.

Other backup targets exist but are **not** in this declared list yet:

- **k3s datastore** (`kube-system/k3s-datastore-backup`) — SQLite → Garage S3
  (not R2), nightly. Would need a Garage (not R2) freshness check.
- **yt-dlp media** (`yt-dlp/yt-dlp-r2-backup`) — PVC → R2, weekly. Would need a
  14-day threshold.

## Two alert tiers

### Secondary — job failure (`backup.job.rules`)

kube-state-metrics signals on the backup CronJobs themselves. Cheap and fast,
but blind to a missing CronJob.

- `BackupCronJobFailed` — `kube_job_status_failed{backup="true"} == 1`. Fires
  immediately when a backup Job fails (the BUG-1014 signature).
- `BackupCronJobLastSuccessStale` —
  `time() - kube_cronjob_status_last_successful_time{backup="true"} > 172800`.
  Fires when a backup CronJob has not succeeded in 48h (2x nightly).

Backup CronJobs must carry the label `backup: "true"` for these rules to match.
The postgres backup CronJobs (coach, equal-risk, glossary, omnipendium) need
this label added at apply time.

### Primary — freshness (`backup.freshness.rules`)

A freshness gauge exported by `grafana/backup-freshness-exporter.py`, which
lists R2 objects for each declared database and reports the age of the newest
backup. Catches both a failing job and a missing job.

- `BackupFreshnessStale` —
  `backup_freshness_seconds > backup_freshness_max_age_seconds`. The newest R2
  backup is older than 2x its interval (or absent → `+Inf`).
- `BackupFreshnessExporterDown` — `backup_freshness_up == 0`. The exporter
  could not list R2 (rclone missing, credentials broken, network).
- `BackupFreshnessExporterStale` —
  `time() - backup_freshness_last_run_timestamp_seconds > 43200`. The exporter
  has not run in 12h (its CronJob is suspended/failing/deleted), so the gauges
  are frozen.

## How the freshness gauge is produced

`grafana/backup-freshness-exporter.py`:

1. Reads the `DECLARED` list (app, R2 prefix, interval).
2. For each app, runs `rclone lsjson r2:<prefix> --files-only --recursive` and
   takes the newest `ModTime`.
3. Emits Prometheus text format:
   - `backup_freshness_seconds{app="..."}` — age of the newest backup (`+Inf`
     if none).
   - `backup_freshness_max_age_seconds{app="..."}` — 2x the declared interval.
   - `backup_freshness_up` — 1 if the run succeeded.
   - `backup_freshness_last_run_timestamp_seconds` — unix time of the run.

R2 credentials come from the environment exactly like the `r2-credentials`
secret (`RCLONE_CONFIG_R2_*`), so the script runs unmodified in a pod that
mounts that secret.

### Scheduling

The script is meant to run periodically and feed a node-exporter textfile
collector (node-exporter already runs with `--collector.textfile`). The
wiring is a follow-up: a CronJob (e.g. every 6h) that runs the script and
writes its output into a directory shared with node-exporter's textfile
collector, which requires mounting that directory into both the CronJob and
node-exporter. The `BackupFreshnessExporterStale` rule guards against the
exporter silently stopping (a frozen textfile would otherwise keep serving
stale-but-fresh-looking values).

## Routing

Alerts carry `alertgroup: backup` and route to the existing `slack-homelab`
receiver, which posts to `#cron-homelab` via the `alertmanager-slack-webhook`
secret (key `api-url`) in the `grafana` namespace. The route matcher is added
in `grafana/helm/kube-prometheus-stack-values.yaml`
(`alertmanager.config.route.routes`). No new secret is created — the existing
one is referenced.
