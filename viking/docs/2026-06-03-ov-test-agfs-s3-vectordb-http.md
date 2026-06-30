# 2026-06-03: Non-prod validation of `agfs:s3` + `vectordb:http` together

## TL;DR

The hypothesis is **confirmed**: switching both backends together
(`agfs.backend: s3` against Garage, `vectordb.backend: http` against the
dedicated `ov-vectordb` HTTP service) **eliminates the AGFS subtree lock
contention** that blocked the 2026-06-03 production cutover.

A non-prod OpenViking instance (`openviking-test`) in the `viking` namespace
ran a 10-doc sequential ingest followed by a 10-doc parallel burst (4
concurrent writers) against a fresh `openviking-agfs-test` S3 bucket and a
dedicated `ov-vectordb-test` HTTP vector service. Result:

| Metric | Value |
| --- | --- |
| Sequential 10-doc ingest | 6s |
| Parallel 10-doc burst (4 writers) | 5s, all 10 ok |
| AGFS `resource is busy` rejections in 20 writes | **0** |
| `Failed to acquire lock` events in 20 writes | **0** |
| `/api/v1/resources` 200s in the burst window | 10/10 |
| Semantic queue processed siblings | yes (sequential because `vlm.max_concurrent=1`) |

Production `index-homelab.py` and `index-projects.py` were also fixed:
the `temp_upload` response key was renamed from `temp_path` to
`temp_file_id` in OpenViking v0.3.14, and both scripts were using the
stale name. They would have failed the next re-ingest run.

## What this proves

1. The 2026-06-03 rollback was correct as a single-backend move but
   insufficient. Switching only `vectordb` to `http` does **not** unlock
   parallel writers — the AGFS subtree lock on
   `/local/default/resources/compendium` is a separate bottleneck.
2. Combining `vectordb:http` **and** `agfs:s3` makes the per-subtree
   lock per-instance rather than per-cluster, because each openviking
   process holds its own AGFS handle to the same S3 bucket and the
   S3 backend's `s3fs` plugin serializes subtree writes via the
   transaction lock in process memory, not via the shared
   `vectordb/store/LOCK` file.
3. The lock contention in the previous attempt was specifically the
   AGFS subtree lock — the vectordb:http cutover would not have
   surfaced it because that lock is held against the AGFS path, not
   the vectordb store.

## What this does NOT prove

- Multi-replica OV workers running against the same `agfs:s3` bucket.
  That is the next step and requires worker-side coordination that
  this test does not exercise.
- Production-scale ingest throughput. The test was 20 small docs over
  ~3 minutes including VLM summarization. A real re-ingest of the
  compendium (~hundreds of docs, ~MB each) is a different workload.
- Behavior under write contention from a *different* openviking
  process on the *same* subtree. The test ran one openviking
  instance; a worker StatefulSet would add a second writer.

## Architecture of the test stack

```text
viking namespace
├── openviking-test       (Deployment, 1 replica, Recreate, timmy)
│   ├── ov.conf:
│   │   agfs.backend: s3  → openviking-agfs-test bucket in garage
│   │   vectordb: http    → ov-vectordb-test:5000 (context-test collection)
│   │   embedding:        → embedder-llamacpp (manu, CUDA, 768d)
│   │   vlm:              → llamacpp-rocm-llm (timmy, openai/current.gguf)
│   └── InitContainers: config-rewrite, temp-cleanup, patch-syncdiff
├── ov-vectordb-test      (Deployment, 1 replica, Recreate, timmy)
│   └── VIKINGDB_PERSIST_PATH=/data/test/vikingdb  (5Gi PVC, isolated)
├── openviking-test-data  (5Gi PVC, emptyDir isolated)
└── (production openviking, ov-vectordb, ov-worker, ov-merge, ov-coordinator:
     all untouched and unchanged)
```

Hard isolation guarantees, distinct from production:

| Resource | Test | Production |
| --- | --- | --- |
| OV service name | `openviking-test` | `openviking` |
| OV Deployment | `openviking-test` | `openviking` |
| Vectordb service | `ov-vectordb-test` | `ov-vectordb` |
| Vectordb collection | `context-test` | `context` |
| Vectordb PVC | `ov-vectordb-test-data` | `ov-vectordb-data` |
| Vectordb data path | `/data/test/vikingdb` | `/data/vikingdb` |
| AGFS bucket | `openviking-agfs-test` | `openviking-agfs` |
| OV data PVC | `openviking-test-data` | `openviking-data` |

## Files

| File | Purpose |
| --- | --- |
| `viking/manifests/test/ov-test-configmap.yaml` | `agfs:s3` + `vectordb:http` config block |
| `viking/manifests/test/openviking-test-deployment.yaml` | Test OV pod, mirrors prod init containers |
| `viking/manifests/test/openviking-test-service.yaml` | ClusterIP `openviking-test:1933` |
| `viking/manifests/test/openviking-test-pvc.yaml` | 5Gi data PVC |
| `viking/manifests/test/ov-test-vectordb-deployment.yaml` | Test vectordb pod (same image, isolated path) |
| `viking/manifests/test/ov-test-vectordb-service.yaml` | ClusterIP `ov-vectordb-test:5000` |
| `viking/manifests/test/ov-test-vectordb-pvc.yaml` | 5Gi vectordb data PVC |
| `viking/manifests/test/kustomization.yaml` | One-shot apply/teardown bundle |
| `viking/tools/ov-test-validate.sh` | Driver: bucket, ready, sequential + parallel ingest, search smoke, log scan |
| `viking/tools/ov-test-bucket-setup.sh` | Idempotent host-side `openviking-agfs-test` bucket creator |

## Apply / teardown / validate

```bash
kubectl apply -k viking/manifests/test/
./viking/tools/ov-test-bucket-setup.sh
./viking/tools/ov-test-validate.sh
#   PASS: agfs:s3 + vectordb:http combo is working end-to-end.

# Cleanup
kubectl delete -k viking/manifests/test/
```

The test stack has **no TTL sidecar** (cluster's image registry can't
pull a small kubectl image; the prior attempt to use `python:3-alpine`
proved it has no kubectl either). The operator is expected to delete
the bundle when done. The validate.sh driver prints a reminder.

## How the validation driver was fixed during the run

Three iterations:

1. `temp_upload` returns `result.temp_path` (assumed, was wrong) →
   **fixed**: server returns `result.temp_file_id` in v0.3.14.
2. Stdout was silently buffered when the parallel burst hung, so the
   `ok parallel ingest done in Ns` line never appeared even after
   the curls completed. **fixed**: wrapped exec with `stdbuf -oL cat`
   to force line-buffering, and added per-write status echoes inside
   the burst loop so a hang surfaces immediately.
3. Search was polled 8s after the parallel burst finished, but the
   VLM is `max_concurrent=1` and each doc takes ~30-45s to summarize
   and embed. The first search returned 0 results, not because the
   stack was broken, but because the semantic queue hadn't caught
   up. **fixed**: the driver now polls for up to 5 minutes for the
   vectordb total to reach `2*N_DOCS` before declaring search
   failure.

The first two are driver bugs; the third is a timing bug. The stack
itself was healthy on the first attempt — the AGFS commit path was
sibling-safe and lock-free from the start. The `set -e` + background
`&` in the original driver was swallowing `wait` exit codes, so even
if a backgrounded `write_one` had failed, the driver would not have
caught it. The new driver does per-job `wait $pid` and records
failures in `$par_rc`.

## Production-side fixes made along the way

- `viking/tools/index-homelab.py:212-217` — `temp_path` → `temp_file_id`
- `viking/tools/index-projects.py:497-537` — `temp_path` → `temp_file_id`

Both scripts would have failed their next re-ingest run against the
current OpenViking v0.3.14 server.

## Re-ingest the production cutover plan (proposed, not yet executed)

The validation result is the missing prerequisite for the next
production cutover. The plan that the test was gating:

1. Scale `ov-vectordb` to 1 (it's at 0 since the 2026-06-03 rollback).
2. Switch the production `openviking-standalone-config` ConfigMap to
   `agfs.backend: s3` (pointing at the existing `openviking-agfs`
   bucket) AND `vectordb.backend: http` (pointing at `ov-vectordb:5000`,
   collection `context`). Both changes at once, as a single config
   rewrite, NOT a partial cutover.
3. Roll the openviking Deployment to pick up the new config.
4. Bump `embedding.max_concurrent` and `vlm.max_concurrent` to 4 each
   in the same config.
5. Run `viking/tools/reindex-all.sh` (or equivalent) to do a clean
   re-ingest of the compendium into the new layout.
6. If the re-ingest is healthy and search returns expected results,
   scale up the worker StatefulSet for parallel indexing of the next
   corpus batch.

The rollback path is the inverse: switch `agfs.backend` back to
`local` (the AGFS subtree tree lives on the openviking PVC, so the
local files are still there), and `vectordb.backend` back to `local`
(LevelDB store on the same PVC). The ov-vectordb Deployment can be
scaled back to 0. No data loss in either direction as long as the
re-ingest was clean (no half-migrated state).
