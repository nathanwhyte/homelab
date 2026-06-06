# Homelab project

3-node K3s cluster running AI/RAG workloads. See [AGENTS.md](AGENTS.md) for repo conventions and safety rules. See [HARDWARE.md](HARDWARE.md) for node specs. See [GPU_AND_AI_REVIEW.md](GPU_AND_AI_REVIEW.md) for design decisions, benchmarks, and architecture history.

## Cluster topology

| Node | Role | GPU | Key workloads |
|------|------|-----|---------------|
| manu | worker | GTX 1080 8 GB | embedder-llamacpp (CUDA); llamacpp-cuda-ov, ov-coordinator (scaled to 0) |
| timmy | worker | RX 9070 XT 16 GB | ollama, openviking, ov-vectordb; llamacpp-rocm (VLM, scaled to 0 at idle — on-demand for indexing); ov-merge, ov-worker (scaled to 0) |
| wemby | CP + worker | GTX 1060 6 GB | ov-console |

## Service routing

| Service | NS | Endpoint | Port | Node | Notes |
|---------|-----|----------|------|------|-------|
| OV LLM | viking | `llamacpp-cuda-llm.viking.svc` | 80→8000 | manu | VLM inference only; selector: `app=llamacpp-cuda-ov`; **currently scaled to 0** |
| OV LLM (ROCm) | viking | `llamacpp-rocm-llm.viking.svc` | 80→8000 | timmy | VLM inference on AMD GPU; selector: `app=llamacpp-rocm`; **scaled to 0 at idle** — brought up on demand for indexing via `viking/tools/ov-vlm.sh` (holds ~9 GB resident) |
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
| Ollama Auth Proxy | llama | `ollama-auth-proxy.llama.svc` | 80→8080 | timmy | nginx Bearer-token auth (`Authorization: Bearer <ollama-api-key>`) |
| Prom remote-write (LAN) | grafana | `192.168.1.19` (NodePort) | 30909 | any | `prom-prometheus` NodePort `9090→30909`; `http://192.168.1.19:30909/api/v1/write` for external pushers (e.g. MacBook Alloy); no auth, LAN only |
| Loki push (LAN) | grafana | `192.168.1.19` (NodePort) | 31080 | any | `loki-gateway` NodePort `80→31080`; `http://192.168.1.19:31080/loki/api/v1/push` for external pushers; no auth, LAN only |
| Image Gen (FLUX) | — | `localhost:11434` | 11434 | MacBook (local) | Ollama `x/flux2-klein:9b` model (9B, non-commercial); `img-pipeline.sh generate`; 4B `x/flux2-klein` also available via `--model` |
| Image Understand (llama-server) | — | `127.0.0.1:8081` | 8081 | MacBook (local) | Qwen3.6-27B+mmproj; `img-pipeline.sh understand`; managed lifecycle via `img-pipeline.sh up/down`; fallback for Ollama vision path |

## LLM configuration

Exact tuning (ctx-size, batch/ubatch, KV cache, resources, env) lives in the manifests — these are the single source of truth. Summary of what runs where:

| Service | Node / GPU | Model | Slots | Role | Manifest |
|---------|-----------|-------|-------|------|----------|
| llamacpp-cuda-ov | manu / GTX 1080 | Qwen3-8B Q4_K_M | 4 | VLM (NVIDIA); **scaled to 0** — OV routes VLM to `llamacpp-rocm-llm` | `viking/manifests/cuda-llamacpp-deployment.yaml` |
| llamacpp-rocm | timmy / RX 9070 XT | Qwen3-8B Q4_K_M | 6 | VLM (AMD); **scaled to 0 at idle**, on-demand for indexing via `viking/tools/ov-vlm.sh`. Workers on timmy route here, on manu → `llamacpp-cuda-llm` | `viking/manifests/rocm-llamacpp-deployment.yaml` |
| embedder-llamacpp | manu / GTX 1080 | nomic-embed-text-v1.5 f16 | 8 | Embeddings, n-gpu-layers=999. Model in emptyDir — re-downloads on restart | `viking/manifests/embedder-llamacpp-deployment.yaml` |
| ollama | timmy / RX 9070 XT | gemma4:e4b + others | 1 | LoadBalancer; shares GPU with llamacpp-rocm via privileged access (no device-plugin claim) | `llama` ns |
| llama-server (local) | MacBook M5 Max | Qwen3.6-27B-uncensored-heretic-v2 Q4_K_M + mmproj | 1 | VLM (local, on-demand); `img-pipeline.sh up/down`; `/no_think` suffix required for direct responses; fallback for Ollama vision | `~/code/robots/media/pipeline/img-pipeline.conf` |
| homework-reader (Ollama local) | MacBook M5 Max | Qwen3.6-27B-uncensored-heretic-v2 Q4_K_M + mmproj (merged monolith) | 1 | Text + vision via Ollama; merged GGUF via `tsunagi-ollama-bridge --model-type qwen35`; capabilities: `completion, thinking, vision, tools`; `/no_think` template + `think:false` in API calls | `~/code/robots/media/pipeline/homework-reader.Modelfile` |
| homework-robot-nvfp4 (Ollama local) | MacBook M5 Max | Qwen3.6-27B-uncensored-heretic-v2 NVFP4 + mmproj (merged monolith) | 1 | Text + vision via Ollama; same template/params as homework-reader; NVFP4 quant (~18 GB); `think:false` required | `~/code/robots/media/pipeline/homework-robot-nvfp4.Modelfile` |
| homework-robot-nvfp4-mlp (Ollama local) | MacBook M5 Max | Qwen3.6-27B-uncensored-heretic-v2 NVFP4-MLP-Q8_0 + mmproj (merged monolith) | 1 | Text + vision via Ollama; same template/params as homework-reader; NVFP4-MLP quant (~20 GB); `think:false` required | `~/code/robots/media/pipeline/homework-robot-nvfp4-mlp.Modelfile` |
| Ollama FLUX | MacBook M5 Max | FLUX.2 Klein 9B (non-commercial) | 1 | Image gen; persistent Ollama service; `img-pipeline.sh generate`; 4B also available via `--model x/flux2-klein` | `~/code/robots/media/pipeline/img-pipeline.conf` |

All llamacpp services: flash-attn on, cont-batching, KV cache q4_0, `Strategy: Recreate`.

### VLM idle scaling (on-demand)

`llamacpp-rocm` (the VLM) holds ~9 GB resident and is only used during indexing (L0/L1 generation); reads/searches never touch it. Its manifest default is `replicas: 0`, so the idle cluster carries no VLM. Bring it up only for the duration of an index run with the wrapper:

```
viking/tools/ov-vlm.sh run -- python3 viking/tools/compendium-sync.py sync --limit 50
```

`run` scales the VLM to 1, waits for readiness (cold model load from the cached Longhorn PVC), runs the command, then `ov wait`s for OV's async index queue to drain before scaling back to 0 — indexing is async, so scaling down immediately after the command returns would kill in-flight VLM work. An EXIT trap returns it to 0 even on failure/Ctrl-C. `ov-vlm.sh up` / `down` / `status` manage the VLM manually for ad-hoc VLM-dependent operations (console/MCP writes). Requires the same `OPENVIKING_URL` / `OPENVIKING_KEY` env as the sync tool (for the `ov wait` drain).

### Image pipeline (MacBook local)

`~/code/robots/media/pipeline/img-pipeline.sh` wraps two local AI visual services on the MacBook M5 Max:

- **`generate "prompt"`** — Calls Ollama FLUX.2 Klein, decodes base64 PNG response, saves to `~/Pictures/ai-generated/` with timestamp filename
- **`generate --manage-ollama "prompt"`** — Same, but auto-starts Ollama if not running and stops it after generation (EXIT trap cleanup)
- **`understand <image>`** — Base64-encodes image, calls llama-server `/v1/chat/completions` with OpenAI vision format and `/no_think` suffix, prints description. Vision also works natively in Ollama via `homework-reader` (merged GGUF monolith); use `ollama run homework-reader "describe this" --image photo.jpg` or the Ollama API `images` field
- **`up`** — Starts llama-server with mmproj on port 8081 (if not running), ensures FLUX model is pulled in Ollama
- **`down`** — Stops llama-server (PID tracking + pkill fallback)
- **`ollama-up`** — Starts Ollama via `brew services start ollama`, waits for readiness
- **`ollama-down`** — Stops Ollama via `brew services stop ollama` (no-op if not running)
- **`status`** — Shows both services' state (Ollama + llama-server)
- **`run -- <cmd>`** — up → cmd → down, with EXIT trap (same pattern as `ov-vlm.sh`)

Config: `~/code/robots/media/pipeline/img-pipeline.conf` (model paths, ports, timeouts, output dir). The Qwen3.6-27B model uses `/no_think` by default to suppress thinking tokens; pass `--raw` to include them.

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

**Production config** — ConfigMap `openviking-standalone-config`: `agfs.backend: s3` (Garage bucket `openviking-agfs`) + `vectordb.backend: http` (`ov-vectordb.viking.svc.cluster.local:5000`). Tuning: `server.workers=2`, `embedding.batch_size=128`, `vlm.max_concurrent=4`, `embedding.max_concurrent=4`. Main OpenViking routes VLM to `llamacpp-rocm-llm`. History of the agfs:s3+vectordb:http cutover and rollback procedure: `viking/docs/2026-06-03-ov-prod-cutover-agfs-s3-vectordb-http.md`.

ConfigMap `openviking-config` is retained for workers if scaled back up (`agfs.backend: s3`, `storage.transaction` defaults). InitContainers in the `openviking` Deployment and `ov-worker` StatefulSet are backend-agnostic: S3 credentials injected only when `agfs.backend == "s3"`.

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
