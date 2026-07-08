# AGENTS

This repo contains Kubernetes manifests, Helm values, and small deploy scripts for a personal homelab cluster.

## Ground rules (safety first)

- **Do not apply changes to a live cluster unless explicitly asked.** Avoid running `kubectl apply`, `helm install/upgrade`, or anything that mutates cluster state unless the user requests it.
- **Do not introduce or commit secrets.**
  - Prefer referencing existing `Secret` resources (by name) rather than inlining secret material.
  - If a manifest must reference a secret key/token/cert, add a placeholder and document what’s required.
  - The repo ignores common sensitive patterns (see `.gitignore`), but still treat all credentials as out-of-scope.
- **Don’t change global tooling config** (git config, shell config, etc.) unless asked.

## Repo layout

Top-level directories generally map to deployed services:

- `cloudflare/`: Cloudflare tunnel manifests (main tunnel, per-namespace tunnels, Access config).
- `compendium/`: In-cluster compendium vault sync — `cluster-sync.sh`, sync Job template, state PVC, namespace.
- `copyparty/`: Copyparty file server (media NFS PV, Longhorn HDD storage class, snapshot policy).
- `dashboard/`: Kubernetes Dashboard manifests and tunnel/ingress config.
- `garage/`: Garage (S3-compatible object storage), values and a `manager/` subfolder for the garage manager manifests/config.
- `gpu/`: NVIDIA GPU Operator Helm values; GPU nodes are labeled `gpu=true`.
- `grafana/`: Observability stack (Prometheus/Grafana/Loki/Alloy), with Helm values under `grafana/helm/` and manifests under `grafana/manifests/`. Grafana is provisioned with Prometheus and Loki datasources; Alloy (k8s-monitoring) sends logs to Loki and metrics to Prometheus; Kubernetes and Logs dashboards are provisioned via the stack values.
- `harbor/`: Harbor registry, Helm values and supporting manifests.
- `headlamp/`: Headlamp Kubernetes UI — **torn down 2026-07-02** (see `headlamp/TORN-DOWN.md`); manifests retained for reference.
- `hermes/`: Hermes Agent deployment (agent + jump SSH terminal), ConfigMap, operator helper script. **Manifests only — not deployed live** (IMPR-1025); its mem0 memory backend was torn down 2026-07-02.
- `llama/`: Ollama and llamacpp LLM serving (chat proxy, auth proxy, cloud LLM counter).
- `longhorn/`: Longhorn storage, Helm values and supporting manifests.
- `mem0/`: Mem0 memory stack for Hermes (server, adapter, dashboard, Postgres/pgvector) — **torn down 2026-07-02** (see `mem0/TORN-DOWN.md`); manifests retained for reference.
- `omnipendium/`: Omnipendium knowledge-base API (FastAPI + Postgres/pgvector) and Slack bot (PROJ-028 stage 1).
- `openwebui/`: OpenWebUI Helm values, deploy script, and system prompt.
- `searxng/`: SearXNG metasearch engine (OpenWebUI's web-search backend).
- `syncthing/`: Syncthing peer for Obsidian/compendium vault sync (IDEA-1024/IDEA-1046) — **scaled to 0 since 2026-06-29** (caused vault duplication; peers sync directly over Tailscale). Manifests + architecture doc under `syncthing/docs/`.
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

## Minimal validation (when making changes)

When editing files, prefer quick, local checks:

- `bash -n <script>` for shell scripts (syntax check)
- If available: `shellcheck <script>` and/or `yamllint <file>`
- Keep manifests syntactically valid YAML (don’t rely on cluster-side validation)

## Worktree & Branch Conventions

**For multi-file updates and longer-running sessions, use git worktrees.** A worktree gives the session its own isolated working tree backed by a real git branch — safe from interruptions, crash-resilient, and mergeable back into `main` when done. This is the default for any task that touches more than a few files or spans an extended period (manifest refactors, multi-service updates, research docs).

### Setup

Worktrees live inside the repo's `.worktrees/` directory (already gitignored):

```bash
cd ~/code/homelab
git worktree add .worktrees/<slug> -b <branch> origin/main
cd .worktrees/<slug>
# Work here...
```

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
