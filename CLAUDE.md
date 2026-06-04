# Homelab project

3-node K3s cluster running AI/RAG workloads. See [AGENTS.md](AGENTS.md) for repo conventions and safety rules. See [HARDWARE.md](HARDWARE.md) for node specs. See [GPU_AND_AI_REVIEW.md](GPU_AND_AI_REVIEW.md) for design decisions, benchmarks, and architecture history.

## Cluster topology

| Node | Role | GPU | Key workloads |
|------|------|-----|---------------|
| manu | worker | GTX 1080 8 GB | embedder-llamacpp (CUDA), llamacpp-cuda-ov (scaled to 0), ov-coordinator (scaled to 0) |
| timmy | worker | RX 9070 XT 16 GB | ollama, llamacpp-rocm, embedder-llamacpp, openviking, ov-merge (scaled to 0), ov-worker (scaled to 0) |
| wemby | CP + worker | GTX 1060 6 GB | ov-console |

## Service routing

| Service | NS | Endpoint | Port | Node | Notes |
|---------|-----|----------|------|------|-------|
| OV LLM | viking | `llamacpp-cuda-llm.viking.svc` | 80→8000 | manu | VLM inference only; selector: `app=llamacpp-cuda-ov`; **currently scaled to 0** |
| OV LLM (ROCm) | viking | `llamacpp-rocm-llm.viking.svc` | 80→8000 | timmy | VLM inference on AMD GPU; selector: `app=llamacpp-rocm`; workers on timmy route here |
| Embedder | viking | `embedder-llamacpp.viking.svc` | 8080→8000 | manu | CUDA on GTX 1080 (n-gpu-layers=999); ~70× faster than CPU |
| OpenViking | viking | `openviking.viking.svc` | 1933 | timmy | Selector: `app=openviking`; single-instance local AGFS |
| ov-merged | viking | `ov-merged.viking.svc` | 1933 | timmy | Selector: `app=openviking` (same pod as OpenViking) |
| ov-coordinator | viking | `ov-coordinator.viking.svc` | 1933 | — | **Scaled to 0**. Stateless proxy, not needed in single-instance mode |
| ov-merge | viking | `ov-merge.viking.svc` | 8080 | — | **Scaled to 0**. Not needed without workers |
| ov-console | viking | `ov-console.viking.svc` | 8020 | wemby | Web UI; `--write-enabled` |
| ov-worker | viking | headless | 1933 | — | **Scaled to 0**. 3-replica StatefulSet; can be restored for parallel indexing |
| ov-vectordb | viking | `ov-vectordb.viking.svc` | 5000 | timmy | HTTP vector service (`vectordb.backend: http` target); image `ghcr.io/volcengine/openviking:v0.3.14`; `python -m openviking.storage.vectordb.service.server_fastapi`; `VIKINGDB_PERSIST_PATH=/data/vikingdb`; **1 replica** (post-2026-06-03 cutover) |
| Ollama | llama | `192.168.1.19` (LB) | 11434 | timmy | LoadBalancer; externalIP 192.168.1.19 |
| Ollama Exporter | llama | `192.168.1.19` (LB) | 9111 | timmy | Sidecar in ollama pod; python:3.12-slim |
| Ollama Auth Proxy | llama | `ollama-auth-proxy.llama.svc` | 80→8080 | timmy | nginx BasicAuth; Bearer token auth |
| Prom remote-write (LAN) | grafana | `192.168.1.19` (NodePort) | 30909 | any | `prom-prometheus` NodePort `9090→30909`; `http://192.168.1.19:30909/api/v1/write` for external pushers (e.g. MacBook Alloy); no auth, LAN only |
| Loki push (LAN) | grafana | `192.168.1.19` (NodePort) | 31080 | any | `loki-gateway` NodePort `80→31080`; `http://192.168.1.19:31080/loki/api/v1/push` for external pushers; no auth, LAN only |

## LLM configuration

### OV LLM — llamacpp-cuda-ov (manu, NVIDIA GPU)

| Setting | Value |
|---------|-------|
| Model | Qwen3-8B Q4_K_M (`/models/current.gguf`) |
| ctx-size | 32768 (shared across 4 parallel slots) |
| parallel | 4 |
| KV cache | q4_0 (K + V) |
| flash-attn | on |
| cont-batching | enabled |
| reasoning-format | deepseek |
| batch | 2048 / ubatch 512 |
| GPU power limit | 220W (nvidia-smi -pl 220 in initContainer) |
| Resources | req 500m/3Gi+1GPU, lim 4/10Gi+1GPU |
| Strategy | Recreate |

### OV LLM (ROCm) — llamacpp-rocm (timmy, AMD GPU)

| Setting | Value |
|---------|-------|
| Model | Qwen3-8B Q4_K_M (`/models/current.gguf`) |
| ctx-size | 49152 (shared across 6 parallel slots; 8192 per slot) |
| parallel | 6 |
| KV cache | q4_0 (K + V) |
| flash-attn | on |
| cont-batching | enabled |
| reasoning-format | deepseek |
| batch | 2048 / ubatch 2048 |
| HIP_VISIBLE_DEVICES | 0 |
| Resources | req 500m/2Gi, lim 8/20Gi (no amd.com/gpu claim — shares GPU with Ollama) |
| GPU sharing | Coexists with Ollama via privileged access (no device plugin claim) |
| VLM routing | OV workers on timmy → `llamacpp-rocm-llm`; workers on manu → `llamacpp-cuda-llm` |
| Strategy | Recreate |

### Embedder — embedder-llamacpp (manu, CUDA GPU)

| Setting | Value |
|---------|-------|
| Model | nomic-embed-text-v1.5 f16 |
| ctx-size | 16384 |
| parallel | 8 |
| n-gpu-layers | 999 (full offload to GTX 1080) |
| rope-scaling | yarn, freq-scale 0.75 |
| pooling | mean |
| mlock | enabled |
| Resources | req 500m/1Gi+1GPU, lim 2/6Gi+1GPU |
| Image | `ghcr.io/ggml-org/llama.cpp:server-cuda` |
| runtimeClassName | nvidia |
| nodeSelector | `kubernetes.io/hostname=manu`, `gpu=true` |

**Note**: Model stored in emptyDir (500Mi limit) — re-downloads on every pod restart.

### Ollama (timmy, AMD ROCm GPU)

| Setting | Value |
|---------|-------|
| NUM_CTX | 65536 |
| FLASH_ATTENTION | 1 |
| KV_CACHE_TYPE | q4_0 |
| KEEP_ALIVE | 1h |
| NUM_PARALLEL | 1 |
| MAX_LOADED_MODELS | 1 |
| HIP_VISIBLE_DEVICES | 0 |
| Resources | req 500m/2Gi, lim 8/20Gi (no amd.com/gpu claim — shares GPU with llamacpp-rocm via privileged access) |
| Startup models | gemma4:e4b (warm), qwen3.5, qwen3.5-128k, mistral-nemo, devstral:24b, codestral:22b, starcoder2:15b |
| Strategy | Recreate |

## Network / Ingress

### Middlewares (viking ns, Traefik CRD)

| Name | Type | Detail |
|------|------|--------|
| openviking-basicauth | basicAuth | secret: `openviking-auth-secret` |
| openviking-https-redirect | redirectScheme | HTTP → HTTPS, permanent |
| ov-console-local-only | ipAllowList | 192.168.1.0/24, 10.42.0.0/16, 10.43.0.0/16 |
| ov-console-https-redirect | redirectScheme | HTTP → HTTPS, permanent |

### External routes

| Host | Backend | Entry | Middlewares | TLS |
|------|---------|-------|-------------|-----|
| `context.nathanwhyte.dev` | `openviking:1933` | websecure | openviking-basicauth | letsencrypt-prod |
| `context.nathanwhyte.dev` | `openviking:1933` | web | openviking-https-redirect | — |
| `viking.nathanwhyte.dev` | `ov-console:8020` | websecure | ov-console-local-only | letsencrypt-prod |
| `viking.nathanwhyte.dev` | `ov-console:8020` | web | ov-console-https-redirect | — |

## Secrets and config

| Secret | NS | Purpose |
|--------|-----|---------|
| openviking-auth-secret | viking | htpasswd for BasicAuth middleware |
| openviking-api-key | viking | OV API key (injected into coordinator, merge, workers) |
| openviking-s3-credentials | viking | Garage S3 credentials (injected into openviking config) |
| ollama-api-key | llama | Bearer token for auth proxy |

ConfigMap `openviking-standalone-config` uses `agfs.backend: s3` (against the `openviking-agfs` Garage bucket) and `vectordb.backend: http` (against the `ov-vectordb` FastAPI service on `ov-vectordb.viking.svc.cluster.local:5000`). This is the **current production default** as of 2026-06-03 — see `viking/docs/2026-06-03-ov-prod-cutover-agfs-s3-vectordb-http.md` for the cutover log and rollback procedure. Main OpenViking routes VLM calls to `llamacpp-rocm-llm`. Base config: `server.workers=1`, `embedding.batch_size=128`, `vlm.max_concurrent=1`, `embedding.max_concurrent=1`. The `server.workers=1` constraint from the local-vectordb era no longer applies (no embedded LevelDB lock), but is kept conservative for the post-cutover queue-drain period. Bump to 2-4 workers and `vlm.max_concurrent=4` after the carryover compendium is fully re-embedded and steady-state latency is observed.

**Cutover note (2026-06-03, completed):** the `agfs:s3`+`vectordb:http` combination is now live in production. The earlier single-backend attempt (only `vectordb:http`, against local `agfs`) had been rolled back because the AGFS subtree lock on `/local/default/resources/compendium` is a separate bottleneck from vectordb. The combined cutover validated by `viking/docs/2026-06-03-ov-test-agfs-s3-vectordb-http.md` (10 sibling writes in 5s, 0 `resource is busy` rejections) is what unlocked parallel writers. **`ov-vectordb` is now scaled to 1** (was 0 during the rolled-back state). The `openviking-data` PVC's local AGFS tree was wiped in step 3.1; the in-progress rehydration is from S3 carryover (May-10 data) plus the live queue. Full homelab + projects re-ingest is a follow-up via `viking/tools/reindex-all.sh`.

ConfigMap `openviking-config` is retained for workers if scaled back up. Uses `agfs.backend: s3` against Garage with `storage.transaction` defaults added. InitContainers in both `openviking` Deployment and `ov-worker` StatefulSet are backend-agnostic: S3 credentials are conditionally injected only when `agfs.backend == "s3"`.

## Failover

Workers and coordinator are scaled to 0. To restore parallel indexing: `kubectl scale statefulset ov-worker --replicas=3 -n viking && kubectl scale deployment ov-coordinator --replicas=1 -n viking`. Workers use the shared `openviking-config` ConfigMap with `agfs.backend: s3` and S3 credentials.

If timmy goes down, the `openviking` Deployment (nodeSelector `timmy`) needs to be rescheduled on another node with PVC access (Longhorn handles replication).

## OpenViking knowledge base

See [OPENVIKING.md](viking/OPENVIKING.md) for the organization guide (L0/L1/L2 tiers, directory rules, what to index). See ~/code/CLAUDE.md for the save/search workflow rules.

### Stack implementation report

Full implementation reference (components, access patterns, data flow, OV internals, parallel design, architecture diagram): [viking/docs/2026-06-04-openviking-stack-implementation-report.md](viking/docs/2026-06-04-openviking-stack-implementation-report.md).

**Executive summary:** OpenViking is a hierarchical RAG engine storing knowledge as a filesystem-shaped tree (`viking://` URIs, "AGFS") with auto-generated L0/L1/L2 semantic indices. It runs single-instance in the `viking` namespace, fronted by Traefik at `context.nathanwhyte.dev` (API + `/mcp`) and `viking.nathanwhyte.dev` (console). Since the 2026-06-03 cutover the file tree lives in S3/Garage (`agfs:s3`) and embeddings in the `ov-vectordb` HTTP service (`vectordb:http`) — moving two write-serializing locks to process-local scope. A nomic-embed embedder (manu GTX 1080) and a Qwen3-8B VLM (timmy RX 9070 XT) do embedding and L0/L1 generation. A coordinator/worker/merge trio for parallel indexing is built but scaled to 0.

### Indexed compendia

| Vault | Source | OV target | Sync tool |
|-------|--------|-----------|-----------|
| Work compendium | `~/code/compendium` | `viking://resources/compendium/` | `viking/tools/compendium-sync.py` (default) |
| Personal compendium | `~/code/personal-compendium` | `viking://resources/personal/` | `viking/tools/compendium-sync.py` with `COMPENDIUM_ROOT=~/code/personal-compendium OV_TARGET_BASE=viking://resources/personal` |

Both vaults use the same `compendium-sync.py` tool; the only difference is the env-var pair. Keep both namespaced under `resources/` so scoped `viking_find(..., scope="viking://resources/personal/")` queries stay clean.
