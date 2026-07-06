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
| manu  | agent                           | GTX 1080 8 GB    | llamacpp-cuda-ov (**VLM failover only** since 2026-07-05 cloud cutover; retirement candidate); hermes-agent (not pinned to manu)                                                                                                                 |
| timmy | server (Control Plane + worker) | RX 9070 XT 16 GB | ollama, openviking, ov-vectordb, **embedder-qwen-rocm** (Qwen3-Embedding-4B, ROCm, **primary** — migrated back from wemby 2026-07-06; 9070 XT shares Ollama + embedder, ~5 GB)                                                                   |
| wemby | agent                           | GTX 1060 6 GB    | `embedder-qwen-cuda` **rollback (manifest-only: repo has replicas=0, Deployment not applied live)** — was primary 2026-06-29→07-06, moved off after repeated power drops (failing charging cable/port); `embedder-llamacpp` (deleted 2026-07-04) |

## AMD / RDNA4 guardrails

ROCm / RDNA4 guardrails for timmy's RX 9070 XT (the 1080 and 1060 are NVIDIA-only) live in [HARDWARE.md](HARDWARE.md#amd-rdna4-guardrails-timmys-rx-9070-xt).

## Network / Ingress

### Middlewares (Traefik CRD)

| Name                      | NS                   | Type           | Detail                                                                                                  |
| ------------------------- | -------------------- | -------------- | ------------------------------------------------------------------------------------------------------- |
| harbor-no-limit           | harbor               | buffering      | Unlimited body size for image pushes                                                                    |
| openviking-https-redirect | viking               | redirectScheme | HTTP → HTTPS, permanent                                                                                 |
| hermes-lan-only           | hermes               | ipAllowList    | 192.168.1.0/24, 10.42.0.0/16, 10.43.0.0/16                                                              |
| hermes-https-redirect     | hermes               | redirectScheme | HTTP → HTTPS, permanent                                                                                 |
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
| hermes-api-server-key     | hermes | Bearer token for Hermes API server                                                                                                                        |
| hermes-dashboard-token    | hermes | Session token for Hermes dashboard remote gateway                                                                                                         |
| hermes-jump-ssh-key       | hermes | SSH key for Hermes terminal (hermes-jump host)                                                                                                            |
| openviking-api-key        | hermes | OV API key for Hermes OV KB tools (duplicated from `viking`; `hermes-openviking-api-key.secret.yaml.example`)                                             |
| hermes-grafana-token      | hermes | Grafana SA token (unused — Grafana MCP removed; retained for future sidecar)                                                                              |
| github-access-token       | hermes | GitHub PAT for hermes-jump `gh` CLI (in repo manifest, not mounted in live deployment)                                                                    |

**Hermes agent config** — ConfigMap `hermes-config`:

- Model: `glm-5.1:cloud` (primary) via `chat-ollama.llama.svc:11434/v1` (reasoning-suppressing shim); `gemma4:12b-it-qat` fallback
- Memory: `provider: mem0` (Mem0 API server in `mem0` ns, writes PostgreSQL/pgvector); `memory_char_limit: 20000`, `user_char_limit: 12000`
- Ports: API 8642 (Bearer auth), dashboard 9119 (session-token, tunnel at `hermes.nathanwhyte.dev`); terminal SSH `hermes-jump.hermes.svc:22` (PVC-backed in manifest, not yet applied live)
- `GATEWAY_ALLOW_ALL_USERS=true`; image `registry.nathanwhyte.dev/homelab/hermes-agent-mem0:fe57dd3` (imagePullPolicy: IfNotPresent, immutable SHA tags — IMPR-1023); s6-overlay supervises gateway + dashboard; `fsGroup: 10000` / `fsGroupChangePolicy: OnRootMismatch`; `revisionHistoryLimit: 1`; `args: ["gateway", "run"]`
- CLI: `hermes/operator.sh` (`health`, `models`, `ask`, `run`, `logs`, `status`, `config`, `restart`)
- Delegation: `max_concurrent_children: 5`, `max_spawn_depth: 2`, `child_timeout_seconds: 900`; compression `threshold: 0.75`, `api_max_retries: 3`
- MCP servers: none currently (K8s MCP stdio incompatible with Hermes; Grafana lacks in-cluster endpoint; OV MCP redundant with bundled memory provider)

**Hermes OV knowledge-base tools** — Env: `OPENVIKING_ENDPOINT=http://openviking.viking.svc.cluster.local:1933` (in-cluster, avoids BasicAuth/WAF), `OPENVIKING_API_KEY` (from duplicated `openviking-api-key`), `OPENVIKING_ACCOUNT=default`, `OPENVIKING_USER=noot` (matches OV `default_user`), `OPENVIKING_AGENT=hermes`. Plugin injects `viking_search`, `viking_read`, `viking_browse`, `viking_remember`, `viking_add_resource`. OV is the knowledge-base provider only; agent memory uses mem0.

**Production config** — ConfigMap `openviking-standalone-config`:

- `agfs.backend: s3` (Garage bucket `openviking-agfs`) + `vectordb.backend: http` (`ov-vectordb.viking.svc.cluster.local:5000`)
- Tuning: `server.workers=2`, `embedding.batch_size=256`, `vlm.max_concurrent=4`, `embedding.max_concurrent=6`
- VLM: **cloud primary** `qwen3.5:cloud` (vision, 256K ctx) via `http://chat-ollama.llama.svc:11434/v1` (llama-ns cloud routing, Bearer from `ollama-api-key`); `vlm.backup` = local `http://llamacpp-vlm.viking.svc/v1` (selector `vlm-pool: "true"` → `llamacpp-cuda-ov`), automatic failover on 429/5xx/timeout (IDEA-1050, 2026-07-05)
- v0.4.7 posture (IMPR-1032): `git.enabled: false` (blocks silent `.ovgit` writes to Garage), `server.public_base_url: https://context.nathanwhyte.dev`, `temp_upload.default_mode: local`, native metrics on (`server.observability.metrics` + prometheus exporter; `/metrics` on 1933 is cluster-internal only — bypasses auth). Target-state reference: compendium INFO-1088
- Auth: `server.auth_mode = "trusted"` (set 2026-06-23). Trusted mode still requires `root_api_key` on every request (host `0.0.0.0` non-localhost) and trusts `X-OpenViking-Account`/`X-OpenViking-User` headers — so root key + identity headers work for tenant-scoped data APIs (the pattern `compendium-sync.py` and local OV MCP rely on)
- Cutover + rollback procedure: `viking/docs/2026-06-03-ov-prod-cutover-agfs-s3-vectordb-http.md`

ConfigMap `openviking-config` is retained for workers if scaled back up (`agfs.backend: s3`). InitContainers inject S3 creds only when `agfs.backend == "s3"`. Reconciled with `openviking-standalone-config` on 2026-07-02 (IMPR-1024): 2560-dim Qwen embedder, `vectordb.backend: http`, `max_input_tokens: 8192`, VLM at `llamacpp-vlm.viking.svc`, `auth_mode: trusted`; only `server.workers: 1` intentionally differs (per-worker instances). Repo manifest updated — re-apply the ConfigMap before scaling workers back up (the live copy may still be stale).

## Failover

Parallel indexing (coordinator/worker/merge trio) was removed at the 2026-06-03 single-instance cutover; manifests retained. Restore: `kubectl apply` `ov-coordinator`/`ov-merge`/`ov-worker`, then `kubectl scale statefulset ov-worker --replicas=3 -n viking && kubectl scale deployment ov-coordinator --replicas=1 -n viking` (shared `openviking-config`, `agfs.backend: s3` + S3 creds). If timmy goes down, reschedule `openviking` (nodeSelector `timmy`) on another node with PVC access (Longhorn replicates).

## OpenViking knowledge base

See [OPENVIKING.md](viking/OPENVIKING.md) for the organization guide (L0/L1/L2 tiers, directory rules, what to index). See ~/code/CLAUDE.md for the save/search workflow rules. Indexed-compendium table: [reference/compendium-index.md](reference/compendium-index.md).

### Stack implementation report

Full reference (components, access patterns, data flow, OV internals, parallel design, architecture diagram): [viking/docs/2026-06-04-openviking-stack-implementation-report.md](viking/docs/2026-06-04-openviking-stack-implementation-report.md).

**Summary:** Hierarchical RAG engine — AGFS `viking://` tree with auto-generated L0/L1/L2 indices; single-instance in `viking` ns; fronted at `context.nathanwhyte.dev` (API + `/mcp` + `/studio/` built-in web console; the separate `viking.nathanwhyte.dev` ov-console was removed). Since 2026-06-03 the tree lives in S3/Garage (`agfs:s3`) and embeddings in `ov-vectordb` (`vectordb:http`). Embedder = Qwen3-Embedding-4B (**timmy RX 9070 XT, ROCm, since 2026-07-06**; was wemby CUDA 2026-06-29→07-06, migrated back after wemby's failing charging cable/port kept hard-dropping the node — `embedder-qwen-cuda` retained as `replicas=0` rollback; runbook `viking/docs/2026-07-06-embedder-migration-wemby-to-timmy.md`); VLM = `qwen3.5:cloud` via llama-ns Ollama cloud routing since 2026-07-05 (manu Qwen3-8B demoted to `vlm.backup` failover).

### VLM scaling (steady-state: failover standby)

Since the 2026-07-05 cloud cutover the local VLM (`llamacpp-cuda-ov`) is a `vlm.backup` failover target, still `replicas=1` during the soak window; retire it (freeing manu's 1080) once the cloud path has soaked without failover incidents. Pre-cutover history + rationale: `GPU_AND_AI_REVIEW.md`. Manual control: `viking/tools/ov-vlm.sh up|down|status` (`down` drains the OV index queue first); `ov-vlm.sh run -- <cmd>` is an alias for "just run the command" (kept for compatibility).

### Image pipeline (MacBook local)

`img-pipeline.sh generate|understand|up|down|status` wraps two local AI visual services on the MacBook M5 Max. Full sub-command reference + config: `~/code/robots/media/CLAUDE.md` and `img-pipeline.conf`. The Qwen3.6-27B model uses `/no_think` by default to suppress thinking tokens; pass `--raw` to include them.
