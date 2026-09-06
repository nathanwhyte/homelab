# Longhorn and PVC workload alerting

The ServiceMonitor scrapes each `longhorn-backend` endpoint on its named
`manager` port every 60 seconds. Prometheus evaluates the rules every 30 seconds.
Metric names and labels were checked against the running Longhorn v1.12.0
managers and then queried in Prometheus before the rules were authored.

## Deployment

Run from any working directory:

```bash
bash /path/to/homelab/longhorn/deploy-storage-alerts.sh --dry-run
bash /path/to/homelab/longhorn/deploy-storage-alerts.sh
```

This applies the monitor, rules and storage AlertmanagerConfig, then re-asserts
`prom-alertmanager`'s matcher strategy as
`OnNamespaceExceptForAlertmanagerNamespace`. It performs no Helm operations.
The strategy is already set in the stack's Helm values, so the patch is an
idempotent safety net for clusters where the values have not been re-applied,
not a new requirement. The Grafana deploy script calls this script after
installing the monitoring CRDs.

The routing object lives in `grafana` so it can reference the existing
`alertmanager-slack-webhook` Secret's `api-url` key. It matches
`alertgroup="storage"` across workload namespaces and sends firing and resolved
notifications to `#cron-homelab`, using the same webhook as the power alert.
Other namespaces retain namespace-scoped AlertmanagerConfig routing.
The installed CRD must support this matcher strategy; server dry-run validates it.

As of the initial deployment, the existing Garage rules do **not** route to
Slack: the previous loaded configuration matched only `WembyOnBattery` and sent
everything else to `null`. These changes add storage routing without claiming
that the Garage or undeployed backup routing is verified.

Allow approximately two minutes for Kubernetes volume projection, configuration
reload and the first scrape. Check the loaded configuration, not just the CRs:

```bash
kubectl -n longhorn-system get servicemonitor longhorn
kubectl -n longhorn-system get prometheusrule longhorn-alerts
kubectl -n grafana port-forward svc/prom-prometheus 19090:9090
```

In another terminal:

```bash
curl -fsSG http://127.0.0.1:19090/api/v1/query \
  --data-urlencode 'query=up{job="longhorn-backend"}'
curl -fsSG http://127.0.0.1:19090/api/v1/query \
  --data-urlencode 'query=count(longhorn_volume_capacity_bytes)'
curl -fsS http://127.0.0.1:19090/api/v1/rules
```

The baseline on 2026-09-06 was three successful targets and 32 volumes, including
four intentionally detached volumes. Native metrics carry `pvc_namespace` and
`pvc`; their scrape `namespace` is `longhorn-system`.

## Signals and thresholds

| Alert                             | Signal                                                    | Hold      |
| --------------------------------- | --------------------------------------------------------- | --------- |
| LonghornVolumeFaulted             | `longhorn_volume_robustness{state="faulted"} == 1`        | Immediate |
| LonghornVolumeDegraded            | Labeled degraded robustness                               | 15m       |
| LonghornVolumeSpaceHigh           | Actual replica bytes / configured bytes > 85%             | 15m       |
| LonghornReplicaFailed             | Replica state `error`                                     | 5m        |
| LonghornVolumeRebuildChurn        | Six changes in rebuilding replica count in 30m            | Immediate |
| LonghornEngineStateChurn          | Four changes in engine running state in 30m               | Immediate |
| LonghornInstanceManagerRestarting | At least three container restarts in 30m                  | Immediate |
| LonghornMetricsUnavailable        | Successful targets fewer than desired manager pods        | 5m        |
| StoragePodNotReady                | Pending/Running PVC-backed pod not Ready                  | 15m       |
| StoragePVCNotBound                | Claim Pending or Lost                                     | 10m       |
| StorageContainerRestarting        | PVC-backed container restarts at least three times in 30m | Immediate |

The 15-minute degradation hold is an initial threshold, not a measured rebuild
SLO. A routine node reboot and rebuild must still be observed before calling the
noise tolerance verified. Large HDD rebuilds may legitimately take longer.

Rebuild churn uses the observed `longhorn_engine_replica_mode` family. Longhorn
marks rebuilding replicas `WO` (write-only). A recording rule fills volumes
without WO replicas with zero; otherwise `changes()` would ignore the gaps.
Six count changes indicate repeated activity, not an exact count of failed
rebuilds. Engine state changes similarly indicate churn, not a precise restart
counter. Both can miss transitions shorter than the scrape interval; inspect
Longhorn events and logs for diagnosis.

`StoragePodNotReady` includes pods blocked in init through their Pending phase
and readiness state. It excludes completed Jobs and joins a deduplicated list of
PVC-backed pods, so mounting two claims does not produce a many-to-many join or
duplicate alert. Workload symptoms can also have application or scheduling
causes; they do not prove a Longhorn failure.

Actual size includes snapshot blocks. A high ratio is a prompt to inspect
filesystem usage, snapshots and retention; it is not authorization to delete
snapshots or a retained model cache. The initial rules identified
`viking/llama-cuda-model-cache` at 91.55%, an intentionally retained warm cache.

### Quick fix: bump the volume size

For volumes with a **small, stable footprint** — an LLM model cache (weights
are fixed once downloaded), a config volume, a slow-growing database — the
cheapest fix for a sustained `LonghornVolumeSpaceHigh` is expansion, not
snapshot surgery. It sidesteps the delete→purge→trim sequence (IMPR-1123) and
never touches data.

Criteria: volume ≤ ~50 GiB, content grows slowly or not at all, and the extra
capacity is cheap (a few GiB). Do NOT use this for volumes that grow
unboundedly (Prometheus, Loki, media) — expansion just delays the next alert
and masks the real problem; right-size or reclaim instead.

Procedure:

1. Confirm expansion is enabled: `kubectl get sc <class> -o jsonpath='{.allowVolumeExpansion}'` → `true`.
2. Pick a target where actualSize/specSize clears 85% with margin (e.g. 9.16 GiB actual → 12 GiB yields 76%).
3. Bump the manifest request (source of truth), then patch the live PVC:
   `kubectl patch pvc <name> -n <ns> --type=merge -p '{"spec":{"resources":{"requests":{"storage":"12Gi"}}}}'`
4. Verify: PVC capacity (`kubectl get pvc`), Longhorn `spec.size`
   (`kubectl -n longhorn-system get volume <vol>`), and the alert clearing.
   For RWO volumes Longhorn attaches transiently to grow the filesystem, then
   detaches again.

Example: 2026-09-06 — `viking/llama-cuda-model-cache` (10 GiB, 91.5%, cached
Qwen3-8B weights) expanded to 12 GiB → 76.3%, alert resolved.

## Monitoring dependency

Both native and kube-state-metrics rules execute inside the same Prometheus,
whose PVC uses Longhorn. **Neither tier works while that Prometheus is down.**
The kube-state-metrics tier is independent of the Longhorn exporter, not of the
rule evaluator. The power alert also uses this evaluator. An external watchdog
or independent evaluator is still required to cover a complete monitoring
outage; this deployment does not provide one.

## Behavioral tests

With `promtool` installed (validated with the same v3.7.3 as live Prometheus):

```bash
uv run --with pyyaml python longhorn/tests/check-alerts.py
```

An explicit executable is supported with `--promtool /path/to/promtool`.
The tests use temporary local storage and cover the hold window, resolution,
transient degradation, detached standby, fault immediacy, duplicate scrapes,
capacity boundary, init-blocked pods, completed Jobs, multiple claims per pod,
Pending/Lost claims, missing scrapes, replica error, engine and rebuild churn,
and restart scoping. They do not prove Slack delivery or real rebuild timing.

## Isolated degradation drill

Only perform this when all production nodes and attached volumes are healthy.
The fixture is never included in deployment. Ensure the `storage-alert-drill`
namespace and StorageClass do not already belong to another test before applying:

```bash
kubectl apply -f longhorn/tests/scratch-volume.yaml
kubectl -n storage-alert-drill wait --for=condition=Ready pod/scratch --timeout=120s
kubectl -n storage-alert-drill get pvc scratch -o jsonpath='{.spec.volumeName}'
```

Use that exact volume name for the remaining operations. Verify its PVC identity
is `storage-alert-drill/scratch`, its replica count is two, its node selector is
empty, and it is attached/healthy with readable `/data/probe`. Record its UID and
resource version. Confirm no Longhorn node has the tag
`storage-alert-drill-no-node`.

Patch **only this volume** to `spec.numberOfReplicas: 3` and
`spec.nodeSelector: [storage-alert-drill-no-node]`, with a JSON Patch `test` of
the recorded resource version to reject concurrent changes. The existing two
replicas keep serving; the requested third cannot schedule, producing real
`degraded` robustness. Reducing the desired replica count alone does not prove
degradation because the smaller desired count can be fully healthy.

Observe the scratch volume's degraded metric, then the actual alert pending
for 15 minutes and firing. Confirm Alertmanager assigns it to
`grafana/storage-alert-routing/slack-homelab` and check Slack for the message.
Monitor all other volumes throughout. Restore the saved selector and desired
count immediately if an unrelated volume degrades or any node goes down.

After firing, restore the same scratch volume to two replicas and an empty node
selector. Verify attached/healthy, readable `/data/probe`, alert resolution in
Prometheus and Alertmanager, and the resolved Slack message. Resolution delivery
can wait for Alertmanager's five-minute group interval.

Delete the fixture resources only after restoring health:

```bash
kubectl delete -f longhorn/tests/scratch-volume.yaml --wait=false
```

Verify the namespace, StorageClass, PV, Longhorn volume and its replica CRs are
gone; `kubectl delete` returning successfully is not sufficient cleanup proof.
This drill tests unmet redundancy, not a physical disk outage or a timed routine
rebuild. Keep the routine-rebuild acceptance item open until observed separately.

## Rollback

Delete `longhorn/alerts.yaml`, `longhorn/servicemonitor.yaml` and
`grafana/manifests/storage-alert-routing.yaml` from the cluster with
`kubectl delete -f`. If no other central AlertmanagerConfig now depends on the
matcher strategy, restore the previous `OnNamespace` strategy and revert the
matching Helm values change. No data volumes need to be changed.

## References

- [Longhorn v1.12 replica rebuilding](https://longhorn.io/docs/1.12.0/advanced-resources/rebuilding/)
- [Prometheus Operator alerting routes](https://prometheus-operator.dev/docs/developer/alerting/)
- [Prometheus Operator API reference](https://prometheus-operator.dev/docs/api-reference/api/)
