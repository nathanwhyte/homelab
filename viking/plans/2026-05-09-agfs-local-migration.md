# OpenViking AGFS Local Backend Migration — Single-Shot Cutover

## Architecture Choice

**Single-instance local AGFS (was "Phase 2A").** Workers and merge are scaled down; the `openviking` Deployment becomes the sole authoritative OpenViking instance on a Longhorn SSD PVC with `agfs.backend: local`. This matches upstream's standalone deployment model and avoids all shard-awareness issues (fs/ls fanout, merged-read routing, content merge).

Workers and coordinator can be scaled back up later if parallel indexing is needed — they just stay on S3 or get a separate local config.

## Why this is the only safe one-shot path

The alternatives require custom coordinator code that doesn't exist yet:

| Blocker | 2A (single instance) | 2B (worker shards) | 2C (real merge) |
|---------|----------------------|--------------------|--------------------|
| fs/ls not shard-aware | Not needed (one instance) | Must patch coordinator.py | Must patch coordinator.py |
| ov-merge can't copy content | Not needed | Must disable merged-read | Must build content copy |
| Temp upload locality | Single writer, no issue | Must route across shards | Must route across shards |
| ConfigMap collision | Dedicated ConfigMap | Must split or override | Must split or override |

2A is the only option that requires zero coordinator code changes.

## Upstream Facts

Sources checked 2026-05-09:

- AGFS is the source of truth; VectorDB is derived. Lost index rebuilds from AGFS; lost AGFS data is unrecoverable.
- `storage.agfs.backend` supports `local`, `s3`, `memory`.
- `local` mounts RAGFS localfs at `/local`, backed by `${storage.workspace}/viking`.
- `storage.transaction.lock_timeout` / `storage.transaction.lock_expire` are the correct lock settings (not `storage.lock_timeout`).
- `upload_mode=local` is per-instance. `upload_mode=shared` stores in AGFS under `viking://upload/...` and is only cross-replica when AGFS itself is shared.
- Upstream supports standalone embedded/local or HTTP server. No native multi-writer sharding.

References: <https://docs.openviking.ai/en/concepts/01-architecture>, <https://docs.openviking.ai/en/concepts/09-transaction>, <https://github.com/volcengine/OpenViking/blob/main/docs/en/guides/01-configuration.md>

## Current State

| Component | Role | AGFS | PVC | ConfigMap |
|-----------|------|------|-----|-----------|
| `ov-coordinator` Deployment | Stateless Python write-proxy. No OV process. | None | None | `ov-coordinator-code` (Python source only) |
| `openviking` Deployment | Merged/read instance behind `ov-merged` Service | S3 (Garage) | `openviking-data` 10Gi | `openviking-config` |
| `ov-worker` StatefulSet (×3) | Sharded write workers | S3 (Garage) | `data-ov-worker-N` 5Gi each | `openviking-config` (shared) |
| `ov-merge` Deployment | URI reindex loop (not content copy) | None | None | None |

Service routing:

- `openviking` Service → `app: openviking` (merged Deployment)
- `ov-merged` Service → `app: openviking` (same Deployment)
- `openviking` external ingress → `openviking:1933` with BasicAuth

Both `openviking` Deployment and `ov-worker` StatefulSet share `openviking-config` ConfigMap. Both initContainers hard-code S3 credential injection.

## Preflight checks

Before any changes, verify the live cluster state matches expectations:

```bash
# Verify the openviking Service points to the merged Deployment (not coordinator)
kubectl -n viking get svc openviking -o jsonpath='{.spec.selector.app}'
# Expected: "openviking"

# If the output is "ov-coordinator", apply the service manifest first:
# kubectl apply -f viking/manifests/openviking-service.yaml
```

## What Changes

### 1. New ConfigMap: `openviking-standalone-config`

Create a dedicated ConfigMap for the standalone `openviking` Deployment. This replaces the shared `openviking-config` for this Deployment only. Workers keep their existing shared ConfigMap (they're being scaled down anyway, but this keeps the config separate for future use).

Changes from `openviking-config`:

- `storage.agfs.backend`: `"local"` (was `"s3"`)
- Remove entire `storage.agfs.s3` block (no S3 credentials needed)
- Add `storage.transaction` block with lock defaults
- Keep `server.workers: 1` (required — multiple uvicorn workers cause RocksDB lock contention)
- Keep everything else identical

```json
{
  "default_account": "default",
  "default_user": "noot",
  "storage": {
    "workspace": "/app/data",
    "transaction": {
      "lock_timeout": 10.0,
      "lock_expire": 300.0
    },
    "vectordb": {
      "backend": "local",
      "name": "context",
      "dimension": 768
    },
    "agfs": {
      "backend": "local"
    }
  },
  "code": {
    "code_summary_mode": "ast"
  },
  "rerank": {
    "threshold": 0.2
  },
  "server": {
    "host": "0.0.0.0",
    "port": 1933,
    "workers": 1,
    "root_api_key": "${API_KEY}"
  },
  "log": {
    "level": "INFO",
    "output": "stdout"
  },
  "embedding": {
    "dense": {
      "provider": "openai",
      "model": "nomic-embed-text-v1.5",
      "api_key": "sk-no-key-required",
      "api_base": "http://embedder-llamacpp.viking.svc.cluster.local:8080/v1",
      "dimension": 768,
      "batch_size": 128
    },
    "max_concurrent": 4
  },
  "vlm": {
    "provider": "litellm",
    "model": "openai/current.gguf",
    "api_key": "sk-no-key-required",
    "api_base": "http://llamacpp-rocm-llm.viking.svc.cluster.local/v1",
    "max_concurrent": 6
  }
}
```

### 2. Make both initContainers backend-agnostic

**`openviking` Deployment** (`openviking-deployment.yaml`): Replace the `config-rewrite` initContainer's unconditional S3 injection:

```python
# BEFORE (crashes if s3 block is absent)
conf["storage"]["agfs"]["s3"]["access_key"] = os.environ["S3_ACCESS_KEY"]
conf["storage"]["agfs"]["s3"]["secret_key"] = os.environ["S3_SECRET_KEY"]

# AFTER (conditional, skips when backend != s3)
agfs = conf.get("storage", {}).get("agfs", {})
if agfs.get("backend", "local") == "s3":
    agfs.setdefault("s3", {})
    agfs["s3"]["access_key"] = os.environ["S3_ACCESS_KEY"]
    agfs["s3"]["secret_key"] = os.environ["S3_SECRET_KEY"]
else:
    agfs.pop("s3", None)
```

**`ov-worker` StatefulSet** (`ov-worker-statefulset.yaml`): Same change in `config-gen`.

Keep S3 env vars mounted in both — they're harmless when unused and simplify rollback.

### 3. Switch `openviking` Deployment to new ConfigMap

In `openviking-deployment.yaml`, change the `config-template` volume:

```yaml
# BEFORE
- name: config-template
  configMap:
    name: openviking-config

# AFTER
- name: config-template
  configMap:
    name: openviking-standalone-config
```

### 4. Apply worker StatefulSet change (before scale-down)

Apply the backend-agnostic initContainer change while workers are still running on S3. This is safe because the conditional logic still injects S3 credentials when `backend: s3` is set.

```bash
kubectl apply -f viking/manifests/ov-worker-statefulset.yaml
```

Wait for rollout:

```bash
kubectl -n viking rollout status statefulset/ov-worker
```

Verify workers are healthy with the new initContainer before proceeding.

### 5. Scale down workers, coordinator, and merge

```bash
kubectl scale statefulset ov-worker --replicas=0 -n viking
kubectl scale deployment ov-coordinator --replicas=0 -n viking
kubectl scale deployment ov-merge --replicas=0 -n viking
```

Do **not** re-apply the worker StatefulSet manifest during or after this step — it declares `replicas: 3` and would scale workers back up.

### 6. Snapshot PVC and clear stale data

The `openviking-data` PVC contains S3-era VectorDB index and AGFS workspace data that must not carry over to local AGFS mode. AGFS is source-of-truth — VectorDB is derived and will be rebuilt during re-ingest.

**Snapshot the PVC** (preserves rollback option with all S3-era data intact):

```bash
kubectl -n viking get pvc openviking-data -o yaml  # verify 10Gi Longhorn SSD
# Longhorn snapshot via UI or kubectl
```

**Clear stale data** from the PVC. The temp-cleanup initContainer only removes lock files and queue.db — it does not clear the VectorDB index or the AGFS workspace. These must be cleared before starting with local AGFS, or the stale VectorDB will reference resources that no longer exist in the empty local AGFS.

The pod is not running after scale-down. Use a one-shot cleanup pod that mounts the same PVC:

```bash
kubectl -n viking run pvc-cleaner --image=busybox:1.37 --restart=Never \
  --overrides='{
    "spec": {
      "containers": [{
        "name": "cleaner",
        "image": "busybox:1.37",
        "command": ["sh", "-c",
          "echo 'Clearing stale VectorDB...'; rm -rf /data/vectordb/*; " +
          "echo 'Clearing stale AGFS workspace...'; rm -rf /data/viking/*; " +
          "echo 'Clearing stale queue...'; rm -rf /data/_system/queue/*; " +
          "echo 'Clearing stale temp...'; rm -rf /data/temp/*; " +
          "echo 'Done.'"
        ],
        "volumeMounts": [{"name": "data", "mountPath": "/data"}]
      }],
      "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "openviking-data"}}]
    }
  }'

# Wait for completion
kubectl -n viking wait --for=condition=Ready pod/pvc-cleaner --timeout=30s
kubectl -n viking wait --for=delete pod/pvc-cleaner --timeout=60s

# Verify cleanup (should show empty dirs)
kubectl -n viking run pvc-verify --image=busybox:1.37 --restart=Never \
  --overrides='{
    "spec": {
      "containers": [{
        "name": "verify",
        "image": "busybox:1.37",
        "command": ["sh", "-c", "ls -la /data/; ls /data/vectordb/ 2>/dev/null; ls /data/viking/ 2>/dev/null; echo Done"],
        "volumeMounts": [{"name": "data", "mountPath": "/data"}]
      }],
      "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "openviking-data"}}]
    }
  }'
kubectl -n viking logs pvc-verify
kubectl -n viking delete pod pvc-verify --force
```

After this step, the PVC retains its directory structure but all S3-era content (VectorDB index, AGFS workspace, queue, temp) is gone. The fresh OpenViking instance will initialize local AGFS from scratch.

### 7. Apply manifests and enforce service routing

Apply all manifest changes and ensure the Service selector points to the standalone instance:

```bash
kubectl apply -f viking/manifests/openviking-standalone-configmap.yaml
kubectl apply -f viking/manifests/openviking-deployment.yaml
kubectl apply -f viking/manifests/openviking-service.yaml   # enforce selector: app=openviking
kubectl rollout restart deployment/openviking -n viking
```

**Do not** apply `ov-worker-statefulset.yaml` here — it was already applied in step 4 and declares `replicas: 3`.

### 8. Verify startup

```bash
kubectl -n viking rollout status deployment/openviking
kubectl -n viking logs -l app=openviking --tail=50
# Check for local AGFS initialization:
kubectl -n viking exec deploy/openviking -- ls /app/data/viking/
```

Expected: local AGFS directory tree created at `/app/data/viking/`. No S3 connection errors.

### 9. Re-ingest source data

Local AGFS starts empty. Re-ingest from canonical sources:

```bash
uv run python viking/tools/compendium-sync.py sync-all
```

Re-ingest will write content to local AGFS and rebuild the VectorDB index from scratch.

### 10. Verify end-to-end

```bash
ov status          # all queues healthy
ov find "*" -u viking://resources/ -n 5   # returns results
ov stat viking://resources/<some-resource>  # content readable
```

## Rollback

If local AGFS causes problems:

1. Restore PVC from Longhorn snapshot taken in step 6 (recovers S3-era VectorDB and workspace data)
2. Switch `openviking` Deployment back to `openviking-config` ConfigMap (`backend: s3`)
3. Restart: `kubectl rollout restart deployment/openviking -n viking`
4. Scale workers/coordinator back up
5. Garage S3 data is untouched — if rollback happens before re-ingest, S3 state is current

If rollback happens after re-ingest into local AGFS, local data is lost but can be re-ingested again. Garage S3 data is always intact as a pre-cutover snapshot.

## What's NOT in scope

- **Worker local AGFS** — requires coordinator code changes (fs/ls fanout, merged-read disabling, temp upload routing)
- **ov-merge as content copier** — current implementation only reindexes by URI; cannot copy AGFS content between shards
- **Removing Garage** — other services still use it
- **Removing the shared `openviking-config` ConfigMap** — workers still reference it if scaled back up
- **Removing S3 secrets** — keep for rollback and future S3 use

## Files to create/modify

| File | Action |
|------|--------|
| `viking/manifests/openviking-standalone-configmap.yaml` | **Create** — dedicated ConfigMap with `backend: local` |
| `viking/manifests/openviking-deployment.yaml` | **Edit** — backend-agnostic initContainer + new ConfigMap ref |
| `viking/manifests/ov-worker-statefulset.yaml` | **Edit** — backend-agnostic initContainer (apply before scale-down, not during cutover) |
| `viking/manifests/openviking-configmap.yaml` | **Edit** — add `storage.transaction` block (for workers if scaled back up) |
| `viking/manifests/openviking-service.yaml` | **Apply** — enforce `selector: app: openviking` during cutover (no content change) |

## Backup story after cutover

Local AGFS on Longhorn SSD needs off-node backups:

- Longhorn recurring snapshots for `openviking-data` PVC
- Restic/Borg on `/app/data/viking` and `/app/data/_system`
- Future: AGFS-to-S3 mirror job (out of scope)

Garage S3 will be stale after cutover — do not treat it as an automatic mirror.
