# Storage alert deployment and drill — 2026-09-06

Implementation: `384adf5` on `feat/storage-observability`.

## Reclaim preflight correction

Before deployment, direct filesystem measurements showed the proposed additional
139 GiB reclaim was not supported. All four targets were already within the
requested approximately 5 GiB of their live filesystem usage:

| PVC | Longhorn actual GiB | Filesystem used GiB | Difference GiB |
| --- | ---: | ---: | ---: |
| media | 76.79 | 72.57 | 4.22 |
| copyparty-files | 48.88 | 44.70 | 4.18 |
| llama-model-cache | 50.89 | 48.82 | 2.07 |
| harbor-registry-rwo | 7.28 | 6.23 | 1.05 |

The operator accepted this criterion as satisfied and authorized continuing to
alerting. No purge, trim or snapshot deletion was performed. Snapshot chain
allocation is not equivalent to dead filesystem data.

## Deployment evidence

- All three nodes Ready; 28 production volumes attached/healthy and four
  intentionally detached/unknown before deployment.
- `ServiceMonitor/longhorn` created at 16:16:50 UTC; all three manager targets
  subsequently returned `up=1`. All 32 volume capacity series were present.
- Live v1.12.0 metrics expose one-hot robustness and engine/replica state labels,
  actual/capacity bytes and engine replica modes. Expressions were written after
  these series became queryable.
- `PrometheusRule/longhorn-alerts`: 11 alerts and one recording rule. All 12
  expressions evaluated successfully and both loaded groups reported `health=ok`.
- `AlertmanagerConfig/storage-alert-routing` in `grafana` references the existing
  webhook Secret. The loaded route matches `alertgroup="storage"` without a
  namespace restriction. `amtool` verified native storage and cross-namespace
  workload alerts route to `grafana/storage-alert-routing/slack-homelab`; power
  still routes to `slack-homelab`, and unrelated alerts still route to `null`.
- Deployment used plain `kubectl`, through `deploy-storage-alerts.sh`; no Helm
  release was installed or upgraded. The script also passed server dry-run from
  `/private/tmp`.
- Ten local behavioral fixtures passed with promtool v3.7.3 (matching live).
  Repository hooks, shell syntax, ShellCheck and shfmt checks passed.

The original loaded Alertmanager configuration routed only the power alert to
Slack. Existing Garage rules did not have Slack routing; backup routing was not
yet deployed. This receipt does not claim either was fixed by this phase.

## Scratch-volume drill

The fixture created a 1 GiB PVC with two replicas and a pod on timmy:

- Namespace/PVC: `storage-alert-drill/scratch`
- Volume: `pvc-3e8887c7-530f-4ab2-8377-f3668a0e2e15`
- Volume UID: `4e63185c-24c0-49f3-99c2-94a75dd9dd96`
- Baseline: attached/healthy; `/data/probe` returned `storage-alert-drill`.

After verifying no Longhorn node carried `storage-alert-drill-no-node`, a
resource-version-guarded JSON patch set only this volume's desired replica count
to three and its node selector to that nonexistent tag. The two existing
replicas remained RW; the unschedulable third caused real degraded robustness.
No production replica was failed or removed, and no node was rebooted.

| Observation | UTC time / result |
| --- | --- |
| Degraded alert became pending | 16:23:50.220225908 |
| Firing after the full 15-minute hold | Observed 16:39:05; same `activeAt` and production `for: 15m` |
| Alertmanager receiver and Slack delivery | Correct receiver at 16:39:05; two successful firing sends, zero failures by 16:39:45; user confirmed two Slack notifications |
| Restore original replica count and selector | 16:40:02.725656, UID and resource-version guarded |
| Healthy volume and readable probe after restore | Immediately after restoration; Kubernetes health observed again at 16:40:28 |
| Prometheus / Alertmanager resolution | By 16:41:01; healthy metric = 1, degraded rule inactive, scratch absent from Alertmanager active alerts |
| Scratch resources fully removed | By 16:42:50; namespace, StorageClass, PV, volume and replica CRs absent |
| Resolved notification delivery evidence | By 16:44:32 Slack sends increased from 2 to 3, with all failure counters still zero, after the five-minute group interval |

All post-baseline safety observations retained the original 28 attached/healthy
production volumes and four detached/unknown volumes. No other volume degraded
or faulted during the observed drill.

The sampled API evidence is retained in
[`tests/evidence/2026-09-06-drill.json`](tests/evidence/2026-09-06-drill.json).
The additional successful notification after resolution is consistent with the
resolved message; the counter is integration-wide and does not contain its
payload. The user subsequently confirmed the resolved Slack message and pasted
its text identifying this exact scratch volume, completing the delivery evidence.

## Limits and follow-up

- The user confirmed the two firing notifications and the scratch volume's
  resolved Slack message.
- A routine node reboot/rebuild must still be observed to assess the 15-minute
  hold. The scratch scheduling failure and transient unit fixture do not prove
  that a real large-volume rebuild fits that window.
- The capacity rule also detected `viking/llama-cuda-model-cache` at 91.55%.
  This retained warm cache was left untouched; the alert does not authorize
  deleting it.
- Kube-state-metrics rules are independent of the Longhorn exporter but share
  Prometheus and its Longhorn PVC. They cannot evaluate during a complete
  Prometheus outage. An external watchdog/evaluator remains outside this change.
