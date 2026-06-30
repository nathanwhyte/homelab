# OpenViking Parallelization Cross-Reference

**Date:** 2026-06-01
**Scope:** Cross-reference homelab OpenViking parallelization notes against upstream OpenViking docs and source.

## Executive summary

The original homelab conclusion is correct **for the current `local` VectorDB deployment**: multiple OpenViking server processes cannot safely write to one embedded local vector store. In that mode, GPU inference can be parallelized, but the durable vector-index write is constrained by the embedded store's exclusive directory lock.

However, upstream OpenViking's broader scaling story is more nuanced:

- The embedded local store is **LevelDB**, not RocksDB.
- The single-writer bottleneck applies to `storage.vectordb.backend: "local"`.
- Upstream supports pluggable networked VectorDB backends (`http`, `qdrant`, `opengauss`, `volcengine`, `vikingdb`) that can accept writes from multiple OpenViking instances through one shared service/database. **Note:** on the deployed `v0.3.14` image only `http`/`volcengine`/`vikingdb` exist; `qdrant`/`opengauss` are main-only (verified 2026-06-01). And `http` is a single-service funnel, not a scalable cluster store.
- AGFS/RAGFS path locking is separate from the VectorDB lock and is designed as a cross-process/distributed lock layer over shared AGFS.

For the homelab, the custom coordinator + per-worker local shards + merge service is best understood as a workaround for staying on embedded `local` VectorDB. A cleaner upstream-aligned approach would be: shared S3 AGFS + networked VectorDB + multiple OpenViking worker instances routed directly by Kubernetes/service load balancing.

## Verified upstream facts

### Storage architecture

Upstream docs describe OpenViking as dual-layer storage:

| Layer | Role |
|------|------|
| AGFS/RAGFS | Source-of-truth content store: L0/L1/L2 files, media, relations |
| Vector Index / VectorDB | Derived semantic index: URI references, vectors, metadata |

The docs explicitly state that AGFS is the source of truth and the vector index can be rebuilt from content.

Relevant upstream docs:

- `docs/en/concepts/01-architecture.md`
- `docs/en/concepts/05-storage.md`
- `docs/en/concepts/09-transaction.md`
- `docs/en/guides/01-configuration.md`

### Embedded local VectorDB is LevelDB

The previous "RocksDB" wording in homelab notes is imprecise. Current upstream source uses LevelDB:

- `third_party/leveldb-1.23/`
- `src/CMakeLists.txt` links `leveldb`
- `src/store/persist_store.h` includes `leveldb/db.h`
- `src/store/persist_store.cpp` opens `leveldb::DB::Open(...)`

The practical consequence is unchanged: LevelDB also uses an exclusive `LOCK` file for a database directory, so multiple processes opening the same local store for writes will contend/fail.

### Local VectorDB lock evidence

Upstream Python code references the local store's lock behavior:

- `openviking/storage/vectordb/store/local_store.py` creates `engine.PersistStore(path)` for persistent local storage.
- `openviking/storage/vectordb/utils/stale_lock.py` documents lock-file cleanup under paths like `vectordb/<collection>/store/LOCK`.
- `openviking/storage/viking_vector_index_backend.py` notes that sharing an adapter avoids `LOCK` contention when multiple account backends point to the same storage path.

This validates the observed homelab failure when increasing `server.workers`: each uvicorn worker is a separate process and attempts to open the same embedded local store.

### Server workers are separate processes

Upstream server bootstrap supports `--workers` / `server.workers`; when `workers > 1`, it calls uvicorn with an import string and separate worker processes.

With `storage.vectordb.backend: "local"`, those processes contend on the embedded LevelDB directory lock. Therefore `server.workers: 1` remains the right setting for the current local-backend deployment.

### Networked VectorDB backends exist upstream

Upstream config and adapter code support these VectorDB backends. **Availability is version-dependent** — the table below was verified by diffing the `v0.3.14` git tree (the deployed image) against `main` (2026-06-01):

| Backend | In `v0.3.14`? | Notes |
|--------|:---:|------|
| `local` | ✅ | Embedded LevelDB/native vector engine; process-local, file-backed; single-writer |
| `http` | ✅ | OpenViking's own HTTP VectorDB service adapter (see confirmed backing below) |
| `volcengine` | ✅ | Volcengine VikingDB (cloud SaaS) |
| `vikingdb` | ✅ | Private VikingDB deployment |
| `qdrant` | ❌ | External Qdrant server — **added after v0.3.14; main-only** |
| `opengauss` | ❌ | openGauss vector backend (docs mention distributed mode) — **added after v0.3.14; main-only** |

So on the deployed `v0.3.14` image, the only non-`local` options are `http`, `volcengine`, and `vikingdb`. Qdrant and openGauss require an OV upgrade first.

Relevant source:

- `openviking_cli/utils/config/vectordb_config.py` (in v0.3.14)
- `openviking/storage/vectordb_adapters/factory.py` (in v0.3.14)
- `openviking/storage/vectordb_adapters/{local,http,volcengine,vikingdb_private}_adapter.py` (in v0.3.14)
- `openviking/storage/vectordb_adapters/{qdrant,opengauss}_adapter.py` (**main only — not in v0.3.14**)
- `openviking/storage/vectordb/service/server_fastapi.py` (in v0.3.14)

This changes the broad answer from "OpenViking cannot cleanly write concurrently to one VectorDB" to:

> OpenViking cannot cleanly write concurrently from multiple processes into one **embedded local** VectorDB directory. It can use networked/shared VectorDB backends for multi-instance writes.

### What the bundled `http` backend actually wraps (confirmed)

The `http` backend points at `openviking/storage/vectordb/service/server_fastapi.py` (titled "VikingDB API"), which delegates to `service/api_fastapi.py` in the **same `openviking.storage.vectordb` package as `store/local_store.py` and the native C++/LevelDB engine**. So the `http` backend is the native local engine fronted by **one FastAPI process** — a single networked write-funnel, not a sharded/replicated cluster DB.

Practical consequence: `http` *does* remove the multi-process-opening-one-LevelDB crash (only the service opens the store), so multiple OV app pods can target it. But it does **not** provide Qdrant/openGauss/VikingDB-class horizontal write scalability — it's "one writer, reachable over the network." Treat it as a way to decouple the store from the app pods, not as a path to many concurrent writers.

## AGFS/RAGFS transaction locking

Upstream docs call the path locks **EXACT** and **TREE** locks. The homelab logs/notes mentioning `[POINT]` / `[SUBTREE]` should be mapped to upstream terminology.

Key properties from `docs/en/concepts/09-transaction.md`:

- Path locks are enabled by default.
- Default lock acquisition is no-wait (`lock_timeout: 0.0`), raising `LockAcquisitionError` if a path is locked.
- `storage.transaction.lock_timeout` can be increased to wait/retry.
- `EXACT` locks protect one path.
- `TREE` locks protect a subtree root and conflict with descendant/ancestor writes.
- Locks use file-based fencing tokens and stale-lock cleanup.
- Queue operations are idempotent/retriable and run outside the main path locks.

Implications:

- AGFS is not globally single-writer.
- Concurrent writes to unrelated paths/subtrees can proceed.
- Concurrent writes under a hot subtree can serialize or time out.
- The path-lock layer is intended to coordinate multiple instances sharing AGFS.

This explains the homelab `bugs/mage/` bulk reindex issue: many semantic tasks attempted commits under the same parent subtree, producing lock contention/timeouts even with only one OpenViking process.

## Homelab-specific findings

### Current live deployment mode

The current cluster is running single-instance OpenViking, not the experimental parallel stack:

- `deployment/openviking`: `1/1`
- `deployment/embedder-llamacpp`: `1/1`
- `deployment/llamacpp-rocm`: `1/1`
- no active `ov-worker`, `ov-coordinator`, or `ov-merge` resources observed in live `kubectl -n viking get deploy,statefulset,svc`

The canonical deployment path in `viking/deploy-openviking.sh` applies:

- `openviking-deployment.yaml`
- `openviking-service.yaml`
- `openviking-standalone-configmap.yaml`

The script explicitly says coordinator/worker manifests are kept for experiments, not default rollout.

### Current standalone config

`viking/manifests/openviking-standalone-configmap.yaml` uses:

```json
{
  "storage": {
    "agfs": { "backend": "local" },
    "vectordb": { "backend": "local" },
    "transaction": {
      "lock_timeout": 30.0,
      "lock_expire": 300.0
    }
  },
  "server": { "workers": 1 },
  "embedding": { "max_concurrent": 1 },
  "vlm": { "max_concurrent": 1 }
}
```

This is a conservative single-writer/single-semantic-commit configuration chosen to avoid both embedded VectorDB process-lock contention and AGFS hot-subtree lock contention.

### Experimental parallel design

The homelab experimental manifests implement a custom sharded design:

- `ov-coordinator`: custom FastAPI proxy.
- `ov-worker` StatefulSet: multiple OpenViking pods.
- `ov-merge`: custom reconciliation service.
- `openviking` / `ov-merged`: canonical merged instance.

The coordinator routes writes by `md5(uri) % N` and fans out or uses merged reads depending on health/staleness.

Important nuance: workers do **not** each own a totally independent AGFS. The worker config uses shared S3 AGFS against the same Garage bucket (`openviking-agfs`) while each worker has its own local PVC/vector store. More precise wording:

> per-worker local VectorDB shards over shared S3 AGFS

### Merge service limitations

The custom `ov-merge` service is addition-only in the current manifest:

```python
missing = all_worker_uris - merged_uris
```

It reindexes missing URIs into the merged instance. It does not fully reconcile:

- changed existing URIs,
- deleted/stale URIs,
- conflict resolution between shards.

Also, the code default `MERGE_INTERVAL` is 60s, but the manifest sets:

```yaml
MERGE_INTERVAL: "300"
STALE_THRESHOLD: "600"
```

So homelab docs should say "default 60s, configured 300s" where relevant.

## Corrected answer to the original question

Question: can OpenViking split queue/indexing work across multiple GPUs and have writes cleanly go through to the VectorDB?

Answer:

- **With `storage.vectordb.backend: "local"`: no.** The embedded LevelDB/native vector engine is file-backed and takes an exclusive directory lock. Multiple OpenViking processes cannot open/write the same local store. Use one server process or per-worker local shards with a merge/reconciliation layer.
- **With a networked VectorDB backend: yes, in principle.** Multiple OpenViking instances can share AGFS and write to one shared vector service/database. On the deployed v0.3.14 that means `vikingdb`/`volcengine` (the `http` backend is only a single-service funnel); Qdrant and openGauss — the cleanest scalable shared stores — require an upgrade past v0.3.14.
- **AGFS remains a separate coordination layer.** Shared AGFS uses distributed path locks, so same-subtree writes serialize correctly but can still become a throughput bottleneck if many indexing tasks target one hot parent directory.

## Recommended homelab direction

If the goal is clean multi-GPU indexing without custom shard/merge complexity:

1. Keep GPU inference split across nodes/services (`manu` CUDA, `timmy` ROCm) as stateless model-serving endpoints.
2. Move `storage.vectordb.backend` off `local` to a networked backend. Candidates **available on the deployed v0.3.14 image**:
   - `vikingdb` (private VikingDB deployment) — closest to a true shared cluster store that runs on-prem.
   - `volcengine` (cloud VikingDB) — only if sending context to Volcengine cloud is acceptable.
   - `http` (bundled service) — decouples the store from app pods but is a single write-funnel, not horizontally scalable (see "What the bundled `http` backend actually wraps").
   - **Requires an OV upgrade first** (not in v0.3.14): Qdrant for a straightforward external vector service; openGauss if already operating Postgres/openGauss-style infrastructure. These are the cleanest "scalable shared store" options but are unavailable until OV is upgraded past v0.3.14.
3. Use shared AGFS, likely Garage S3 (`storage.agfs.backend: "s3"`).
4. Enable shared upload/task settings as needed for multi-instance HTTP routing:
   - `server.temp_upload.default_mode: "shared"` or client `upload.mode: "shared"` if upload and consume requests can land on different pods.
   - `storage.task_tracker.backend: "persistent"` if task IDs may be polled from different instances or must survive restart.
   - Review QueueFS mode/backend for the desired worker semantics.
5. Spread/batch indexing by independent subtrees where possible to avoid TREE-lock contention on hot directories.
6. Retire the custom coordinator/merge/per-local-shard machinery once a shared networked VectorDB path is validated.

## Caveats

- The homelab image currently uses `ghcr.io/volcengine/openviking:v0.3.14`. Current upstream docs/source are newer. **Verified 2026-06-01:** `qdrant` and `opengauss` adapters do **not** exist in the v0.3.14 tree (main-only); v0.3.14 offers `local`, `http`, `volcengine`, `vikingdb`. Re-verify after any image bump.
- The bundled `http` VectorDB backend centralizes access behind a service. **Confirmed:** that service is OpenViking's own native/LevelDB engine fronted by one FastAPI process — it avoids many OV app processes opening the same local DB, but is itself a single backend-service write-funnel. Qdrant/openGauss/VikingDB are the genuinely networked shared-store options (and Qdrant/openGauss require an upgrade past v0.3.14).
- AGFS distributed locks coordinate correctness, not unlimited throughput. Hot parent directories can still serialize semantic commit work.

## Final bottom line

For the current homelab `local` VectorDB deployment, the safe approach is still `server.workers: 1` and serialized semantic processing, or the experimental shard+merge workaround.

For upstream-aligned horizontal scaling, use shared S3 AGFS plus a networked VectorDB backend. That is the clean path for multi-GPU OpenViking workers writing into one shared index without per-shard merge reconciliation.
