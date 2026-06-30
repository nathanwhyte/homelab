# 2026-06-03: OpenViking data wipe + clean restart

## TL;DR

Wiped all OpenViking data and restarted with empty state. S3 bucket `openviking-agfs` is now empty (917 → 0 objects). Both PVCs (`openviking-data`, `ov-vectordb-data`) were deleted and recreated against fresh Longhorn volumes. The cluster is ready for a clean re-ingest (TASK-008) without carryover noise.

| Field | Value |
| --- | --- |
| Wipe executed | 2026-06-03 (late evening) |
| Bucket objects removed | 917 (all `_system/` and `default/` prefixes) |
| PVCs recreated | `openviking-data`, `ov-vectordb-data` (10Gi each, `longhorn-ssd`) |
| Scope | OpenViking only (NOT embedder, VLM, Ollama, Garage, claude-mem) |
| Preserved | All Secrets (`openviking-api-key`, `openviking-s3-credentials`, `openviking-tls`); all ConfigMaps; all Deployments / Services / Ingress / Middleware |
| Image | unchanged (`ghcr.io/volcengine/openviking:v0.3.14`) |
| ConfigMap | unchanged (`agfs:s3` + `vectordb:http`) |
| Smoke result | ✅ temp_upload + add_resource + VLM semantic gen + embedding + search round-trip all healthy |

## What was wiped

| Surface | Before | After |
| --- | --- | --- |
| `openviking-agfs` S3 bucket | 917 objects (879 May-10 carryover + 35 cutover smoke + 3 rehydration markers) | 0 objects |
| `openviking-data` PVC | 24-day-old workspace scratch + lock files | fresh Longhorn volume |
| `ov-vectordb-data` PVC | 35h-old LevelDB store built from carryover | fresh Longhorn volume |
| OV pod local temp/queue/lock state | process-local, gone with the old pod | process-local, gone with the old pod |

## What was preserved (per user)

- `openviking-api-key` — Bearer token for OV API
- `openviking-s3-credentials` — Garage S3 access keys
- `openviking-tls` — TLS cert for `context.nathanwhyte.dev`
- `openviking-standalone-config` ConfigMap — points at the (now empty) `openviking-agfs` bucket + `ov-vectordb` HTTP service
- `openviking-config` ConfigMap (worker variant) — unused, workers are at 0 replicas
- All Deployments, Services, Ingress, Middleware

## Sequence

### Phase 0: Pre-wipe verification

```bash
$ kubectl -n viking get deploy openviking ov-vectordb
NAME          READY   UP-TO-MINUTE   AVAILABLE   AGE
openviking    1/1     1              1           24d
ov-vectordb   1/1     1              1           35h

$ kubectl -n viking get pvc openviking-data ov-vectordb-data
NAME               STATUS   VOLUME                                    CAPACITY   ACCESS MODES   STORAGECLASS
openviking-data    Bound    pvc-a7eaa3c2-7902-4d9e-9224-2d5f8c0ad621  10Gi       RWO            longhorn-ssd
ov-vectordb-data   Bound    pvc-c92a5746-5b12-487d-8b8e-7be49c1cd4c7  10Gi       RWO            longhorn-ssd

$ # from inside openviking pod:
objects in openviking-agfs: 917
top-level prefixes: ['_system', 'default']

$ # /ready
{"status":"ready","checks":{"agfs":"ok","vectordb":"ok","api_key_manager":"ok","ollama":"not_configured"}}
```

### Phase 1: Stop OV workloads

```bash
$ kubectl -n viking scale deploy openviking --replicas=0
deployment.apps/openviking scaled
$ kubectl -n viking scale deploy ov-vectordb --replicas=0
deployment.apps/ov-vectordb scaled
$ kubectl -n viking wait --for=delete pod -l 'app in (openviking, ov-vectordb)' --timeout=120s
pod/openviking-678fcf498f-66g8p condition met
$ kubectl -n viking get pods -l 'app in (openviking, ov-vectordb)'
No resources found in viking namespace.
```

### Phase 2: Wipe S3 bucket contents

Both OV Deployments were scaled to 0, so the openviking pod (which had boto3 installed by the `config-rewrite` init container) was no longer available. Spun up a one-shot wipe pod with the same OV image, installed boto3 via `pip install --target /tmp/pylibs boto3`, mounted `ov.conf` and `wipe_bucket.py` from a ConfigMap.

`wipe_bucket.py` paginates `list_objects_v2` and batch-deletes 1000 keys at a time via `delete_objects`:

```bash
$ kubectl -n viking logs ov-bucket-wipe
  batch: deleted 917, errors: 0
total deleted: 917; remaining in openviking-agfs: 0
```

Clean exit. Cleaned up the wipe pod + configmap.

### Phase 3: Delete + recreate PVCs

```bash
$ kubectl -n viking delete pvc openviking-data ov-vectordb-data
persistentvolumeclaim "openviking-data" deleted from viking namespace
persistentvolumeclaim "ov-vectordb-data" deleted from viking namespace

$ kubectl -n viking apply -f /tmp/ov-pvcs.yaml
persistentvolumeclaim/openviking-data created
persistentvolumeclaim/ov-vectordb-data created

# New Longhorn volumes provisioned (~3 min total)
$ kubectl -n viking get pvc openviking-data ov-vectordb-data
NAME               STATUS   VOLUME                                     CAPACITY   ACCESS MODES   STORAGECLASS
openviking-data    Bound    pvc-6ba324aa-6827-4829-84ef-9d2e3c8de0f1   10Gi       RWO            longhorn-ssd
ov-vectordb-data   Bound    pvc-0aa76aa9-97c2-463f-b935-0b4a17feb05c   10Gi       RWO            longhorn-ssd
```

The PVs were released by Longhorn automatically when the PVCs were deleted. New PVCs with the same name bound to fresh Longhorn volumes (note the new PV IDs vs the pre-wipe ones). The Deployment objects reference PVCs by name only, so the new pods will pick up the fresh volumes on the next rollout.

### Phase 4: Restart OV workloads

```bash
$ kubectl -n viking scale deploy ov-vectordb --replicas=1
deployment.apps/ov-vectordb scaled
$ kubectl -n viking wait --for=condition=available --timeout=120s deploy/ov-vectordb
deployment.apps/ov-vectordb condition met

$ kubectl -n viking scale deploy openviking --replicas=1
deployment.apps/openviking scaled
$ kubectl -n viking wait --for=condition=available --timeout=120s deploy/openviking
deployment.apps/openviking condition met

$ kubectl -n viking get pods -l 'app in (openviking, ov-vectordb)' -o wide
NAME                          READY   STATUS    RESTARTS   AGE   IP            NODE
openviking-678fcf498f-j465v   2/2     Running   0          28s   10.42.2.105   timmy
ov-vectordb-bdd9bcbd7-2hmt8   1/1     Running   0          48s   10.42.2.104   timmy
```

Order matters: `ov-vectordb` first so the openviking pod's `/ready` vectordb check doesn't flap during startup.

### Phase 5: Verify clean state

**1. /ready from inside openviking pod:**

```bash
$ kubectl -n viking exec openviking-678fcf498f-j465v -c openviking -- \
    python3 -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:1933/ready', timeout=5).read().decode())"
{"status":"ready","checks":{"agfs":"ok","vectordb":"ok","api_key_manager":"ok","ollama":"not_configured"}}
```

All 4 checks healthy.

**2. Fresh PVCs are empty (besides bootstrap dirs OV creates on first start):**

```bash
$ kubectl -n viking exec openviking-678fcf498f-j465v -c openviking -- ls -la /app/data/
total 44
drwxr-xr-x 6 root root  4096 Jun  4 04:18 .
-rw-r--r-- 1 root root     1 Jun  4 04:18 .openviking.pid
drwxr-xr-x 3 root root  4096 Jun  4 04:18 _system      # OV bootstrap
drwxr-xr-x 5 root root  4096 Jun  4 04:18 bot          # OV bootstrap
drwx------ 2 root root 16384 Jun  4 04:17 lost+found  # fresh fs
drwxr-xr-x 2 root root  4096 Jun  4 04:18 viking       # OV bootstrap

$ kubectl -n viking exec ov-vectordb-bdd9bcbd7-2hmt8 -- ls -la /data/vikingdb/
drwxr-xr-x 3 root root 4096 Jun  4 04:18 default      # OV bootstrap
```

Timestamps from today (2026-06-04 UTC) confirm fresh filesystems. No carryover data.

**3. Smoke test: write a fresh file, search for it (full round-trip):**

```bash
$ # temp_upload
$ kubectl -n viking exec openviking-... -c openviking -- curl -sS -X POST \
    "http://127.0.0.1:1933/api/v1/resources/temp_upload" -H "X-API-Key: ..." \
    -H "X-OpenViking-Account: default" -H "X-OpenViking-User: noot" \
    -F "file=@-;filename=smoke.txt;type=text/plain" <<'EOF'
Post-wipe smoke test. If you can read this back via search, the fresh agfs:s3+vectordb:http stack is healthy.
EOF
{"status":"ok","result":{"temp_file_id":"upload_182ca9be43a04466a4ac23547a1336de.txt"}}

$ # add_resource (commit)
$ kubectl -n viking exec openviking-... -c openviking -- curl -sS -X POST \
    "http://127.0.0.1:1933/api/v1/resources" -H "X-API-Key: ..." \
    -H "X-OpenViking-Account: default" -H "X-OpenViking-User: noot" \
    -H "Content-Type: application/json" \
    -d '{"temp_file_id":"upload_...","to":"viking://resources/smoke/20260603-231934"}'
{"status":"ok","result":{"status":"success","root_uri":"viking://resources/smoke/20260603-231934",...}}
```

**4. OV logs show VLM + embedding completed:**

```text
2026-06-04 04:19:51 - openviking.storage.queuefs.embedding_tracker - INFO - Registered embedding tracker for SemanticMsg c4201966-...: 3 tasks
2026-06-04 04:19:51 - openviking.storage.queuefs.semantic_processor - INFO - Completed semantic generation for: viking://resources/smoke/20260603-231934
2026-06-04 04:19:52 - openviking.storage.queuefs.embedding_tracker - INFO - All embedding tasks(3) completed for SemanticMsg c4201966-...
```

Took ~15s from upload to fully-embedded (no carryover queue to drain).

**5. S3 confirms the new state — 40 objects total (bootstrap + smoke):**

```bash
$ # from openviking pod, using boto3 installed via pip --target /tmp/pylibs
total objects in openviking-agfs: 40
  2026-06-04 04:18:01      0 default/
  2026-06-04 04:18:01     72 default/agent/default/.abstract.md
  ...
  2026-06-04 04:19:51    256 default/resources/smoke/20260603-231934/.abstract.md
  2026-06-04 04:19:51   3147 default/resources/smoke/20260603-231934/.overview.md
  2026-06-04 04:19:51      0 default/resources/smoke/20260603-231934/smoke.md
```

**6. Semantic search round-trip finds the smoke file as the top hit:**

```bash
$ curl -sS -X POST .../api/v1/search/search -d '{"query":"post-wipe smoke test fresh stack"}'
total: 10
  uri: viking://resources/smoke/20260603-231934/.abstract.md  score: 0.572
  uri: viking://user/.abstract.md                               score: 0.476
  uri: viking://agent/.abstract.md                              score: 0.437
  ...
```

**All checks passed. The fresh stack is fully functional: S3 persistence, VLM semantic generation, embedding, vectordb upsert, and search round-trip — all working against the empty bucket + empty vectordb.**

## Why this is safe

The 2026-06-03 cutover already moved durable state out of the local PVCs. The current local stack only depends on the S3 bucket + `ov-vectordb-data` for durability. Wiping both recreates that exact pair from scratch — there's no hidden state elsewhere.

The ConfigMap, Secrets, and Deployments are pure infrastructure. They don't change between runs. So this is a "data plane reset, control plane untouched" — the most conservative kind of wipe.

## What this does NOT do

- Does not re-ingest the homelab + projects compendium. That's TASK-008.
- Does not change the OV image. Still `v0.3.14`.
- Does not change the ConfigMap. Still `agfs:s3` + `vectordb:http` + same S3 creds.
- Does not bump concurrency knobs. Still `vlm.max_concurrent=1`, `embedding.max_concurrent=1`, `server.workers=1`.
- Does not scale up `ov-worker` / `ov-coordinator`. Still 0 replicas.
- Does not touch the test stack (`viking/manifests/test/`). Already torn down.
- Does not touch embedder, VLM, Ollama, Garage, or claude-mem.

## Recovery notes

If something goes wrong after the wipe (OV CrashLoopBackOff, /ready not 200, etc.):

1. Check pod logs first: `kubectl -n viking logs deploy/openviking -c openviking --tail=200`
2. Check ConfigMap is still correct: `kubectl -n viking get cm openviking-standalone-config -o jsonpath='{.data.ov\.conf}'` — should show the agfs:s3 + vectordb:http config from commit `c3a413b`.
3. Check PVCs bound: `kubectl -n viking describe pvc openviking-data ov-vectordb-data`
4. If the ConfigMap is wrong (shouldn't happen, we didn't edit it), re-apply from git: `kubectl -n viking apply -f viking/manifests/openviking-standalone-configmap.yaml`
5. Last resort: scale both Deployments to 0, delete them, re-apply from git manifests. The bucket and PVCs will stay empty; the deployments will restart clean against the existing empty backends.

## What's next

- **TASK-008** is now actionable. The carryover queue is empty, the bucket is empty, the vectordb is empty. Run `viking/tools/index-homelab.py` + `viking/tools/index-projects.py` to re-ingest.
- After re-ingest, monitor for `resource is busy` lock rejections in OV logs. If zero, the parallel-writer path is proven and we can bump `vlm.max_concurrent` / `embedding.max_concurrent` to 4.
- After concurrency is bumped, scale `ov-worker` to 3 replicas + `ov-coordinator` to 1.

## Files changed

- `viking/docs/2026-06-03-ov-data-wipe-restart.md` — this doc (new)
- `~/code/archive/personal-compendium/tasks/TASK-008-homelab-reingest-compendium-after-cutover.md` — frontmatter `status_detail` updated to reflect empty stat
- `~/code/archive/personal-compendium/log.md` — 2026-06-03 entry recording the wipe
