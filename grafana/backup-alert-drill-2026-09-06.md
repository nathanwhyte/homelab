# Backup alert deployment and drill — 2026-09-06

Phase 3 deployed the exporter, ServiceMonitor, five production alerts and an
AlertmanagerConfig for `#cron-homelab`. No Helm upgrade was performed. The previous
implementation had a script but no scheduled execution or scrape wiring; this
deployment closes that gap with an HTTP exporter refreshing R2 every five minutes.

## Observed results

| Check | Evidence |
| --- | --- |
| Declared coverage | All four database samples present; coach and equal-risk have recent nonempty dumps, glossary and omnipendium report `+Inf` |
| Exporter health | `backup_freshness_up = 1`, Deployment restored to one available replica |
| Production rules | All five expressions evaluate successfully; rule health `ok` |
| Credential failure | One copy of coach's actual backup Job used deliberately invalid R2 upload credentials; dump succeeded, upload failed `Unauthorized` at 17:17:45Z |
| Real job-failure timer | `BackupCronJobFailed` fired by 17:23:51Z after its unchanged five-minute pending period and reached `grafana/backup-alert-routing/slack-homelab` |
| Missing job coverage | Glossary and omnipendium have no backup CronJobs or dumps; their real samples triggered `BackupFreshnessStaleDrill` through the same receiver |
| Stopped exporter | Scaled only the new exporter to zero; production Down entered pending, one-minute `BackupFreshnessExporterDownDrill` fired and reached the receiver |
| Frozen worker | A credential-free HTTP fixture served a timestamp older than 12h; `BackupFreshnessExporterStaleDrill` fired through the receiver |
| Restoration | Exporter returned to one available replica; production Down, ExporterStale and CronJobFailed were inactive at 17:26:03Z |
| Cleanup | Failed test Job, companion PrometheusRule, fixture Deployment, Service, ServiceMonitor and ConfigMap all deleted |
| Delivery counters | Slack notification counter rose from 3 to 8 by 17:26:03Z; all failure counters remained zero |

The companion rules carried `drill=backup-alert-drill`, names ending in `Drill`,
and a `[1-minute drill]` summary. Production delays were unchanged: five minutes
for failed Jobs, 30 minutes for exporter Down, and one hour for freshness/Stale.
The frozen-worker companion selected the separate fixture's job label. Other
companion conditions used real exporter samples. A finally block bounded the
exporter stop test and restored the Deployment even if observation failed.

The failed-upload Job was named `coach/postgres-backup-alert-drill`, had no
retries, a ten-minute deadline, and a distinct backup app name. The scheduled
CronJob and its credentials were not edited. No successful test upload occurred.

## Limits and outstanding verification

- The missing-job drill used the two real, already-missing jobs. No functioning
  database's CronJob was removed. The plan's literal removal-and-aging test remains
  unperformed; hermetic tests independently prove declared entries produce `+Inf`
  without any Kubernetes job series.
- Full 30-minute Down and 12h-plus-one-hour Stale timing were tested with promtool's
  simulated clock. Live companion tests establish signal evaluation and routing,
  not an uninterrupted wall-clock test of those production durations.
- The user's Slack screenshot confirms all four firing alert names and resolved
  messages for freshness, exporter Down and the failed Job. The user also
  confirmed receipt of the final frozen-worker message: `[1-minute drill]
  Backup freshness exporter has not run in 12h`. All test alerts had cleared
  from Alertmanager by 17:29:36Z, with ten notifications and no errors.
- A healthy-night soak is still pending. Glossary and omnipendium are expected to
  alert after the production one-hour delay until backups are implemented; they
  are known coverage gaps, not noise to silence by deleting declarations.
- k3s has Job-failure and last-success coverage, but no Garage-object freshness
  check. Weekly media backup has Job-failure coverage, not object freshness.

## Versions and credentials

| Release | Chart version before/after | Revision before/after |
| --- | --- | --- |
| kube-prometheus-stack | 87.17.0 / 87.17.0 | 20 / 20 |
| k8s-monitoring | 3.8.4 / 3.8.4 | 2 / 2 |
| loki | 7.1.0 / 7.1.0 | 14 / 14 |

The user authorized storing the existing R2 backup credentials in
`~/code/keys/homelab-backup-freshness-r2.json` (verified mode 600). The provisioning
script reads this file and supplies the exporter Secret without printing values.
No credential file is tracked in the repository.

Raw rule states, receiver selection, counters and final verification are in
[`backup-alert-drill-2026-09-06.json`](backup-alert-drill-2026-09-06.json).
Local verification passed: five exporter tests, seven promtool scenarios,
shell syntax/shellcheck, YAML validation and scoped repository hooks.
