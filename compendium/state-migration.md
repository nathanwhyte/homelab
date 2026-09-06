# Compendium sync state v2 migration

BUG-1101 replaces the existing `compendium-sync-state` PVC instead of trying to
mutate it. Kubernetes does not permit changing a bound PVC's
`spec.storageClassName` or access mode. The replacement
`compendium-sync-state-v2` uses `longhorn-compendium-sync`: two replicas on the
`ssd` disk tier, with a `Retain` reclaim policy.

Two is intentional. A pre-change live read confirmed manu, timmy, and wemby all
have schedulable `ssd`-tagged Longhorn disks (`allowScheduling=true`,
`evictionRequested=false`). Two replicas are fully placeable on distinct nodes
and keep a healthy copy through one node loss. A third copy is unnecessary for
the tiny, reconstructible last-synced JSON and would add storage/write cost
without improving the single-node-failure objective.

## Migration (operator-run; do not apply automatically)

Run these from the repository root with the intended kubectl context explicit.
The old claim remains intact and is mounted read-only by the migration Job.

1. Stop and verify all writers are terminal. Do not delete an active Job:

   ```sh
   kubectl --context=tailnet -n compendium get jobs -l app=compendium-sync
   kubectl --context=tailnet -n compendium get pods -l app=compendium-sync
   ```

2. Create the dedicated class and replacement claim. This does **not** modify
   the existing claim:

   ```sh
   kubectl --context=tailnet apply \
     -f longhorn/compendium-sync-storage-class.yaml
   kubectl --context=tailnet apply -f compendium/state-pvc.yaml
   kubectl --context=tailnet -n compendium wait \
     --for=jsonpath='{.status.phase}'=Bound pvc/compendium-sync-state-v2 \
     --timeout=120s
   ```

3. Copy and byte-compare the state, then inspect the two printed SHA-256 values;
   they must match:

   ```sh
   kubectl --context=tailnet apply -f compendium/state-migration-job.yaml
   kubectl --context=tailnet -n compendium wait \
     --for=condition=complete job/compendium-sync-state-migrate-v1-to-v2 \
     --timeout=600s
   kubectl --context=tailnet -n compendium logs \
     job/compendium-sync-state-migrate-v1-to-v2
   ```

4. Only after the matching hashes are recorded, dispatch the updated Job. It
   mounts v2. Keep the old PVC for at least one successful changed sync and one
   planned node-failover test. Do not delete the old PVC or the completed
   migration Job as part of this change: `cluster-sync.sh` uses that terminal
   Job condition as the cutover gate while the legacy claim exists.

## Rollback

1. Stop and verify all sync Jobs are terminal as in migration step 1.
2. Copy the newest v2 baseline back to the legacy claim so rollback cannot
   regress the last-synced SHA. The rollback Job saves the old destination as
   `compendium-sync-state.json.pre-rollback`, then byte-compares and prints both
   hashes:

   ```sh
   kubectl --context=tailnet apply -f compendium/state-rollback-job.yaml
   kubectl --context=tailnet -n compendium wait \
     --for=condition=complete job/compendium-sync-state-rollback-v2-to-v1 \
     --timeout=600s
   kubectl --context=tailnet -n compendium logs \
     job/compendium-sync-state-rollback-v2-to-v1
   ```

3. Revert the sync Job template and `--status` reader to claim
   `compendium-sync-state`, render a new Job, and confirm one successful
   `--changed` sync. Do not delete v2: its StorageClass retains the PV, but
   preserving the PVC makes a forward retry simpler and avoids manual PV
   recovery.

Both migration Jobs are deliberately separate from normal bootstrap and have
zero retries. They are operator-controlled data movement, not recurring sync
workloads.
