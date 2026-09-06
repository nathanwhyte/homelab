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
  `kube_job_status_failed{job_name=~"postgres-backup-.*"} > 0`, restricted with
  `and on (namespace, job_name) (time() - kube_job_status_start_time < 86400)`
  to Jobs started in the last 24h. Failed Jobs are retained by
  `failedJobsHistoryLimit` (four are visible in Prometheus right now, the oldest
  from 2026-08-26), so without the window a long-recovered failure would fire
  forever.
- `BackupCronJobLastSuccessStale` —
  `time() - kube_cronjob_status_last_successful_time{cronjob="postgres-backup"} > 172800`.
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
- `BackupFreshnessExporterDown` — `backup_freshness_up == 0`. The exporter
  could not list R2 (rclone missing, credentials broken, network). The per-app
  series are withheld in this state so `BackupFreshnessStale` does not also
  fire four criticals for what is one exporter problem.
- `BackupFreshnessExporterStale` —
  `time() - backup_freshness_last_run_timestamp_seconds > 43200`. The exporter
  has not run in 12h (its CronJob is suspended/failing/deleted), so the gauges
  are frozen.

## How the freshness gauge is produced

`grafana/backup-freshness-exporter.py`:

1. Runs one recursive `rclone lsjson` of
   `r2:homelab/backups/cluster/homelab-k3s/rebuild-exports/`.
2. For each `DECLARED` entry, takes the newest `ModTime` among objects whose
   path contains `/postgres/<app>/`.
3. Emits Prometheus text format:
   - `backup_freshness_seconds{app="..."}` — age of the newest backup (`+Inf`
     if none).
   - `backup_freshness_max_age_seconds{app="..."}` — 2x the declared interval.
   - `backup_freshness_up` — 1 if the listing succeeded.
   - `backup_freshness_last_run_timestamp_seconds` — unix time of the run.

### Credentials

The exporter uses rclone with a remote named `r2`, configured from
`RCLONE_CONFIG_R2_*` environment variables. The postgres backup jobs do **not**
use rclone — they run a boto3 script fed by `S3_PROVIDER_*` keys from each
app's `r2-backup-credentials` secret — so that secret is the wrong shape. The
weekly `yt-dlp/yt-dlp-r2-backup` CronJob does use rclone, via `envFrom` on the
`yt-dlp/r2-credentials` secret, which carries exactly the seven
`RCLONE_CONFIG_R2_*` keys the exporter needs. Copy that secret into the
namespace that runs the exporter (or export the same variables on a host).

### Scheduling

The script is meant to run periodically and feed a node-exporter textfile
collector (node-exporter already runs with `--collector.textfile`). The
wiring is a follow-up: a CronJob (e.g. every 6h) that runs the script and
writes its output into a directory shared with node-exporter's textfile
collector, which requires mounting that directory into both the CronJob and
node-exporter. The `BackupFreshnessExporterStale` rule guards against the
exporter silently stopping (a frozen textfile would otherwise keep serving
stale-but-fresh-looking values).

## Applying

`grafana/deploy-grafana.sh` applies `manifests/backup-alerts.yaml` alongside
the other grafana-namespace manifests. The PrometheusRule needs no extra
labels — the cluster Prometheus has empty rule selectors (same as
`garage/manifests/garage-alerts.yaml`).

## Routing

Alerts carry `alertgroup: backup` and route to the existing `slack-homelab`
receiver, which posts to `#cron-homelab` via the `alertmanager-slack-webhook`
secret (key `api-url`) in the `grafana` namespace. The route matcher is added
in `grafana/helm/kube-prometheus-stack-values.yaml`
(`alertmanager.config.route.routes`). No new secret is created — the existing
one is referenced.
