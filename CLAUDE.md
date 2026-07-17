# Homelab project

3-node K3s cluster running AI/RAG workloads. See [AGENTS.md](AGENTS.md) for repo conventions and safety rules. See [HARDWARE.md](HARDWARE.md) for node specs. See [GPU_AND_AI_REVIEW.md](GPU_AND_AI_REVIEW.md) for design decisions, benchmarks, and architecture history.

**Reference tables** (kept out of this file so the SessionStart hook stays under the 40k char cap) — `Read` them when needed:

- [Service routing](reference/service-routing.md) — endpoints, ports, namespaces, per-service notes
- [LLM configuration](reference/llm-config.md) — model, ctx-size, slots, role, manifest path
- [External routes](reference/external-routes.md) — public hosts, auth, ingress type + Tailscale node table
- [Compendium index](reference/compendium-index.md) — OV sync targets, legacy personal-compendium archive pointer

## Cluster topology

| Node  | Role                            | GPU              | Key workloads                                                                                                                                                                                                                                    |
| ----- | ------------------------------- | ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| manu  | agent                           | GTX 1080 8 GB    | **embedder-qwen-cuda** (Qwen3-Embedding-4B, CUDA backend, **primary embedder** since the 2026-07-17 cutover — IMPR-1077; own `embedder-cuda-model-cache` PVC, ~5.7 GB at batch 512); llamacpp-cuda-ov (**VLM failover only** since 2026-07-05 cloud cutover; retirement candidate)                             |
| timmy | server (Control Plane + worker) | RX 9070 XT 16 GB | ollama, openviking, ov-vectordb, **embedder-qwen** (Qwen3-Embedding-4B, ROCm backend, **rollback only** — retained `replicas=1` during the cutover soak, scaling to 0 afterward; was primary until the 2026-07-17 move to manu/1080, IMPR-1077; renamed backend-neutral IMPR-1040)                             |
| wemby | agent                           | GTX 1060 6 GB    | no active workloads — `embedder-qwen-cuda` relocated to manu 2026-07-17 (IMPR-1077); `embedder-llamacpp` deleted 2026-07-04                                                                                                                       |

## AMD / RDNA4 guardrails

ROCm / RDNA4 guardrails for timmy's RX 9070 XT (the 1080 and 1060 are NVIDIA-only) live in [HARDWARE.md](HARDWARE.md#amd-rdna4-guardrails-timmys-rx-9070-xt).

## Network / Ingress

### Middlewares (Traefik CRD)

| Name                      | NS                   | Type           | Detail                                                                                                  |
| ------------------------- | -------------------- | -------------- | ------------------------------------------------------------------------------------------------------- |
| harbor-no-limit           | harbor               | buffering      | Unlimited body size for image pushes                                                                    |
| openviking-https-redirect | viking               | redirectScheme | HTTP → HTTPS, permanent                                                                                 |
| k8s-dashboard-lan-only    | kubernetes-dashboard | ipAllowList    | CRD exists, **not wired to the Ingress** — Traefik CRD provider bug, see external-routes.md (IMPR-1029) |
| longhorn-lan-only         | longhorn-system      | ipAllowList    | CRD exists, **not wired to the Ingress** — Traefik CRD provider bug, see external-routes.md (IMPR-1029) |

> OpenViking auth posture is **API-key-only** (IMPR-1007 Phase 4 decision, confirmed 2026-07-04): OV enforces `root_api_key` on every tier, no Traefik BasicAuth layer. The BasicAuth middleware manifest, secret example, and the dangling ingress annotation were removed 2026-07-04 (the Middleware was never deployed live, and the Cloudflare tunnel bypasses Traefik anyway). The `/mcp` path has its own Ingress (`viking/manifests/openviking-mcp-ingress.yaml`) so the native OpenViking MCP endpoint can be reached via Bearer token. `context.nathanwhyte.dev` is also reachable through the Cloudflare tunnel (see [External routes](reference/external-routes.md), marked "both").

OV endpoint tiers and the full external-routes + Tailscale tables live in [reference/external-routes.md](reference/external-routes.md).

## Secrets and config

| Secret                    | NS     | Purpose                                                                                                                                                   |
| ------------------------- | ------ | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| openviking-api-key        | viking | OV API key (injected into coordinator, merge, workers)                                                                                                    |
| openviking-s3-credentials | viking | Garage S3 credentials (injected into openviking config)                                                                                                   |
| ollama-api-key            | llama  | Bearer token for auth proxy                                                                                                                               |
| ollama-api-key            | viking | Duplicate of `llama/ollama-api-key` — config-rewrite injects it into OV `vlm.api_key` for the cloud VLM (IDEA-1050; `ollama-api-key.secret.yaml.example`) |

**Hermes** was retired 2026-07-16 — namespace, secrets (`hermes-api-server-key`, `hermes-dashboard-token`, `hermes-jump-ssh-key`, `hermes-grafana-token`, `github-access-token`, the hermes-scoped `openviking-api-key` duplicate), PVCs, and Cloudflare tunnel route deleted; see [`hermes/RETIRED.md`](hermes/RETIRED.md). mem0 (its memory backend) was torn down 2026-07-02 (`mem0/TORN-DOWN.md`); OpenViking's knowledge-base tooling has since covered the persistent-memory use case, so the project was retired rather than migrated to a new provider.

**Production config** — ConfigMap `openviking-standalone-config`:

- `agfs.backend: s3` (Garage bucket `openviking-agfs`) + `vectordb.backend: http` (`ov-vectordb.viking.svc.cluster.local:5000`)
- Tuning: `server.workers=2`, `embedding.batch_size=256`, `vlm.max_concurrent=8` (raised 4→8 2026-07-06 for resync throughput), `embedding.max_concurrent=6`
- VLM: **cloud primary** via `http://chat-ollama.llama.svc:11434/v1` (llama-ns cloud routing, Bearer from `ollama-api-key`) — `gemma4:31b-cloud` (non-thinking; settled 2026-07-06 after `qwen3.5:cloud` + `reasoning_effort` crashlooped OV v0.4.7, which doesn't recognize that config field — `reasoning_effort` must stay unset for any model here). `vlm.backup` = local `http://llamacpp-vlm.viking.svc/v1` (selector `vlm-pool: "true"` → `llamacpp-cuda-ov`), automatic failover on 429/5xx/timeout (IDEA-1050, 2026-07-05)
- v0.4.10 posture (IMPR-1032, image bumped to v0.4.10 by IMPR-1063 on 2026-07-16): `git.enabled: false` (blocks silent `.ovgit` writes to Garage), `server.public_base_url: https://context.nathanwhyte.dev`, `temp_upload.default_mode: local`, native metrics on (`server.observability.metrics` + prometheus exporter; `/metrics` on 1933 is cluster-internal only — bypasses auth). Target-state reference: compendium INFO-1088
- Auth: `server.auth_mode = "trusted"` (set 2026-06-23). Trusted mode still requires `root_api_key` on every request (host `0.0.0.0` non-localhost) and trusts `X-OpenViking-Account`/`X-OpenViking-User` headers — so root key + identity headers work for tenant-scoped data APIs (the pattern `compendium-sync.py` and local OV MCP rely on)
- Cutover + rollback procedure: `viking/docs/2026-06-03-ov-prod-cutover-agfs-s3-vectordb-http.md`

ConfigMap `openviking-config` is retained for workers if scaled back up (`agfs.backend: s3`). InitContainers inject S3 creds only when `agfs.backend == "s3"`. Reconciled with `openviking-standalone-config` on 2026-07-02 (IMPR-1024): 2560-dim Qwen embedder, `vectordb.backend: http`, `max_input_tokens: 8192`, VLM at `llamacpp-vlm.viking.svc`, `auth_mode: trusted`; only `server.workers: 1` intentionally differs (per-worker instances). Repo manifest updated — re-apply the ConfigMap before scaling workers back up (the live copy may still be stale).

## Failover

Parallel indexing (coordinator/worker/merge trio) was removed at the 2026-06-03 single-instance cutover; manifests retained. Restore: `kubectl apply` `ov-coordinator`/`ov-merge`/`ov-worker`, then `kubectl scale statefulset ov-worker --replicas=3 -n viking && kubectl scale deployment ov-coordinator --replicas=1 -n viking` (shared `openviking-config`, `agfs.backend: s3` + S3 creds). If timmy goes down, reschedule `openviking` (nodeSelector `timmy`) on another node with PVC access (Longhorn replicates).

## OpenViking knowledge base

See [OPENVIKING.md](viking/OPENVIKING.md) for the organization guide (L0/L1/L2 tiers, directory rules, what to index). See ~/code/CLAUDE.md for the save/search workflow rules. Indexed-compendium table: [reference/compendium-index.md](reference/compendium-index.md).

### Stack implementation report

Full reference (components, access patterns, data flow, OV internals, parallel design, architecture diagram): [viking/docs/2026-06-04-openviking-stack-implementation-report.md](viking/docs/2026-06-04-openviking-stack-implementation-report.md).

**Summary:** Hierarchical RAG engine — AGFS `viking://` tree with auto-generated L0/L1/L2 indices; single-instance in `viking` ns; fronted at `context.nathanwhyte.dev` (API + `/mcp` + `/studio/` built-in web console; the separate `viking.nathanwhyte.dev` ov-console was removed). Since 2026-06-03 the tree lives in S3/Garage (`agfs:s3`) and embeddings in `ov-vectordb` (`vectordb:http`). Embedder = Qwen3-Embedding-4B (**manu GTX 1080, CUDA, since the 2026-07-17 cutover** — IMPR-1077, `embedder-qwen-cuda` Deployment on its own `embedder-cuda-model-cache` PVC; the neutral `embedder-qwen` Service selects `app=embedder-qwen-cuda`, a cosmetic deviation from IMPR-1040's backend-neutral naming; the timmy RX 9070 XT ROCm `embedder-qwen` Deployment is retained `replicas=1` as rollback through the soak, then scaled to 0. Prior homes: wemby CUDA 2026-06-29→07-06, timmy ROCm 2026-07-06→07-17; runbook `viking/docs/2026-07-06-embedder-migration-wemby-to-timmy.md`); VLM = cloud via llama-ns Ollama routing since 2026-07-05, `gemma4:31b-cloud` (non-thinking, settled 2026-07-06); manu Qwen3-8B demoted to `vlm.backup` failover.

### VLM scaling (steady-state: failover standby)

Since the 2026-07-05 cloud cutover the local VLM (`llamacpp-cuda-ov`) is a `vlm.backup` failover target, still `replicas=1` during the soak window; retire it (freeing manu's 1080) once the cloud path has soaked without failover incidents. Pre-cutover history + rationale: `GPU_AND_AI_REVIEW.md`. Manual control: `viking/tools/ov-vlm.sh up|down|status` (`down` drains the OV index queue first); `ov-vlm.sh run -- <cmd>` is an alias for "just run the command" (kept for compatibility).

### Image pipeline (MacBook local)

`img-pipeline.sh generate|understand|up|down|status` wraps two local AI visual services on the MacBook M5 Max. Full sub-command reference + config: `~/code/robots/media/CLAUDE.md` and `img-pipeline.conf`. The Qwen3.6-27B model uses `/no_think` by default to suppress thinking tokens; pass `--raw` to include them.
