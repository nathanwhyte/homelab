# Homelab project

3-node K3s cluster running AI/RAG workloads. See [AGENTS.md](AGENTS.md) for repo conventions and safety rules. See [HARDWARE.md](HARDWARE.md) for node specs. See [GPU_AND_AI_REVIEW.md](GPU_AND_AI_REVIEW.md) for design decisions, benchmarks, and architecture history.

## Cluster topology

| Node | Role | GPU | Key workloads |
|------|------|-----|---------------|
| manu | worker | GTX 1080 8 GB | llamacpp-cuda-ov, ov-coordinator, openviking |
| timmy | worker | RX 9070 XT 16 GB | ollama, llamacpp-rocm, embedder-llamacpp, ov-merge, ov-worker (preferred) |
| wemby | CP + worker | GTX 1060 6 GB | ov-console |

## Service routing

| Service | NS | Endpoint | Port | Node | Notes |
|---------|-----|----------|------|------|-------|
| OV LLM | viking | `llamacpp-cuda-llm.viking.svc` | 80→8000 | manu | VLM inference only; selector: `app=llamacpp-cuda-ov` |
| OV LLM (ROCm) | viking | `llamacpp-rocm-llm.viking.svc` | 80→8000 | timmy | VLM inference on AMD GPU; selector: `app=llamacpp-rocm`; workers on timmy route here |
| Embedder | viking | `embedder-llamacpp.viking.svc` | 8080→8000 | timmy | CPU-only (n-gpu-layers=0) |
| OpenViking | viking | `openviking.viking.svc` | 1933 | manu | Selector: `app=openviking`; single-instance local AGFS |
| ov-merged | viking | `ov-merged.viking.svc` | 1933 | manu | Selector: `app=openviking` (same pod as OpenViking) |
| ov-coordinator | viking | `ov-coordinator.viking.svc` | 1933 | — | **Scaled to 0**. Stateless proxy, not needed in single-instance mode |
| ov-merge | viking | `ov-merge.viking.svc` | 8080 | — | **Scaled to 0**. Not needed without workers |
| ov-console | viking | `ov-console.viking.svc` | 8020 | wemby | Web UI; `--write-enabled` |
| ov-worker | viking | headless | 1933 | — | **Scaled to 0**. 3-replica StatefulSet; can be restored for parallel indexing |
| Ollama | llama | `192.168.1.19` (LB) | 11434 | timmy | LoadBalancer; externalIP 192.168.1.19 |
| Ollama Exporter | llama | `192.168.1.19` (LB) | 9111 | timmy | Sidecar in ollama pod; python:3.12-slim |
| Ollama Auth Proxy | llama | `ollama-auth-proxy.llama.svc` | 80→8080 | timmy | nginx BasicAuth; Bearer token auth |

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

### Embedder — embedder-llamacpp (timmy, CPU-only)

| Setting | Value |
|---------|-------|
| Model | nomic-embed-text-v1.5 f16 |
| ctx-size | 16384 |
| parallel | 8 |
| n-gpu-layers | 0 (CPU-only) |
| rope-scaling | yarn, freq-scale 0.75 |
| pooling | mean |
| mlock | enabled |
| Resources | req 2/1Gi, lim 8/6Gi |

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

ConfigMap `openviking-standalone-config` uses `agfs.backend: local` with `storage.transaction` lock defaults. Main OpenViking routes VLM calls to `llamacpp-rocm-llm` with `vlm.max_concurrent=6`. Base config: `server.workers=1`, `embedding.batch_size=128`. Note: `server.workers=1` is required — multiple uvicorn workers per pod cause RocksDB VectorDB lock contention, crashing child processes and stalling the semantic queue.

ConfigMap `openviking-config` is retained for workers if scaled back up. Uses `agfs.backend: s3` against Garage with `storage.transaction` defaults added. InitContainers in both `openviking` Deployment and `ov-worker` StatefulSet are backend-agnostic: S3 credentials are conditionally injected only when `agfs.backend == "s3"`.

## Failover

Workers and coordinator are scaled to 0. To restore parallel indexing: `kubectl scale statefulset ov-worker --replicas=3 -n viking && kubectl scale deployment ov-coordinator --replicas=1 -n viking`. Workers use the shared `openviking-config` ConfigMap with `agfs.backend: s3` and S3 credentials.

If manu goes down, the `openviking` Deployment needs to be rescheduled on another node with PVC access (Longhorn handles replication).

## OpenViking knowledge base

See [OPENVIKING.md](viking/OPENVIKING.md) for the organization guide (L0/L1/L2 tiers, directory rules, what to index). See ~/code/CLAUDE.md for the save/search workflow rules.
