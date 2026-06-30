# Homelab project

3-node K3s cluster running AI/RAG workloads. See [AGENTS.md](AGENTS.md) for repo conventions and safety rules. See [HARDWARE.md](HARDWARE.md) for node specs. See [GPU_AND_AI_REVIEW.md](GPU_AND_AI_REVIEW.md) for design decisions, benchmarks, and architecture history.

## Cluster topology

| Node  | Role                            | GPU              | Key workloads                                                                      |
| ----- | ------------------------------- | ---------------- | ---------------------------------------------------------------------------------- |
| manu  | agent                           | GTX 1080 8 GB    | llamacpp-cuda-ov (VLM, **always on, replicas=1**); hermes-agent (not pinned to manu) |
| timmy | server (Control Plane + worker) | RX 9070 XT 16 GB | ollama, openviking, ov-vectordb, **embedder-qwen-rocm** (Qwen3-Embedding-4B, ROCm)  |
| wemby | agent                           | GTX 1060 6 GB    | _(idle GPU; `embedder-llamacpp` scaled-to-0 legacy rollback path until 2026-06-29)_  |

## Service routing

| Service                         | NS        | Endpoint                          | Port                         | Node                | Notes                                                                                                                                                                                                                       |
| ------------------------------- | --------- | --------------------------------- | ---------------------------- | ------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| OV LLM (unified)                | viking    | `llamacpp-vlm.viking.svc`         | 80→8000                      | manu                | Selector `vlm-pool: "true"` → `llamacpp-cuda-ov`; replicas=1 always on (1080 VLM-exclusive). Scale down manually to free GPU.                                                                                              |
| OV LLM (NVIDIA)                 | viking    | `llamacpp-cuda-llm.viking.svc`    | 80→8000                      | manu                | `llamacpp-cuda-ov`; Qwen3-8B IQ4_XS, ctx=32768, 4 slots; sole VLM backend.                                                                                                                                                    |
| Embedder                        | viking    | `embedder-qwen.viking.svc`        | 8080→8000                    | timmy               | Qwen3-Embedding-4B Q8_0 (2560 dim), ROCm on RX 9070 XT; `--ctx-size 32768 --parallel 4` = 8k/slot matching `embedding.max_input_tokens=8192`; model on `embedder-qwen-model-cache` PVC.                                     |
| Embedder (legacy, rollback)     | viking    | `embedder-llamacpp.viking.svc`    | 8080→8000                    | wemby               | nomic-embed-text-v1.5, **scaled to 0**; rollback path until 2026-06-29, then removable.                                                                                                                                      |
| OpenViking                      | viking    | `openviking.viking.svc`           | 1933                         | timmy               | Selector `app=openviking`; single-instance, AGFS + API + MCP; also via Cloudflare tunnel at `context.nathanwhyte.dev`.                                                                                                      |
| OpenViking (LAN NodePort)       | viking    | `192.168.1.19:31933` (NodePort)   | 31933→1933                   | any                 | `openviking-lan` NodePort; any node IP incl. Tailscale iface; OV `root_api_key`. See Endpoint tiers below.                                                                                                                  |
| ov-vectordb                     | viking    | `ov-vectordb.viking.svc`          | 5000                         | timmy               | HTTP vector service (`vectordb.backend: http`); `ghcr.io/volcengine/openviking:v0.4.4`; `VIKINGDB_PERSIST_PATH=/data/vikingdb`; 1 replica.                                                                                   |
| Ollama                          | llama     | `192.168.1.19` (LB)               | 11434                        | timmy               | LoadBalancer; externalIP 192.168.1.19.                                                                                                                                                                                       |
| Ollama Exporter                 | llama     | `192.168.1.19` (LB)               | 9111                         | timmy               | Sidecar in ollama pod; python:3.12-slim.                                                                                                                                                                                     |
| Ollama Auth Proxy               | llama     | `ollama-auth-proxy.llama.svc`     | 80                           | — (no nodeSelector) | nginx Bearer-token auth (`Authorization: Bearer <ollam...y>`).                                                                                                                                                                |
| Chat Ollama Proxy               | llama     | `chat-ollama-proxy.llama.svc`     | 11434                        | —                   | Reasoning-suppressing shim (`INJECT_REASONING_NONE=true`) for Hermes; routes to `ollama.llama.svc:11434`.                                                                                                                    |
| Ollama pop                      | —         | `ollama-pop.homelab.local:11434`  | 11434                        | MacBook (pop)       | pop's native MLX ollama linked in via `coredns-custom` ConfigMap (`k8s/coredns-custom-configmap.yaml`); resolves to `192.168.1.6` (router-reserved). Hermes `fallback_providers` entry for `qwen3.6:35b-mlx`; pop LaunchAgent `com.user.ollama-serve` binds `0.0.0.0:11434`. No public tunnel; LAN + Tailscale CGNAT only. See `llama/ollama-pop/README.md` and FEAT-1021. |
| Prom remote-write (LAN)         | grafana   | `192.168.1.19` (NodePort)         | 30909                        | any                 | `prom-prometheus` NodePort `9090→30909`; `http://192.168.1.19:30909/api/v1/write` for external pushers (e.g. MacBook Alloy); no auth, LAN only.                                                                              |
| Loki push (LAN)                 | grafana   | `192.168.1.19` (NodePort)         | 31080                        | any                 | `loki-gateway` NodePort `80→31080`; `http://192.168.1.19:31080/loki/api/v1/push`; no auth, LAN only.                                                                                                                          |
| Image Gen (FLUX)                | —         | `localhost:11434`                 | 11434                        | MacBook (local)     | Ollama `x/flux2-klein:9b` (9B, non-commercial); `img-pipeline.sh generate`; 4B `x/flux2-klein` via `--model`.                                                                                                                |
| Hermes Agent                    | hermes    | `hermes-agent.hermes.svc`         | 8642 (api), 9119 (dashboard) | — (no nodeSelector) | `registry.nathanwhyte.dev/homelab/hermes-agent-mem0:fe57dd3` (mem0 adapter sidecar; imagePullPolicy: Always); gateway + dashboard; remote gateway `hermes.nathanwhyte.dev` via Cloudflare tunnel; **mem0 memory provider**; OV KB tools via `OPENVIKING_ENDPOINT`. |
| Hermes Jump                     | hermes    | `hermes-jump.hermes.svc`          | 22                           | —                   | SSH terminal for Hermes; PVC-backed home in manifest (`hermes-jump-home`, 5Gi), not yet applied to live cluster (see `hermes/README.md`).                                                                                    |
| Image Understand (llama-server) | —         | `127.0.0.1:8081`                  | 8081                         | MacBook (local)     | Qwen3.6-27B+mmproj; `img-pipeline.sh understand`; lifecycle via `img-pipeline.sh up/down`; fallback for Ollama vision.                                                                                                       |
| copyparty                       | copyparty | `copyparty.copyparty.svc`         | 80→3923                      | wemby               | Consolidated file server (FEAT-1017). Serves `/files` (rwmd noot, `copyparty-files` PVC 200 Gi `longhorn-hdd-2x` 2-replica) + `/media` (r noot, `media-nfs` PVC fronting `yt-dlp/media` via NFS gateway). Image `copyparty/iv:1.20.16` digest-pinned (Pillow+pyvips+ffmpeg → image incl. HEIF/AVIF/JXL, video, audio thumbs + transcoding + ffprobe tags); args `-e2d --hist=/cfg --dotpart --no-robots --force-js`; thumb+sqlite cache in `cfg` emptyDir (10 GiB, ephemeral); daily Longhorn snapshots @ 04:00, 7-day retention (`copyparty-files-snap-daily`). |
| copyparty (Tailscale)           | copyparty | `100.95.215.105:31920` (NodePort) | 31920→3923                   | any                 | `copyparty-lan` NodePort, primary off-LAN endpoint. LAN equivalent `192.168.1.19:31920`. MacBook backup pushes via `~/code/dotfiles/bin/macbook-backup.sh` (LaunchAgent 6h) over Tailscale, not the public tunnel.            |
| copyparty (public)              | copyparty | `files.nathanwhyte.dev`           | 443                          | any                 | Bare Cloudflare tunnel → `copyparty.copyparty.svc:80`. Auth = shared account password (upgrade to Cloudflare Access deferred, INFO-1064). Browse + WebDAV. DNS via `cloudflared tunnel route dns` against tunnel `936478c5-b4a9-400c-8490-053451dda497`. |
| media NFS gateway               | yt-dlp    | `media-nfs.yt-dlp.svc`            | 2049,111,32765               | wemby               | `itsthenetwork/nfs-server-alpine:12` re-exports RWO `yt-dlp/media` PVC over NFSv4 RO; pinned to wemby for RWO same-node share with `yt-dlp-archive-push`; privileged (nfsd needs `/proc/fs/nfsd`). Cluster-scope PV `media-nfs-readonly` plumbs the Service IP into `media-nfs` PVC in `copyparty` ns, mounted RO at `/srv/media`. |
| syncthing (GUI)                 | syncthing | `syncthing-gui.syncthing.svc`     | 80→8384                      | timmy               | Always-on Syncthing peer for Obsidian/compendium vault sync (IDEA-1024, IDEA-1046). Deployment pinned to timmy (`nodeSelector`, colocated with Longhorn replica). PVC `syncthing-data` 50 Gi `longhorn-nvme` single-replica (OK — every peer holds a full copy). Image `docker.io/syncthing/syncthing:1.27.12` (TODO: digest-pin). Daily snapshots `syncthing-data-snap-daily` @ 04:30, 7-day retention. **GUI is ClusterIP-only** — `kubectl -n syncthing port-forward svc/syncthing-gui 8384:80`. GUI auth (`noot` / bcrypt) in `/var/syncthing/config/config.xml` on PVC. Architecture: `syncthing/docs/2026-06-25-syncthing-mesh-architecture.md`. |
| syncthing (sync)                | syncthing | `192.168.1.19:32200` / `100.95.215.105:32200` (Tailscale → timmy) | 32200→22000 (TCP+UDP), 32127→21027 (UDP) | any  | `syncthing-sync` NodePort — sync protocol entry point. Peers set explicit Addresses in their `timmy` device entry: `tcp://192.168.1.19:32200, tcp://100.95.215.105:32200, quic://192.168.1.19:32200, quic://100.95.215.105:32200, dynamic`. Cluster device ID `TV6TSSE-KGK7I5I-67NHCFE-5W2UWHK-5MSILTH-DANAGXN-SXFX5IF-JOV37AU`. Folder `compendium` shared with pop (`ZVRGSKY-…`), workmac (`GKF45N7-…`); iPad/phone TBD. timmy should be `Receive Only` for `compendium` so cluster-internal writes can't propagate. |

## LLM configuration

Exact tuning (ctx-size, batch/ubatch, KV cache, resources, env) lives in the manifests — single source of truth. Summary:

| Service              | Node / GPU          | Model                                                 | Slots | Role                                                                                                                                                                                                                                   | Manifest                                              |
| -------------------- | ------------------- | ----------------------------------------------------- | ----- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------- |
| llamacpp-cuda-ov     | manu / GTX 1080     | Qwen3-8B IQ4_XS, ctx=32768                            | 4     | **Primary VLM** (NVIDIA); replicas=1 always on (1080 VLM-exclusive). ~29-30 tok/s gen, ~414 tok/s prompt eval.                                                                                                                                                                                          | `viking/manifests/cuda-llamacpp-deployment.yaml`      |
| embedder-qwen-rocm   | timmy / RX 9070 XT  | Qwen3-Embedding-4B Q8_0, ctx=32768                    | 4     | **Primary embedder**. 2560-dim, `--pooling last`, `--n-gpu-layers 999`, `--flash-attn on`, `--batch-size 4096 --ubatch-size 4096`. ROCm, `privileged: true`, mounts `/opt/rocm-7.2.1/lib/rocblas/library`. Per-slot ctx 32768/4=8192 matching `embedding.max_input_tokens=8192`. Model on `embedder-qwen-model-cache` PVC. | `viking/manifests/embedder-qwen-rocm-deployment.yaml` |
| embedder-llamacpp    | wemby / GTX 1060    | nomic-embed-text-v1.5 f16, ctx=16384                  | 2     | **Retired** (scaled to 0); manifest kept as rollback path until 2026-06-29. n-gpu-layers=999, model on `wemby-model-cache` PVC.                                                                                                          | `viking/manifests/embedder-llamacpp-deployment.yaml`  |
| ollama               | timmy / RX 9070 XT  | gemma4:12b-it-qat (local), glm-5.1:cloud (remote)     | 6     | LoadBalancer; `OLLAMA_NUM_PARALLEL=6`, `OLLAMA_CONTEXT_LENGTH=16384` (fits mem0's ~9-10K extraction prompt), `OLLAMA_MAX_LOADED_MODELS=1`, `OLLAMA_KV_CACHE_TYPE=q4_0`, `OLLAMA_KEEP_ALIVE=3m`; chat proxy suppresses reasoning for local models. | `llama/ollama-deployment.yaml`                        |
| hermes-agent         | — (no nodeSelector) | glm-5.1:cloud (primary), gemma4:12b-it-qat (fallback) | 1     | `registry.nathanwhyte.dev/homelab/hermes-agent-mem0:fe57dd3` (mem0 adapter sidecar; imagePullPolicy: Always); s6-overlay supervises gateway + dashboard; SSH via hermes-jump (PVC-backed in manifest, not yet applied live); `hermes/operator.sh` CLI; **mem0 memory provider** (adapter on localhost:18080); OV KB tools via `OPENVIKING_ENDPOINT`; `args: ["gateway", "run"]`. | `hermes/hermes-deployment.yaml`                       |
| llama-server (local) | MacBook M5 Max      | Qwen3.6-27B-uncensored-heretic-v2 Q4_K_M + mmproj     | 1     | VLM (local, on-demand); `img-pipeline.sh up/down`; `/no_think` suffix for direct responses; fallback for Ollama vision.                                                                                                                 | `~/code/robots/media/pipeline/img-pipeline.conf`      |
| Ollama FLUX          | MacBook M5 Max      | FLUX.2 Klein 9B (non-commercial)                      | 1     | Image gen; persistent Ollama; `img-pipeline.sh generate`; 4B via `--model x/flux2-klein`.                                                                                                                                                | `~/code/robots/media/pipeline/img-pipeline.conf`      |

Local MacBook Ollama models (homework-reader, homework-robot-nvfp4, homework-robot-nvfp4-mlp) are documented in `~/code/robots/media/homework/CLAUDE.md`.

All current llamacpp services (cuda-llamacpp-deployment, embedder-qwen-rocm-deployment): flash-attn on, cont-batching, KV cache q4_0, `Strategy: Recreate`. The legacy `embedder-llamacpp` (scaled-to-0 rollback path) uses `Strategy: RollingUpdate` and does not set `--flash-attn` or `--cont-batching`.

### ROCm / RDNA4 guardrails

Timmy's RX 9070 XT (`gfx1201`) runs both Ollama and the embedder-qwen-rocm llama.cpp server. Rules for maintaining the ROCm serving path:

| Rule | Detail |
|---|---|
| GPU visibility | `HIP_VISIBLE_DEVICES=0` is mandatory in all ROCm manifests. Prevents iGPU (`gfx1036`) selection. |
| `HSA_OVERRIDE_GFX_VERSION` | Do NOT set. The 9070 XT is natively `gfx1201`; overriding masks real compatibility failures. |
| rocBLAS hostPath | Mount `/opt/rocm-7.2.1/lib/rocblas/library` only. Do NOT mount hipBLASLt (`/opt/rocm-7.2.1/lib/hipblaslt/library`) without a version-aligned benchmark. |
| `ROCBLAS_USE_HIPBLASLT` | Do NOT set. Benchmarked 2026-06-28: no performance difference, no warnings to suppress. |
| Image upgrades | Before bumping any ROCm image tag, run the gfx1201 validation commands in `llama/docs/2026-06-28-rocm-gfx1201-validation-baseline.md`. |
| vLLM | Experimental only — use a separate manifest/branch, not production Ollama. Verify `torch.cuda.is_available()` and `torch.cuda.device_count()` inside the container. For PyTorch/vLLM, also set `CUDA_VISIBLE_DEVICES=0` alongside `HIP_VISIBLE_DEVICES=0`. |
| ROCm tooling | Prefer `amd-smi` over legacy `rocm-smi`; prefer `rocprofv3` over legacy `rocprof`. |
| Host ROCm | Current baseline is ROCm 7.2.1. Do not upgrade without a benchmark justification. |

### VLM scaling (steady-state: always on)

VLM is steady-state `replicas=1`, always on. History + rationale: `GPU_AND_AI_REVIEW.md`. Manual control: `viking/tools/ov-vlm.sh up|down|status` (`down` drains the OV index queue first); `ov-vlm.sh run -- <cmd>` is an alias for "just run the command" (kept for compatibility).

### Image pipeline (MacBook local)

`img-pipeline.sh generate|understand|up|down|status` wraps two local AI visual services on the MacBook M5 Max. Full sub-command reference + config: `~/code/robots/media/CLAUDE.md` and `img-pipeline.conf`. The Qwen3.6-27B model uses `/no_think` by default to suppress thinking tokens; pass `--raw` to include them.

## Network / Ingress

### Middlewares (Traefik CRD)

| Name                      | NS     | Type           | Detail                                     |
| ------------------------- | ------ | -------------- | ------------------------------------------ |
| harbor-no-limit           | harbor | buffering      | Unlimited body size for image pushes       |
| openviking-https-redirect | viking | redirectScheme | HTTP → HTTPS, permanent                    |
| hermes-lan-only           | hermes | ipAllowList    | 192.168.1.0/24, 10.42.0.0/16, 10.43.0.0/16 |
| hermes-https-redirect     | hermes | redirectScheme | HTTP → HTTPS, permanent                    |

> `openviking-basicauth` (viking ns, basicAuth) exists in repo manifest (`viking/manifests/openviking-basicauth-middleware.yaml`) and is referenced by the Traefik Ingress in `viking/manifests/openviking-ingress.yaml`. The `/mcp` path has its own Ingress (`viking/manifests/openviking-mcp-ingress.yaml`) without BasicAuth so the native OpenViking MCP endpoint can be reached via Bearer token. `context.nathanwhyte.dev` is also reachable through the Cloudflare tunnel (see External routes table, marked "both"). Auth is OV `root_api_key` on every tier.

### Endpoint tiers

OpenViking is reachable on four tiers; pick by network location:

| Tier         | Endpoint                            | Use                                                               |
| ------------ | ----------------------------------- | ----------------------------------------------------------------- |
| In-cluster   | `http://openviking.viking.svc:1933` | pods / scripts running in the cluster                             |
| LAN NodePort | `http://192.168.1.19:31933`         | MacBook / on-LAN tools (`openviking-lan` NodePort, any node IP)   |
| Tailscale    | `http://100.95.215.105:31933`       | off-LAN MacBook (timmy Tailnet IP; NodePort binds all interfaces) |
| Public       | `https://context.nathanwhyte.dev`   | off-LAN / external / Pi agent (Cloudflare tunnel)                 |

The MacBook shell auto-selects LAN → Tailscale via a 0.5s health probe in `dotfiles/zsh/.zshrc`. Auth is OV `root_api_key` on every tier.

### External routes

Mix of Cloudflare tunnel (`cloudflare/main-tunnel/cloudflared-configmap.yaml`) and Traefik IngressRoutes. In the Notes column below, `CF` = Cloudflare-tunnel-only, `Ingress` = Traefik IngressRoute-only, `both` = both.

| Host                                     | Backend                                         | Auth                            | Notes                                                                                                                                                |
| ---------------------------------------- | ----------------------------------------------- | ------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| `context.nathanwhyte.dev`                 | `openviking:1933`                               | OV `root_api_key`               | API + `/mcp`; both                                                                                                                                   |
| `hermes.nathanwhyte.dev`                  | `hermes-agent:9119`                             | session token                  | Dashboard; both                                                                                                                                      |
| `viking.nathanwhyte.dev`                  | `ov-console:8020`                               | OV `root_api_key`               | OV console; Ingress (see `viking/manifests/ov-console-ingress.yaml`)                                                                                 |
| `llama.nathanwhyte.dev`                   | `ollama-auth-proxy`                             | Bearer token                   | CF                                                                                                                                                    |
| `api-mem0.nathanwhyte.dev`                | `mem0-server:8080`                              | `ADMIN_API_KEY`                 | Mem0 API; CF (dashboard's browser-side client uses this as `NEXT_PUBLIC_API_URL`)                                                                    |
| `ssh.nathanwhyte.dev`                     | SSH → timmy:22, user `noot`                     | Access service token + `homelab-breakglass` key | Break-glass SSH → timmy:22 (works with Tailscale off). Tunnel proxies raw TCP to timmy:22; break-glass pubkey in root-owned `/etc/ssh/auth_keys/%u` (sshd `sshd_config.d/10-breakglass.conf`). Hop from timmy to wemby/manu. Client `~/.ssh/config` sources the service-token env file in its ProxyCommand. |
| `mem0.nathanwhyte.dev`                    | `mem0-dashboard:3000`                           | —                               | Mem0 dashboard; CF                                                                                                                                  |
| `k8s.nathanwhyte.dev`                     | k8s-dashboard                                   | —                               | CF                                                                                                                                                    |
| `lamp.nathanwhyte.dev`                    | headlamp                                        | —                               | CF                                                                                                                                                    |
| `uploads.nathanwhyte.dev`                 | garage:3900                                     | —                               | S3; CF                                                                                                                                                |
| `logs.nathanwhyte.dev`                    | grafana                                         | —                               | CF                                                                                                                                                    |
| `longhorn.nathanwhyte.dev`                | longhorn-frontend                               | —                               | CF                                                                                                                                                    |
| `chat.nathanwhyte.dev`                    | open-webui                                      | —                               | CF                                                                                                                                                    |
| `nathanwhyte.dev` / `www.nathanwhyte.dev` | portfolio                                       | —                               | CF                                                                                                                                                    |

### Tailscale — private external access (PROJ-008)

Host-level Tailscale on all 3 nodes (WireGuard mesh) for **private admin/network access** from off-LAN. Complements the Cloudflare Tunnel (public web); the two coexist. Runbook: `tailscale/README.md`.

| Node  | Tailscale role   | Tailnet IP     | OS user |
| ----- | ---------------- | -------------- | ------- |
| manu  | HA subnet router | 100.114.66.32  | noot    |
| wemby | HA subnet router | 100.118.40.21  | natew   |
| timmy | plain node       | 100.95.215.105 | noot    |

- **Advertised routes**: `192.168.1.0/24` only (cluster CIDRs dropped — reachable via LAN route, advertising risked collision)
- **`--accept-routes`**: belongs only on **off-LAN client devices** (MacBook/phone). Do NOT set on the nodes themselves — they're physically on the advertised subnet and accepting the route causes asymmetric routing (ERR-007)
- **kubectl**: two contexts in `~/.kube/config` — `homelab` (LAN IP 192.168.1.19:6443 via subnet route) and `tailnet` (100.95.215.105:6443 direct; requires `--tls-san` in `/etc/rancher/k3s/config.yaml` on timmy, already done)
- **SSH**: `ssh wemby` / `ssh manu` / `ssh timmy` via `~/.ssh/config` aliases (all point to tailnet IPs). Wemby user is `natew`, not `noot`
- **Tailscale SSH** (`tailscale ssh`): ACL-gated keyless SSH also available; wemby requires `tailscale ssh natew@wemby`

## Secrets and config

| Secret                    | NS     | Purpose                                                                                                       |
| ------------------------- | ------ | ------------------------------------------------------------------------------------------------------------- |
| openviking-auth-secret    | viking | htpasswd for BasicAuth middleware                                                                              |
| openviking-api-key        | viking | OV API key (injected into coordinator, merge, workers)                                                         |
| openviking-s3-credentials | viking | Garage S3 credentials (injected into openviking config)                                                        |
| ollama-api-key            | llama  | Bearer token for auth proxy                                                                                   |
| hermes-api-server-key     | hermes | Bearer token for Hermes API server                                                                             |
| hermes-dashboard-token    | hermes | Session token for Hermes dashboard remote gateway                                                              |
| hermes-jump-ssh-key       | hermes | SSH key for Hermes terminal (hermes-jump host)                                                                 |
| openviking-api-key        | hermes | OV API key for Hermes OV KB tools (duplicated from `viking`; `hermes-openviking-api-key.secret.yaml.example`)  |
| hermes-grafana-token      | hermes | Grafana SA token (unused — Grafana MCP removed; retained for future sidecar)                                   |
| github-access-token       | hermes | GitHub PAT for hermes-jump `gh` CLI (in repo manifest, not mounted in live deployment)                           |

**Hermes agent config** — ConfigMap `hermes-config`:

- Model: `glm-5.1:cloud` (primary) via `chat-ollama.llama.svc:11434/v1` (reasoning-suppressing shim); `gemma4:12b-it-qat` fallback
- Memory: `provider: mem0` (Mem0 API server in `mem0` ns, writes PostgreSQL/pgvector); `memory_char_limit: 20000`, `user_char_limit: 12000`
- Ports: API 8642 (Bearer auth), dashboard 9119 (session-token, tunnel at `hermes.nathanwhyte.dev`); terminal SSH `hermes-jump.hermes.svc:22` (PVC-backed in manifest, not yet applied live)
- `GATEWAY_ALLOW_ALL_USERS=true`; image `registry.nathanwhyte.dev/homelab/hermes-agent-mem0:fe57dd3` (imagePullPolicy: Always); s6-overlay supervises gateway + dashboard; `fsGroup: 10000` / `fsGroupChangePolicy: OnRootMismatch`; `revisionHistoryLimit: 1`; `args: ["gateway", "run"]`
- CLI: `hermes/operator.sh` (`health`, `models`, `ask`, `run`, `logs`, `status`, `config`, `restart`)
- Delegation: `max_concurrent_children: 5`, `max_spawn_depth: 2`, `child_timeout_seconds: 900`; compression `threshold: 0.75`, `api_max_retries: 3`
- MCP servers: none currently (K8s MCP stdio incompatible with Hermes; Grafana lacks in-cluster endpoint; OV MCP redundant with bundled memory provider)

**Hermes OV knowledge-base tools** — Env: `OPENVIKING_ENDPOINT=http://openviking.viking.svc.cluster.local:1933` (in-cluster, avoids BasicAuth/WAF), `OPENVIKING_API_KEY` (from duplicated `openviking-api-key`), `OPENVIKING_ACCOUNT=default`, `OPENVIKING_USER=noot` (matches OV `default_user`), `OPENVIKING_AGENT=hermes`. Plugin injects `viking_search`, `viking_read`, `viking_browse`, `viking_remember`, `viking_add_resource`. OV is the knowledge-base provider only; agent memory uses mem0.

**Production config** — ConfigMap `openviking-standalone-config`:

- `agfs.backend: s3` (Garage bucket `openviking-agfs`) + `vectordb.backend: http` (`ov-vectordb.viking.svc.cluster.local:5000`)
- Tuning: `server.workers=2`, `embedding.batch_size=256`, `vlm.max_concurrent=4`, `embedding.max_concurrent=6`
- VLM endpoint: `http://llamacpp-vlm.viking.svc/v1` (generic VLM service, selector `vlm-pool: "true"` → `llamacpp-cuda-ov`)
- Auth: `server.auth_mode = "trusted"` (set 2026-06-23). Trusted mode still requires `root_api_key` on every request (host `0.0.0.0` non-localhost) and trusts `X-OpenViking-Account`/`X-OpenViking-User` headers — so root key + identity headers work for tenant-scoped data APIs (the pattern `compendium-sync.py` and local OV MCP rely on)
- Cutover + rollback procedure: `viking/docs/2026-06-03-ov-prod-cutover-agfs-s3-vectordb-http.md`

ConfigMap `openviking-config` is retained for workers if scaled back up (`agfs.backend: s3`). InitContainers inject S3 creds only when `agfs.backend == "s3"`. **Note:** this ConfigMap still has pre-Qwen-cutover values (768-dim nomic embedder, `vectordb.backend: local`, `max_input_tokens: 1900`, VLM pointing at Ollama) — update it to match `openviking-standalone-config` (2560-dim Qwen embedder, `vectordb.backend: http`, `max_input_tokens: 8192`, VLM at `llamacpp-vlm.viking.svc`) before scaling workers back up.

## Failover

Parallel indexing (coordinator/worker/merge trio) was removed at the 2026-06-03 single-instance cutover; manifests retained. Restore: `kubectl apply` `ov-coordinator`/`ov-merge`/`ov-worker`, then `kubectl scale statefulset ov-worker --replicas=3 -n viking && kubectl scale deployment ov-coordinator --replicas=1 -n viking` (shared `openviking-config`, `agfs.backend: s3` + S3 creds). If timmy goes down, reschedule `openviking` (nodeSelector `timmy`) on another node with PVC access (Longhorn replicates).

## OpenViking knowledge base

See [OPENVIKING.md](viking/OPENVIKING.md) for the organization guide (L0/L1/L2 tiers, directory rules, what to index). See ~/code/CLAUDE.md for the save/search workflow rules.

### Stack implementation report

Full reference (components, access patterns, data flow, OV internals, parallel design, architecture diagram): [viking/docs/2026-06-04-openviking-stack-implementation-report.md](viking/docs/2026-06-04-openviking-stack-implementation-report.md).

**Summary:** Hierarchical RAG engine — AGFS `viking://` tree with auto-generated L0/L1/L2 indices; single-instance in `viking` ns; fronted by Traefik at `context.nathanwhyte.dev` (API + `/mcp`) and `viking.nathanwhyte.dev` (console). Since 2026-06-03 the tree lives in S3/Garage (`agfs:s3`) and embeddings in `ov-vectordb` (`vectordb:http`). Embedder = Qwen3-Embedding-4B (timmy ROCm); VLM = Qwen3-8B (manu CUDA).

### Indexed compendia

| Vault               | Source               | OV target                        | Sync tool                         |
| ------------------- | -------------------- | -------------------------------- | --------------------------------- |
| Compendium (unified) | `~/code/compendium` | `viking://resources/compendium/` | `viking/tools/compendium-sync.py` |

Legacy `personal-compendium` was merged into `~/code/compendium` (June 2026, IDEA-034, +1000 ID offset); old repo archived at `~/code/archive/personal-compendium/`. Personal-band entries (IDs ≥ 1000) sync with `COMPENDIUM_ROOT=~/code/compendium OV_TARGET_BASE=viking://resources/personal/`.