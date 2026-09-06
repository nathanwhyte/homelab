# Homelab project

3-node K3s cluster running AI/RAG workloads. This file is the canonical home for repo conventions and safety rules (`AGENTS.md` just points here). See [HARDWARE.md](HARDWARE.md) for node specs. See [GPU_AND_AI_REVIEW.md](GPU_AND_AI_REVIEW.md) for design decisions, benchmarks, and architecture history.

## Ground rules (safety first)

- **Do not apply changes to a live cluster unless explicitly asked.** Avoid running `kubectl apply`, `helm install/upgrade`, or anything that mutates cluster state unless the user requests it.
- **Do not introduce or commit secrets.**
  - Prefer referencing existing `Secret` resources (by name) rather than inlining secret material.
  - If a manifest must reference a secret key/token/cert, add a placeholder and document what’s required.
  - The repo ignores common sensitive patterns (see `.gitignore`), but still treat all credentials as out-of-scope.
- **Don’t change global tooling config** (git config, shell config, etc.) unless asked.

**Reference tables** (kept out of this file so the SessionStart hook stays under the 40k char cap) — `Read` them when needed:

- [Service routing](reference/service-routing.md) — endpoints, ports, namespaces, per-service notes
- [LLM configuration](reference/llm-config.md) — model, ctx-size, slots, role, manifest path
- [External routes](reference/external-routes.md) — public hosts, auth, ingress type + Tailscale node table
- [Compendium index](reference/compendium-index.md) — OV sync targets, legacy personal-compendium archive pointer

## Cluster topology

| Node  | Role                            | GPU              | Key GPU / AI workloads                                                                                                                                                                                                                                                                                                                                   |
| ----- | ------------------------------- | ---------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| manu  | agent                           | GTX 1080 8 GB    | **embedder-qwen-cuda** (Qwen3-Embedding-4B, CUDA backend, **primary embedder** since the 2026-07-17 cutover — IMPR-1077; own `embedder-cuda-model-cache` PVC, ~5.7 GB at batch 512); llamacpp-cuda-ov (**VLM failover only** since 2026-07-05 cloud cutover; retirement candidate)                                                                       |
| timmy | server (Control Plane + worker) | RX 9070 XT 16 GB | ollama, openviking, ov-vectordb, **embedder-qwen** (Qwen3-Embedding-4B, ROCm backend, **rollback only** — retained `replicas=1` during the cutover soak, scaling to 0 afterward; was primary until the 2026-07-17 move to manu/1080, IMPR-1077; renamed backend-neutral IMPR-1040)                                                                       |
| wemby | agent                           | GTX 1060 6 GB    | **reranker-bge** (bge-reranker-v2-m3 cross-encoder on llama.cpp `--reranking`, deployed 2026-08-26 for BUG-1016; **deployed but not wired into `ov.conf`** — measured as making OV retrieval worse, so `rerank` is back to threshold-only). Prior: `embedder-qwen-cuda` relocated to manu 2026-07-17 (IMPR-1077); `embedder-llamacpp` deleted 2026-07-04 |

> **This table covers GPU/AI workloads only** — it is not a full inventory, and an empty cell does not mean an idle node. Non-GPU services are spread across all three: wemby carries the largest share (glossary, copyparty, searxng, cert-manager, harbor core/portal, the k8s dashboard, the ollama auth + chat proxies, equal-risk, coach, yt-dlp gateway), timmy adds portfolio and the omnipendium stack alongside ollama/openviking, and manu adds the Postgres instances, buildkit, harbor registry/jobservice, and the GPU-operator + monitoring stacks. Check `kubectl get pods -A -o wide` before assuming a node can be drained; endpoints are in [reference/service-routing.md](reference/service-routing.md).

## AMD / RDNA4 guardrails

ROCm / RDNA4 guardrails for timmy's RX 9070 XT (the 1080 and 1060 are NVIDIA-only) live in [HARDWARE.md](HARDWARE.md#amd-rdna4-guardrails-timmys-rx-9070-xt).

## Network / Ingress

### Middlewares (Traefik CRD)

| Name                      | NS     | Type           | Detail                               |
| ------------------------- | ------ | -------------- | ------------------------------------ |
| harbor-no-limit           | harbor | buffering      | Unlimited body size for image pushes |
| openviking-https-redirect | viking | redirectScheme | HTTP → HTTPS, permanent              |

> **No `ipAllowList` middleware exists on any route, deliberately.**
> `k8s-dashboard-lan-only` and `longhorn-lan-only` were removed 2026-07-21
> (BUG-1057 resolved). They filtered nothing: k3s ServiceLB (klipper-lb)
> masquerades the client address in the `svclb-traefik` pod, beneath kube-proxy,
> so Traefik logged a node IP (`192.168.1.19`) as `ClientHost` for every request
> — which matched the allowlist's own `192.168.1.0/24`. Setting
> `externalTrafficPolicy` to `Local` does **not** fix it (it governs kube-proxy's
> SNAT, which never gets a say). Real filtering needs MetalLB or Traefik on
> `hostNetwork`/`hostPort`; anyone adding one back must verify with a **negative**
> test (out-of-range client actually gets `403`). Details:
> [reference/external-routes.md](reference/external-routes.md).
>
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

See [openviking.md](viking/openviking.md) for the organization guide (L0/L1/L2 tiers, directory rules, what to index). See ~/code/CLAUDE.md for the save/search workflow rules. Indexed-compendium table: [reference/compendium-index.md](reference/compendium-index.md).

### Stack implementation report

Full reference (components, access patterns, data flow, OV internals, parallel design, architecture diagram): [viking/docs/2026-06-04-openviking-stack-implementation-report.md](viking/docs/2026-06-04-openviking-stack-implementation-report.md).

**Summary:** Hierarchical RAG engine — AGFS `viking://` tree with auto-generated L0/L1/L2 indices; single-instance in `viking` ns; fronted at `context.nathanwhyte.dev` (API + `/mcp` + `/studio/` built-in web console; the separate `viking.nathanwhyte.dev` ov-console was removed). Since 2026-06-03 the tree lives in S3/Garage (`agfs:s3`) and embeddings in `ov-vectordb` (`vectordb:http`). Embedder = Qwen3-Embedding-4B (**manu GTX 1080, CUDA, since the 2026-07-17 cutover** — IMPR-1077, `embedder-qwen-cuda` Deployment on its own `embedder-cuda-model-cache` PVC; the neutral `embedder-qwen` Service selects `app=embedder-qwen-cuda`, a cosmetic deviation from IMPR-1040's backend-neutral naming; the timmy RX 9070 XT ROCm `embedder-qwen` Deployment is retained `replicas=1` as rollback through the soak, then scaled to 0. Prior homes: wemby CUDA 2026-06-29→07-06, timmy ROCm 2026-07-06→07-17; runbook `viking/docs/2026-07-06-embedder-migration-wemby-to-timmy.md`); VLM = cloud via llama-ns Ollama routing since 2026-07-05, `gemma4:31b-cloud` (non-thinking, settled 2026-07-06); manu Qwen3-8B demoted to `vlm.backup` failover.

### VLM scaling (steady-state: failover standby)

Since the 2026-07-05 cloud cutover the local VLM (`llamacpp-cuda-ov`) is a `vlm.backup` failover target, still `replicas=1` during the soak window; retire it (freeing manu's 1080) once the cloud path has soaked without failover incidents. Pre-cutover history + rationale: `GPU_AND_AI_REVIEW.md`. Manual control: `viking/tools/ov-vlm.sh up|down|status` (`down` drains the OV index queue first); `ov-vlm.sh run -- <cmd>` is an alias for "just run the command" (kept for compatibility).

### Image pipeline (MacBook local)

`img-pipeline.sh generate|understand|up|down|status` wraps two local AI visual services on the MacBook M5 Max. Full sub-command reference + config: `~/code/robots/media/CLAUDE.md` and `img-pipeline.conf`. The Qwen3.6-27B model uses `/no_think` by default to suppress thinking tokens; pass `--raw` to include them.

## Repo layout

Top-level directories generally map to deployed services:

- `arc/`: Self-hosted GitHub Actions runners — actions-runner-controller (gha-runner-scale-set) Helm values, deploy script, custom runner image (IDEA-1094). **Private repos only** — see `arc/arc.md`.
- `cloudflare/`: Cloudflare tunnel manifests (main tunnel, per-namespace tunnels, Access config).
- `compendium/`: In-cluster compendium vault sync — `cluster-sync.sh`, sync Job template, state PVC, namespace.
- `copyparty/`: Copyparty file server (media NFS PV, Longhorn HDD storage class, snapshot policy).
- `dashboard/`: Kubernetes Dashboard manifests and tunnel/ingress config.
- `garage/`: Garage (S3-compatible object storage), values and a `manager/` subfolder for the garage manager manifests/config.
- `gpu/`: NVIDIA GPU Operator Helm values; GPU nodes are labeled `gpu=true`.
- `grafana/`: Observability stack (Prometheus/Grafana/Loki/Alloy), with Helm values under `grafana/helm/` and manifests under `grafana/manifests/`. Grafana is provisioned with Prometheus and Loki datasources; Alloy (k8s-monitoring) sends logs to Loki and metrics to Prometheus; Kubernetes and Logs dashboards are provisioned via the stack values.
- `harbor/`: Harbor registry, Helm values and supporting manifests.
- `headlamp/`: Headlamp Kubernetes UI — **torn down 2026-07-02** (see `headlamp/TORN-DOWN.md`); manifests retained for reference.
- `hermes/`: Hermes Agent deployment (agent + jump SSH terminal), ConfigMap, operator helper script. **Retired 2026-07-16** — namespace deleted in full (see `hermes/RETIRED.md`); its mem0 memory backend was torn down 2026-07-02, and OpenViking's knowledge-base tooling has since covered the persistent-memory use case. Manifests retained for reference.
- `llama/`: Ollama and llamacpp LLM serving (chat proxy, auth proxy, cloud LLM counter).
- `longhorn/`: Longhorn storage, Helm values and supporting manifests.
- `mem0/`: Mem0 memory stack for Hermes (server, adapter, dashboard, Postgres/pgvector) — **torn down 2026-07-02** (see `mem0/TORN-DOWN.md`); manifests retained for reference.
- `omnipendium/`: Omnipendium knowledge-base API (FastAPI + Postgres/pgvector) and Slack bot (PROJ-028 stage 1).
- `openwebui/`: OpenWebUI Helm values, deploy script, and system prompt.
- `searxng/`: SearXNG metasearch engine (OpenWebUI's web-search backend).
- `syncthing/`: Syncthing peer for Obsidian/compendium vault sync (IDEA-1024/IDEA-1046) — **torn down 2026-07-25**, namespace and 50 GiB PVC deleted; the vault syncs through git. Manifests + architecture doc retained under `syncthing/`; see `syncthing/TORN-DOWN.md`. Do not re-deploy.
- `tailscale/`: Tailscale node setup scripts and WireGuard mesh configuration (PROJ-008).
- `viking/`: OpenViking RAG engine (standalone deployment, embedder, VLM, vectordb, sync tools, docs).

Supporting (non-service) directories:

- `_scripts/`, `scripts/`: repo lint helpers (`check-bare-fences.sh`) and ops scripts (`rotated-creds.sh`).
- `backlog/`: parked service configs not yet deployed (e.g. nanochat).
- `benchmarks/`: LLM benchmarking harnesses + results — Ollama/MLX edit-prediction (`ollama/`), embedding retrieval quality (`embedding-retrieval/`), Claude Code session-start latency (`claude-session/`), shared `lib/`, plots (`visualize/`), raw output (`results/`).
- `build/`: BuildKit daemon config for image builds.
- `k8s/`, `kube-system/`: cluster-wide odds and ends — CoreDNS custom ConfigMap; k3s datastore backup CronJob + crictl image prune.
- `mac/`: MacBook → cluster telemetry (Alloy, exporters, launchd units, pf rules) shipping Ollama/GPU/hardware metrics into the in-cluster stack.
- `media/`: yt-dlp tooling.
- `network/`: LAN device report and network docs.
- `reference/`: on-demand reference tables linked from `CLAUDE.md` (service routing, LLM config, external routes, compendium index).
- `reports/`: one-off investigation reports.

Each service folder typically includes:

- `deploy-*.sh`: an opinionated deploy script for that service
- `*-values.yaml`: Helm values (when deployed via Helm)
- `namespace.yaml`, `rbac.yaml`, `ingress.yaml`, `cloudflared.yaml`, etc.: supporting Kubernetes manifests

## Editing conventions

- **YAML style**
  - Keep indentation consistent (2 spaces).
  - Prefer explicit names/namespaces and stable labels/selectors.
  - Avoid large refactors/renames unless necessary; keep diffs reviewable.
- **Helm values**
  - Keep values files focused on overrides; prefer chart defaults where reasonable.
  - When adding configuration, include a short comment explaining _why_ (especially for security, persistence, ingress, or resource limits).
- **Scripts**
  - Keep deploy scripts idempotent where possible.
  - Avoid embedding tokens/credentials in scripts; require env vars or pre-created secrets instead.
- **Markdown filenames**
  - Nested markdown files are lowercase-kebab (e.g. `longhorn/alerting.md`).
  - Uppercase/title-case is reserved for: repo-root entry points (`README.md`, `HARDWARE.md`, `GPU_AND_AI_REVIEW.md`, `LICENSE.md`), agent-instruction files matched by basename at any depth (`CLAUDE.md`, `AGENTS.md`), `README.md` anywhere, and the lifecycle markers `TORN-DOWN.md` / `RETIRED.md`.
  - Vendored trees follow upstream casing.

## Minimal validation (when making changes)

When editing files, prefer quick, local checks:

- `bash -n <script>` for shell scripts (syntax check)
- If available: `shellcheck <script>` and/or `yamllint <file>`
- Keep manifests syntactically valid YAML (don’t rely on cluster-side validation)

## Worktree & Branch Conventions

**`~/code/homelab` is a BARE repository with sibling worktrees — it is not a normal working tree.** (Migrated 2026-07-19; the same layout `~/code/qpendium` uses.) There are no source files at the repo root — only git internals (`HEAD`, `config`, `objects/`, `refs/`, `worktrees/`). Every checkout lives in its own sibling directory:

```text
~/code/homelab/                          bare root — never edit or run builds here
├── main/                                the `main` branch  ← default working location
├── llama-timmy-edit-prediction/         llama/timmy-edit-prediction-node
└── impr-1068-embedder-vulkan/           worktree-impr-1068-embedder-vulkan
```

**Consequences for every session:**

- Run `git worktree list` before starting, and work in the worktree that owns your target branch.
- `main` lives at `~/code/homelab/main/`. **Never switch that worktree to a feature branch.**
- Paths that used to be `~/code/homelab/<dir>` are now `~/code/homelab/main/<dir>` — e.g. `~/code/homelab/main/compendium/cluster-sync.sh`, `kubectl apply -k ~/code/homelab/main/copyparty/`.
- Run status, tests, formatting, commits, and rebases from a worktree, never the bare root.
- Preserve uncommitted changes in every worktree; do not modify unrelated worktrees.

### Setup

Worktrees are sibling directories created from the bare root:

```bash
git -C ~/code/homelab worktree add <slug> -b <branch> origin/main
cd ~/code/homelab/<slug>
# Work here...
```

Remove one when finished (this deletes the directory, so commit or merge first):

```bash
git -C ~/code/homelab worktree remove <slug>
```

**Gotchas:**

- Worktree admin files store **absolute paths**. If the repo directory is ever moved or renamed, every worktree breaks with `prunable`. Fix with `git worktree repair <new-path>…` passing the new paths explicitly — **never** `git worktree prune`, which deletes the admin entries instead of repairing them.
- Local ignore patterns live in `~/code/homelab/info/exclude` (the bare-repo equivalent of `.git/info/exclude`). They are **not** tracked and are **not** copied by `git clone` — this file carries the `rotated-creds-*.txt` and `**/.claude/*` exclusions, so preserve it in any future repo move.
- Do not set `extensions.worktreeConfig`. With it enabled, the root's `core.bare = true` leaks into every linked worktree and breaks `git status` ("this operation must be run in a work tree").

### Branch naming

Follow the existing `type/description` convention used in this repo:

- `docs/drift-fix-<date>` — documentation corrections and drift fixes
- `feat/<short-description>` — new features or services
- `fix/<short-description>` — bug fixes
- `scan/<description>` — scan or audit outputs

All PRs target `main` directly.

### When to use a worktree

- Multi-file changes (manifest updates across services, config refactors)
- Long-running sessions that might be interrupted
- Parallel work on the same repo (one worktree per branch, never share)

### When NOT to use a worktree

- Simple single-file edits (one manifest tweak, one values override) — just commit on `main` directly.
- Read-only operations (querying, linting, searching, `kubectl get`).
- When the user asks for a specific branch name instead.

### Cleanup

After a PR merges or is closed:

```bash
cd ~/code/homelab
git worktree remove .worktrees/<slug>
git branch -d <branch>
```

Never leave orphaned worktrees — they lock branches and waste disk space. If a directory was deleted manually, run `git worktree prune` before adding new worktrees.

### Parallel work rules

- **One worktree per branch.** Never check out the same branch in two worktrees.
- **Rebase before push.** Always `git fetch origin main && git rebase origin/main` before pushing.
- **Clean up after merge.** Remove the worktree and delete the local branch when the PR is done.
- See the `git-parallel-work` skill for the full protocol (merge ordering, YAML conflict avoidance, post-merge cascade, cron PR supersession).

## Change hygiene

- Keep changes scoped to the service you're touching.
- Update `README.md` only when adding/removing services or materially changing how deployments work.
