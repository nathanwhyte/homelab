# Homelab project

3-node K3s cluster running AI/RAG workloads. See [AGENTS.md](AGENTS.md) for repo conventions and safety rules. See [HARDWARE.md](HARDWARE.md) for node specs. See [GPU_AND_AI_REVIEW.md](GPU_AND_AI_REVIEW.md) for design decisions, benchmarks, and architecture history.

## Cluster topology

| Node | Role | GPU | Key workloads |
|------|------|-----|---------------|
| manu | agent | GTX 1080 8 GB | llamacpp-cuda-ov (VLM, **always on — replicas=1, sole GPU workload**); hermes-agent; ov-coordinator (scaled to 0) |
| timmy | agent | RX 9070 XT 16 GB | ollama, openviking, ov-vectordb; llamacpp-rocm (retired, scaled to 0 — see Phase 4 banner); ov-merge, ov-worker (scaled to 0) |
| wemby | server (Control Plane + worker) | GTX 1060 6 GB | embedder-llamacpp (CUDA, persistent model cache on wemby-model-cache PVC); ov-console |

## Service routing

| Service | NS | Endpoint | Port | Node | Notes |
|---------|-----|----------|------|------|-------|
| OV LLM (unified) | viking | `llamacpp-vlm.viking.svc` | 80→8000 | manu | Generic VLM Service; selector `vlm-pool: "true"` → routes to `llamacpp-cuda-ov`. **Steady-state: replicas=1, always on.** The 1080 is VLM-exclusive (embedder moved to wemby's 1060 in IDEA-009 Phase 2; hermes-agent is CPU-only), so there's no GPU memory conflict and the on-demand scaling dance isn't worth the cold-load + in-flight-abort risk. To release the GPU for something else, scale down manually. |
| OV LLM (NVIDIA) | viking | `llamacpp-cuda-llm.viking.svc` | 80→8000 | manu | `llamacpp-cuda-ov`; Qwen3-8B IQ4_XS, ctx=32768, 2 slots; current sole VLM backend (Phase 3 onwards) |
| OV LLM (AMD, retired) | viking | `llamacpp-rocm-llm.viking.svc` | 80→8000 | timmy | `llamacpp-rocm`; **permanently retired** (IDEA-009 Phase 4, 2026-06-06). Selector label `vlm-pool` removed; manifest kept for rollback only. Do not scale up. |
| Embedder | viking | `embedder-llamacpp.viking.svc` | 8080→8000 | wemby | CUDA on GTX 1060; model cached on `wemby-model-cache` PVC. Moved off manu in IDEA-009 Phase 2 to free the 1080 for the VLM |
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
| Hermes Agent | hermes | `hermes-agent.hermes.svc` | 8642 (api), 9119 (dashboard) | manu | Official Docker image `nousresearch/hermes-agent:v2026.6.5`; gateway + dashboard; remote gateway at `hermes.nathanwhyte.dev` via Cloudflare tunnel; **OpenViking memory provider active** (in-cluster `openviking.viking.svc:1933`, writes to `viking://resources/patterns/` and `viking://resources/preferences/`) |
| Image Understand (llama-server) | — | `127.0.0.1:8081` | 8081 | MacBook (local) | Qwen3.6-27B+mmproj; `img-pipeline.sh understand`; managed lifecycle via `img-pipeline.sh up/down`; fallback for Ollama vision path |

## LLM configuration

Exact tuning (ctx-size, batch/ubatch, KV cache, resources, env) lives in the manifests — these are the single source of truth. Summary of what runs where:

| Service | Node / GPU | Model | Slots | Role | Manifest |
|---------|-----------|-------|-------|------|----------|
| llamacpp-cuda-ov | manu / GTX 1080 | Qwen3-8B IQ4_XS, ctx=32768 | 2 | **Primary VLM** (NVIDIA); **steady-state replicas=1, always on.** The 1080 is VLM-exclusive (embedder on wemby/1060, hermes-agent is CPU-only) so there's no GPU memory conflict. Earlier on-demand scaling was retired 2026-06-11 because cold model load + risk of cutting off in-flight L0 jobs made the dance not worth the saved watts. ~29-30 tok/s gen, ~414 tok/s prompt eval on the 1080. | `viking/manifests/cuda-llamacpp-deployment.yaml` |
| llamacpp-rocm | timmy / RX 9070 XT | Qwen3-8B IQ4_XS, ctx=57344 | 6 | **Retired** (IDEA-009 Phase 4, 2026-06-06). `vlm-pool` label removed from pod template so the unified service no longer routes here. Kept for rollback only. | `viking/manifests/rocm-llamacpp-deployment.yaml` |
| embedder-llamacpp | wemby / GTX 1060 | nomic-embed-text-v1.5 f16, ctx=16384 | 4 | Embeddings, n-gpu-layers=999. Model on `wemby-model-cache` PVC (no re-download on restart). | `viking/manifests/embedder-llamacpp-deployment.yaml` |
| ollama | timmy / RX 9070 XT | gemma4:12b-it-qat (local), glm-5.1:cloud (remote) | 1 | LoadBalancer; `OLLAMA_NUM_PARALLEL=3`, `OLLAMA_CONTEXT_LENGTH=131072`, `OLLAMA_MAX_LOADED_MODELS=1`, `OLLAMA_KV_CACHE_TYPE=q4_0`; chat proxy suppresses reasoning for local models | `llama/ollama-deployment.yaml` |
| hermes-agent | manu | glm-5.1:cloud (primary), gemma4:12b-it-qat (fallback) | 1 | Official image `nousresearch/hermes-agent:v2026.6.5`; s6-overlay manages gateway + dashboard; SSH terminal via hermes-jump (persistent `hermes-jump-home` PVC 5Gi — `uv`, Python 3.12, `gh`, git config survive restarts per PR #11); `hermes/operator.sh` for CLI; **OpenViking memory provider** (`OPENVIKING_ENDPOINT=http://openviking.viking.svc:1933`, `OPENVIKING_USER=noot`) | `hermes/hermes-deployment.yaml` |
| llama-server (local) | MacBook M5 Max | Qwen3.6-27B-uncensored-heretic-v2 Q4_K_M + mmproj | 1 | VLM (local, on-demand); `img-pipeline.sh up/down`; `/no_think` suffix required for direct responses; fallback for Ollama vision | `~/code/robots/media/pipeline/img-pipeline.conf` |
| Ollama FLUX | MacBook M5 Max | FLUX.2 Klein 9B (non-commercial) | 1 | Image gen; persistent Ollama service; `img-pipeline.sh generate`; 4B also available via `--model x/flux2-klein` | `~/code/robots/media/pipeline/img-pipeline.conf` |

Local MacBook Ollama models (homework-reader, homework-robot-nvfp4, homework-robot-nvfp4-mlp) are documented in `~/code/robots/media/homework/CLAUDE.md`.

All llamacpp services: flash-attn on, cont-batching, KV cache q4_0, `Strategy: Recreate`.

### VLM scaling (steady-state: always on)

`llamacpp-cuda-ov` (the VLM) holds ~6 GB resident on the GTX 1080 and is used for indexing (L0/L1 generation) and for `viking.nathanwhyte.dev` console / MCP writes that require VLM. **Steady-state: `replicas=1`, always on** (changed 2026-06-11 from on-demand scaling). The 1080 is VLM-exclusive — embedder moved to wemby's 1060 in IDEA-009 Phase 2, and hermes-agent and hermes-jump are CPU-only — so there's no GPU memory conflict. Earlier on-demand scaling was retired because (a) cold model load from the Longhorn PVC is ~40s, and (b) a premature scale-down can cut off in-flight L0 jobs the VLM is actively generating (this exact failure mode was hit during the 2026-06-10 sync cleanup, requiring a manual scale-back-up to drain the queue). Keeping the model warm is cheaper than the round-trip risk.

`viking/tools/ov-vlm.sh` is still available for manual control — `up` / `down` / `status` to manage the VLM ad-hoc (e.g. release the GPU if something else ever needs the 1080; `down` waits for the OV index queue to drain before scaling to 0, with the same `ov wait`-with-polling-fallback workaround the wrapper had). `ov-vlm.sh run -- <cmd>` is now an alias for "just run the command" since the VLM is already up — kept for compatibility with older sync recipes.

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

### Middlewares (Traefik CRD)

| Name | NS | Type | Detail |
|------|-----|------|--------|
| openviking-basicauth | viking | basicAuth | secret: `openviking-auth-secret` |
| openviking-https-redirect | viking | redirectScheme | HTTP → HTTPS, permanent |
| ov-console-local-only | viking | ipAllowList | 192.168.1.0/24, 10.42.0.0/16, 10.43.0.0/16 |
| ov-console-https-redirect | viking | redirectScheme | HTTP → HTTPS, permanent |
| hermes-lan-only | hermes | ipAllowList | 192.168.1.0/24, 10.42.0.0/16, 10.43.0.0/16 |
| hermes-https-redirect | hermes | redirectScheme | HTTP → HTTPS, permanent |

### External routes

| Host | Backend | Entry | Middlewares | TLS |
|------|---------|-------|-------------|-----|
| `context.nathanwhyte.dev` | `openviking:1933` | websecure | openviking-basicauth | cert-manager (letsencrypt-prod) |
| `context.nathanwhyte.dev` | `openviking:1933` | web | openviking-https-redirect | — |
| `viking.nathanwhyte.dev` | `ov-console:8020` | websecure | ov-console-local-only | cert-manager (letsencrypt-prod) |
| `viking.nathanwhyte.dev` | `ov-console:8020` | web | ov-console-https-redirect | — |
| `hermes.nathanwhyte.dev` | `hermes-agent:9119` | Cloudflare tunnel | session token auth | Cloudflare origin cert |

### Tailscale — private external access (PROJ-008)

Host-level Tailscale on all 3 nodes (WireGuard mesh) for **private admin/network access** from off-LAN. Complements the Cloudflare Tunnel (public web); the two coexist. Runbook: `tailscale/README.md`.

| Node | Tailscale role | Tailnet IP | OS user |
|------|---------------|------------|---------|
| manu | HA subnet router | 100.114.66.32 | noot |
| wemby | HA subnet router | 100.118.40.21 | natew |
| timmy | plain node | 100.95.215.105 | noot |

- **Advertised routes**: `192.168.1.0/24` only (cluster CIDRs dropped — reachable via LAN route, advertising them risked collision)
- **`--accept-routes`**: belongs only on **off-LAN client devices** (MacBook/phone). Do NOT set on the nodes themselves — they're physically on the advertised subnet and accepting the route causes asymmetric routing (ERR-007)
- **kubectl**: two contexts in `~/.kube/config` — `homelab` (LAN IP 192.168.1.9:6443 via subnet route) and `tailnet` (100.118.40.21:6443 direct; requires `--tls-san` in `/etc/rancher/k3s/config.yaml` on wemby, already done)
- **SSH**: `ssh wemby` / `ssh manu` / `ssh timmy` via `~/.ssh/config` aliases (all point to tailnet IPs). Wemby user is `natew`, not `noot`
- **Tailscale SSH** (`tailscale ssh`): ACL-gated keyless SSH also available; wemby requires `tailscale ssh natew@wemby`

## Secrets and config

| Secret | NS | Purpose |
|--------|-----|---------|
| openviking-auth-secret | viking | htpasswd for BasicAuth middleware |
| openviking-api-key | viking | OV API key (injected into coordinator, merge, workers) |
| openviking-s3-credentials | viking | Garage S3 credentials (injected into openviking config) |
| ollama-api-key | llama | Bearer token for auth proxy |
| hermes-api-server-key | hermes | Bearer token for Hermes API server |
| hermes-dashboard-token | hermes | Session token for Hermes dashboard remote gateway |
| hermes-jump-ssh-key | hermes | SSH key for Hermes terminal (hermes-jump host) |
| openviking-api-key | hermes | OV API key for Hermes memory provider (duplicated from `viking` namespace; `hermes-openviking-api-key.secret.yaml.example`) |
| hermes-grafana-token | hermes | Grafana SA token (unused — Grafana MCP removed; secret retained for future sidecar deployment) |
| github-access-token | hermes | GitHub PAT for hermes-jump `gh` CLI (copied from `build` namespace; `gh auth login --with-token` on first boot, persists on PVC) |

**Hermes agent config** — ConfigMap `hermes-config`: Model `glm-5.1:cloud` (primary) via `chat-ollama.llama.svc:11434/v1` (reasoning-suppressing shim proxy); `gemma4:12b-it-qat` as fallback. Memory: `provider: openviking` (bundled plugin, writes to `viking://resources/patterns/` and `viking://resources/preferences/`); limits `memory_char_limit: 11000`, `user_char_limit: 6875` (bumped 5× from 2200/1375 after agent reported "Memory is full"). Terminal: SSH to `hermes-jump.hermes.svc:22`. Dashboard: session-token auth on port 9119, exposed via Cloudflare tunnel at `hermes.nathanwhyte.dev`. API server: Bearer-token auth on port 8642. `GATEWAY_ALLOW_ALL_USERS=true` bypasses the auth-provider requirement (Hermes dashboard showed "no provider set up" with `auth_providers: []`). Official image `nousresearch/hermes-agent:v2026.6.5` with s6-overlay managing gateway + dashboard services. `fsGroup: 10000` ensures PVC files are writable by the hermes user. `hermes/operator.sh` provides CLI access (`health`, `models`, `ask`, `run`, `logs`, `status`, `config`, `restart`). Delegation: `max_concurrent_children: 3`, `max_spawn_depth: 2`, `child_timeout_seconds: 900`, `ollama_num_ctx: 8192`, `ollama_think: false` (tuned for parallel subagent workloads on 9070 XT per PR #9 VRAM analysis; see INFO-039/040 for cloud vs local profiles). All auxiliary sections also set `ollama_think: false` with per-role `ollama_num_ctx`: compression 8192, skills_hub/mcp/profile_describer 4096, approval/title_generation 2048. MCP servers: none currently (Kubernetes MCP stdio transport incompatible with Hermes; Grafana lacks in-cluster MCP endpoint; OpenViking MCP redundant with bundled memory provider). Will re-add as separate provider setup.

**Hermes OpenViking memory provider** — Env vars: `OPENVIKING_ENDPOINT=http://openviking.viking.svc.cluster.local:1933` (in-cluster, avoids BasicAuth and WAF), `OPENVIKING_API_KEY` (from `openviking-api-key` Secret duplicated in `hermes` namespace), `OPENVIKING_ACCOUNT=default`, `OPENVIKING_USER=noot` (must match OV's `default_user`), `OPENVIKING_AGENT=hermes`. Plugin injects 5 tools (`viking_search`, `viking_read`, `viking_browse`, `viking_remember`, `viking_add_resource`), prefetches context before each turn, mirrors built-in memory writes, and auto-extracts 6 memory categories on session end.

**Production config** — ConfigMap `openviking-standalone-config`: `agfs.backend: s3` (Garage bucket `openviking-agfs`) + `vectordb.backend: http` (`ov-vectordb.viking.svc.cluster.local:5000`). Tuning: `server.workers=2`, `embedding.batch_size=128`, `vlm.max_concurrent=2`, `embedding.max_concurrent=4` (was 4 each; `vlm.max_concurrent` dropped to 2 in IDEA-009 Phase 3 to match the VLM server's `--parallel 2` — over-commit caused every request to time out at 60s × 3 retries and stall the queue; PR #10 bumped to `max_retries=10` with `delay=min(2^attempt, 60)` backoff cap)). VLM endpoint: `http://llamacpp-cuda-llm.viking.svc/v1` (direct NVIDIA service; `llamacpp-vlm` generic service retired for VLM in PR #10 — stale endpoint caused timeouts). `embedding.max_input_tokens=4096` (token-truncation guard added in PR #10). History of the agfs:s3+vectordb:http cutover and rollback procedure: `viking/docs/2026-06-03-ov-prod-cutover-agfs-s3-vectordb-http.md`.

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
