# Upgrading the Longhorn chart

`deploy-longhorn.sh` only reapplies values at the deployed chart version
(`scripts/helm-deploy.md`). A chart upgrade is this separate, deliberate run with
an explicit `--version`. Longhorn cannot be downgraded once the upgrade finishes,
so read the target's release notes and important-notes page before starting.

## 1.12.0 to 1.12.1 (BUG-1232)

**Why.** v1.12.0 resolves the host `iscsid` PID once and keeps it. Unattended
upgrades restart `iscsid` on every node (timmy 2026-10-15 06:17:58 CDT), after
which every host-namespace call from that node's
instance-manager fails with `nsenter: cannot open /host/proc/<pid>/ns/mnt`.
Attached volumes keep serving I/O, so nothing alerts, but a PVC expansion stalls
at `expansionRequired=true` until the instance-manager is restarted. That is
upstream longhorn/longhorn#13334, fixed in v1.12.1 (backport #13368,
go-common-libs#192 re-resolves the PID and retries).

**What the upgrade changes** (`helm diff`, 2026-10-17):

- Images to v1.12.1 (manager, engine, instance-manager, UI, share-manager,
  backing-image-manager), support-bundle-kit v0.0.92, CSI provisioner v5.3.0,
  resizer v2.2.1, snapshotter v8.6.0.
- CRD schema updates, and two new CRDs (`shards`, `shardgroups`).
- Two drift fixes that converge the release on what is already live: the
  `replica-replenishment-wait-interval` default setting (live value already
  `1800`) and the Ingress middleware (release still records the retired
  `longhorn-lan-only`; live is `longhorn-basicauth`).
- No NetworkPolicies. 1.12.1's new `networkPolicies.restrictInternalTraffic`
  defaults to true and would render six; `longhorn-values.yaml` sets it false
  because the manager policy would block Prometheus's scrape from `grafana`.

### Steps

1. Preflight: every volume `healthy` or detached, nothing rebuilding.

   ```bash
   kubectl -n longhorn-system get volumes.longhorn.io -o custom-columns='N:.metadata.name,STATE:.status.state,ROBUST:.status.robustness' | rg -v 'healthy|detached'
   ```

2. Diff, and check it matches the list above.

   ```bash
   helm repo update longhorn
   helm diff upgrade longhorn longhorn/longhorn -n longhorn-system --version 1.12.1 \
     -f longhorn/longhorn-values.yaml --suppress-secrets
   ```

3. Upgrade. The chart's `longhorn-pre-upgrade` job checks the path first; a
   failure leaves the release unchanged and records a `FailedUpgradePreCheck`
   event.

   ```bash
   helm upgrade longhorn longhorn/longhorn -n longhorn-system --version 1.12.1 \
     -f longhorn/longhorn-values.yaml
   kubectl -n longhorn-system rollout status ds/longhorn-manager --timeout=10m
   kubectl -n longhorn-system get engineimages.longhorn.io
   ```

   Wait for the v1.12.1 engine image to show `deployed`. Each node gets a new
   instance-manager pod; the old one stays while it still hosts a process.

4. Live-upgrade the attached volumes' engines.

   ```bash
   longhorn/upgrade-engines.sh v1.12.1 --dry-run
   longhorn/upgrade-engines.sh v1.12.1
   ```

5. Check:

   - `helm list -n longhorn-system` shows `longhorn-1.12.1`.
   - `kubectl get networkpolicies -n longhorn-system` returns nothing.
   - Prometheus target `longhorn-system/longhorn-prometheus-servicemonitor` is up,
     and `longhorn_volume_robustness` reports every attached volume.
   - Every volume reports `currentImage` v1.12.1 or is detached.

### Which volumes get the fix

A live engine upgrade re-creates replicas in the new instance-manager, but the
engine process stays in the old instance-manager pod until the volume detaches
(Longhorn docs, "Instance Manager Pods During Upgrade"). The fix spans the engine
and the instance-manager, so a volume is fully covered only once its workload
has restarted onto the new pod. Before expanding a volume whose engine is still
in a pre-upgrade instance-manager, restart its workload first.

### Verify the fix (optional)

On one node, create a scratch PVC and a pod pinned there with `nodeName`, so
its engine starts in the new instance-manager. Then restart `iscsid` on that
node (`sudo systemctl restart iscsid`; this is what unattended upgrades do),
patch the PVC one GiB larger, and confirm the filesystem grows. Delete the
scratch pod and PVC afterwards.
