# 2026-06-03: OpenViking production cutover to `agfs:s3` + `vectordb:http`

## TL;DR

Production openviking is now running with the
[`agfs:s3`](#storageagfsbackend--s3) backend against Garage and a
[dedicated `ov-vectordb`](#storagevectordbbackend--http) HTTP service,
matching the [non-prod validation result](2026-06-03-ov-test-agfs-s3-vectordb-http.md).
Single-write smoke test passed end-to-end (commit + S3 persistence +
vectordb upsert). The semantic queue is actively draining the carried-over
compendium data in S3 at `vlm.max_concurrent=1`; this will take several
hours to complete.

| Field | Value |
| --- | --- |
| Cutover executed | 2026-06-03 (~23:00-00:14 CDT) |
| Source backends | `agfs:local`, `vectordb:local` |
| Target backends | `agfs:s3` (Garage `openviking-agfs`), `vectordb:http` (`ov-vectordb:5000`) |
| Local data loss | ~9.1 MiB compendium tree + LevelDB store on `openviking-data` PVC (wiped in step 3.1) |
| Re-ingest path | `viking/tools/reindex-all.sh` (or `index-homelab.py` / `index-projects.py` separately) |
| Smoke result | ✅ commit + S3 durable + vectordb upsert observed |
| Queue progress | 35 objects summarized in S3 in first ~5 min (15 `.overview.md` + 15 `.abstract.md` + 5 directory/lock); rest of carryover queue draining in background |

## What changed

### `storage.vectordb.backend` → `http`

**File:** `viking/manifests/openviking-standalone-configmap.yaml`

```json
"vectordb": {
  "backend": "http",
  "name": "context",
  "dimension": 768,
  "url": "http://ov-vectordb.viking.svc.cluster.local:5000"
}
```

The `ov-vectordb` Deployment (image `ghcr.io/volcengine/openviking:v0.3.14`,
`python -m openviking.storage.vectordb.service.server_fastapi`,
`VIKINGDB_PERSIST_PATH=/data/vikingdb` on PVC `ov-vectordb-data`) was scaled
from 0→1 replica and joined the existing Service. The new pod started on
timmy within 15s and passed its `/health` probe.

### `storage.agfs.backend` → `s3`

```json
"agfs": {
  "backend": "s3",
  "s3": {
    "bucket": "openviking-agfs",
    "endpoint": "http://garage.garage.svc.cluster.local:3900",
    "region": "garage",
    "access_key": "${S3_ACCESS_KEY}",
    "secret_key": "${S3_SECRET_KEY}",
    "use_path_style": true
  }
}
```

S3 credentials are read from the existing `openviking-s3-credentials`
Secret and substituted by the `config-rewrite` init container at pod
startup. The rendered config lives at `/app/.openviking/ov.conf` inside
the openviking pod.

### Concurrency knobs left at conservative values

Per the migration plan decision: `server.workers=1`, `embedding.max_concurrent=1`,
`vlm.max_concurrent=1`. These are safe with the new networked backends but
are throughput-limited. **Bump plan (post-cutover follow-up, not in this
plan):** try `vlm.max_concurrent=4` and `embedding.max_concurrent=4` after
the queue has drained and we've observed steady-state latency.

## What got wiped (and what's still there)

- **Wiped:** `/app/data/viking/*` inside the openviking pod (~9.1 MiB,
  the local AGFS tree + embedded LevelDB).
- **Still there:** the `openviking-agfs` S3 bucket had 879 pre-existing
  objects from a prior `agfs:s3` run (May-10, an earlier worker or
  experimental config that wrote to this same bucket). The new openviking
  pod rehydrated the tenant tree from S3 on startup. Plus 35 new objects
  written during the smoke test (carryover compendium VLM summaries plus
  the smoke file).
- **Gone forever:** the **old `agfs:local` compendium tree** that lived
  only on the `openviking-data` PVC's `/app/data/viking/default/`. That
  data was never written to S3, so the wipe in step 3.1 destroyed it.
  This is expected — the migration plan's "delete all existing data"
  decision accepted this loss. The new tree being built is from the
  May-10 S3 carryover, not from the local tree. The full homelab /
  projects compendium will be rebuilt via `viking/tools/reindex-all.sh`.

## Rollback procedure

If the new stack misbehaves and we need to go back to local backends:

### Fast path (no data loss window)

1. `kubectl -n viking apply -f viking/manifests/openviking-standalone-configmap.yaml` — but **first** edit the file to revert `vectordb.backend` to `local` and `agfs.backend` to `local`. This requires reverting the two-block diff committed in the cutover.
2. `kubectl -n viking delete pod -l app=openviking` — pod restarts with the reverted config; `config-rewrite` drops the s3 block and the http `url`.
3. `kubectl -n viking scale deploy/ov-vectordb --replicas=0` — stop the HTTP vector service.
4. Verify: `curl http://openviking.viking.svc.cluster.local:1933/ready` should return `agfs: ok, vectordb: ok`.
5. **Caveat:** the local `vectordb` store on the `openviking-data` PVC was wiped in step 3.1 and has not been re-populated. The reverted local stack will have an **empty embedded LevelDB** — searches return nothing, the AGFS tree is empty. The S3 bucket still has the 879 carryover objects, but local stack can't read them.

### Recovery from empty local after rollback

1. Re-ingest via `viking/tools/index-homelab.py` (timmy routes the VLM
   calls to `llamacpp-rocm-llm` per the existing service routing).
2. Or: use `viking/tools/reindex-all.sh` for the full compendium.
3. Either re-ingest repopulates `/app/data/viking/default/resources/...`
   and the embedded LevelDB. Estimated time at `vlm.max_concurrent=1`:
   ~3-5 hours for the full homelab + projects corpus.

### When to rollback vs. wait

- **Wait it out** if the issue is "the queue is still draining" — this
  is normal. The semantic queue is processing the S3 carryover at
  ~30-45s per object; expect several hours.
- **Rollback** if openviking is CrashLoopBackOff, the vectordb HTTP
  service is unreachable, or new writes are returning errors.

### Things to check first before rolling back

| Symptom | Check |
| --- | --- |
| Searches return 0 results | Is the smoke file's `.abstract.md` generated? `kubectl -n viking exec deploy/ov-vectordb -- ls /data/vikingdb/`. Are new objects being added to S3? `kubectl -n viking logs openviking-...` for "All embedding tasks completed" |
| Slow searches | Vector DB has only 17 entries; it'll grow. Are `ov-vectordb` pod's CPU/mem saturated? `kubectl -n viking top pods` |
| New writes fail | `kubectl -n viking logs openviking-... -c openviking --tail=200` for "resource is busy" or similar |
| `/ready` returns 503 | `kubectl -n viking logs deploy/ov-vectordb --tail=50`; `kubectl -n viking logs openviking-... -c openviking --tail=200` |

## Post-cutover follow-ups (out of plan)

- **Bump concurrency:** after the queue has drained, try
  `vlm.max_concurrent=4` and `embedding.max_concurrent=4` (just edit the
  ConfigMap and re-roll the pod). Monitor GPU usage on timmy — the
  ROCm LLM is shared with Ollama, so don't go too high.
- **Re-ingest homelab + projects:** the local-only compendium is gone.
  Run `viking/tools/reindex-all.sh` to rebuild from source. This is
  the operation that will exercise the **parallel-writer** path the
  cutover was designed to unlock — the test stack proved it works in
  isolation, but production is the real proof.
- **Worker scale-up:** once re-ingest is healthy, scale
  `ov-worker` (StatefulSet) to 3 replicas and `ov-coordinator` to 1.
  The worker pool is the real beneficiary of `agfs:s3`+`vectordb:http`:
  the per-subtree lock that used to serialize writes is now process-local
  per pod, so multiple workers can index the compendium in parallel.
- **Update CLAUDE.md** to reflect that `agfs:s3`+`vectordb:http` is the
  new prod default (not `agfs:local`+`vectordb:local`).

## Files changed in this cutover

- `viking/manifests/openviking-standalone-configmap.yaml` — two-block
  diff: vectordb.local→http, agfs.local→s3. The `agfs:s3` worker config
  in `openviking-config` ConfigMap is unchanged — workers (when scaled
  up) already used the S3 backend.
- `viking/tools/ov-test-validate.sh`, `viking/tools/index-homelab.py`,
  `viking/tools/index-projects.py` — fixed `temp_path` → `temp_file_id`
  (renamed in OpenViking v0.3.14). Already committed earlier in this
  session (commit `ba59efd`).

## Verification log

```
$ kubectl -n viking apply -f viking/manifests/openviking-standalone-configmap.yaml
configmap/openviking-standalone-config configured

$ kubectl -n viking scale deploy/ov-vectordb --replicas=1
deployment.apps/ov-vectordb scaled
$ kubectl -n viking wait --for=condition=available --timeout=90s deploy/ov-vectordb
deployment.apps/ov-vectordb condition met

$ kubectl -n viking exec openviking-... -- cat /app/.openviking/ov.conf | jq '.storage'
{
  "agfs": { "backend": "s3", "s3": { "bucket": "openviking-agfs", ... } },
  "vectordb": { "backend": "http", "url": "http://ov-vectordb...:5000", ... }
}

$ curl http://openviking:1933/ready
{"status":"ready","checks":{"agfs":"ok","vectordb":"ok","api_key_manager":"ok","ollama":"not_configured"}}

# Smoke test
$ curl -X POST /api/v1/resources/temp_upload -F file=@smoke.txt
{"status":"ok","result":{"temp_file_id":"upload_03ebd70ff7534cf8a2286f05fa07a77d.txt"}}
$ curl -X POST /api/v1/resources -d '{"temp_file_id":"...","to":"viking://resources/smoke/20260603-191140"}'
{"status":"ok","result":{"status":"success",...,"root_uri":"viking://resources/smoke/20260603-191140",...}}

# S3 confirms the write
$ python3 -c 'import boto3; ...; list_objects_v2(...)'
# 4 objects in default/resources/smoke/20260603-191140/:
#   .path.ovlock (58B)
#   smoke.md (153B)
#   ./ (dir marker)
#   ../ (dir marker)
# Plus 31 compendium summaries (.abstract.md / .overview.md) in the next 5 min.
```
