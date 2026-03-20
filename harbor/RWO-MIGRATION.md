---
# Harbor RWX to RWO Migration

This runbook migrates Harbor's `registry` and `jobservice` data from the current
Longhorn RWX volumes to new Longhorn RWO volumes.

Use this when the live Harbor workloads are healthy, but you want to remove the
Longhorn share-manager/NFS dependency for the single-replica `registry` and
`jobservice` components.

## Why this is a migration

Kubernetes does not allow changing a bound PVC access mode from
`ReadWriteMany` to `ReadWriteOnce`. The current claims are:

- `harbor-registry`
- `harbor-jobservice`

So the safe path is:

1. Create new RWO PVCs.
2. Copy the existing data into them.
3. Point Harbor at the new claims with `existingClaim`.
4. Switch Harbor's update strategy to `Recreate`.

## Downtime expectation

Expect a short maintenance window. Copying can happen while Harbor is still up,
but do a final sync after scaling down `registry` and `jobservice` so the data
is consistent.

## Proposed target claims

- `harbor-registry-rwo` - 50Gi, `ReadWriteOnce`, `longhorn-harbor`
- `harbor-jobservice-rwo` - 1Gi, `ReadWriteOnce`, `longhorn-harbor`

## Pre-flight

1. Confirm Harbor is healthy.
2. Take Longhorn snapshots/backups of `harbor-registry` and `harbor-jobservice`.
3. Make sure no large image push, replication, or garbage collection job is
   running during the migration.

## 1. Create the new RWO PVCs

Apply this once:

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: harbor-registry-rwo
  namespace: harbor
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 50Gi
  storageClassName: longhorn-harbor
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: harbor-jobservice-rwo
  namespace: harbor
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 1Gi
  storageClassName: longhorn-harbor
```

## 2. Seed-copy the data

Create a temporary pod that mounts both the old and new claims, then copy the
data across.

Registry copy pod:

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: harbor-registry-migrator
  namespace: harbor
spec:
  restartPolicy: Never
  containers:
    - name: rsync
      image: alpine:3.20
      command: ["/bin/sh", "-c"]
      args:
        - apk add --no-cache rsync && rsync -aHAX --delete /source/ /dest/
      volumeMounts:
        - name: source
          mountPath: /source
        - name: dest
          mountPath: /dest
  volumes:
    - name: source
      persistentVolumeClaim:
        claimName: harbor-registry
    - name: dest
      persistentVolumeClaim:
        claimName: harbor-registry-rwo
```

Jobservice copy pod:

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: harbor-jobservice-migrator
  namespace: harbor
spec:
  restartPolicy: Never
  containers:
    - name: rsync
      image: alpine:3.20
      command: ["/bin/sh", "-c"]
      args:
        - apk add --no-cache rsync && rsync -aHAX --delete /source/ /dest/
      volumeMounts:
        - name: source
          mountPath: /source
        - name: dest
          mountPath: /dest
  volumes:
    - name: source
      persistentVolumeClaim:
        claimName: harbor-jobservice
    - name: dest
      persistentVolumeClaim:
        claimName: harbor-jobservice-rwo
```

## 3. Quiesce Harbor and do a final sync

Scale down the writers before the final copy:

```bash
kubectl scale deploy/harbor-jobservice -n harbor --replicas=0
kubectl scale deploy/harbor-registry -n harbor --replicas=0
kubectl wait --for=delete pod -l component=jobservice -n harbor --timeout=5m
kubectl wait --for=delete pod -l component=registry -n harbor --timeout=5m
```

Run the two migrator pods again so the destination PVCs get a final clean sync.

## 4. Update Harbor values

Change `harbor/harbor-values.yaml` as follows:

- `persistence.persistentVolumeClaim.registry.existingClaim: harbor-registry-rwo`
- `persistence.persistentVolumeClaim.registry.accessMode: ReadWriteOnce`
- `persistence.persistentVolumeClaim.jobservice.jobLog.existingClaim: harbor-jobservice-rwo`
- `persistence.persistentVolumeClaim.jobservice.jobLog.accessMode: ReadWriteOnce`
- `updateStrategy.type: Recreate`

Note: Harbor uses a single `harbor-jobservice` PVC for `/var/log/jobs`. If you
also decide to move `scanDataExports`, verify first whether the chart version in
use actually materializes a separate PVC for that path in this deployment.

## 5. Redeploy Harbor

Redeploy with:

```bash
./harbor/deploy-harbor.sh
```

Then verify:

```bash
kubectl get pods -n harbor
kubectl get pvc -n harbor
helm status harbor -n harbor
```

## 6. Cleanup after validation

After Harbor is stable and you have verified image push/pull, cleanup can be:

- keep the old RWX PVCs for rollback for a while, or
- delete the old RWX PVCs once you are confident the migration is complete.

## Rollback

If Harbor fails after switching to the new claims:

1. Scale Harbor down again.
2. Put the old claim names back in `harbor/harbor-values.yaml`.
3. Redeploy with `./harbor/deploy-harbor.sh`.
4. Investigate before deleting any old PVCs.
