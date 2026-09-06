# OpenViking endpoint + ov CLI / MCP doc audit

Date: 2026-06-22

## Infra changes to reconcile against

- LAN NodePort `192.168.1.19:31933` added (`viking/manifests/openviking-nodeport-service.yaml`) — low-latency LAN API+MCP, protected by OV `root_api_key`.
- Tailscale fallback `100.95.215.105:31933` (timmy Tailnet IP) added in `dotfiles/zsh/.zshrc` — NodePort binds all interfaces, so the same port works over Tailnet.
- `viking/tools/index-projects.py` default `OPENVIKING_URL` switched from `https://context.nathanwhyte.dev` to `http://openviking.viking.svc:1933` (in-cluster).
- `compendium/_scripts/compendium-sync.py`: `ov wait` drain replaced by `ov status --output json` polling; `--order interleave` (round-robin across parent dirs) added to avoid same-parent lock conflicts; `DEFAULT_OPENVIKING_URL` = LAN NodePort.
- `embedding.max_input_tokens=1900` set in `openviking-standalone-config` (nomic reports `n_ctx=2048` to llama.cpp regardless of `--ctx-size`; OV only truncates when `max_input_tokens` is set).
- Parallel coordinator/worker/merge trio removed from the cluster after the 2026-06-03 single-instance cutover (manifests retained in repo for restore).
- VLM steady-state `replicas=1`, always on (on-demand scaling retired 2026-06-11); `llamacpp-rocm` retired (commit `ebeffcc`).

## Endpoint model (target state)

| Use case                              | Endpoint                            |
| ------------------------------------- | ----------------------------------- |
| In-cluster scripts / pods             | `http://openviking.viking.svc:1933` |
| LAN MacBook / shell tools             | `http://192.168.1.19:31933`         |
| Tailnet MacBook off-LAN               | `http://100.95.215.105:31933`       |
| Public internet / Pi agent / external | `https://context.nathanwhyte.dev`   |

## Gap inventory

### A. Endpoint defaults still on the public URL (should be LAN / in-cluster)

| File:line                                          | Current                                                                       | Gap                                                                | Suggested fix                                                                                     |
| -------------------------------------------------- | ----------------------------------------------------------------------------- | ------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------- |
| `viking/tools/example.py:10,19`                    | docstring + default `https://context.nathanwhyte.dev`                         | Quickstart example points users at the public edge by default      | Default to in-cluster `http://openviking.viking.svc:1933`; keep public as an explicit-env example |
| `viking/deploy-openviking.sh:80,82`                | `echo "LAN: https://context.nathanwhyte.dev"`; health curl against public URL | Labels the public tunnel as "LAN"; ignores the actual LAN NodePort | Echo `http://192.168.1.19:31933` as LAN; keep public as "Public"                                  |
| `viking/deploy-openviking-parallel.sh:114,117,118` | same "LAN: <https://context>..." + parallel-trio smoke test                   | Same mislabel; also exercises the removed coordinator/worker trio  | Either delete (parallel mode decommissioned) or relabel + drop trio refs                          |
| `dotfiles/claude/.mcp.json` (openviking block)     | `https://context.nathanwhyte.dev/mcp`                                         | Intentionally public for portability (per fork note)               | Document rationale in-file comment; consider Tailscale URL as a commented alternative             |
| `dotfiles/pi/agent/mcp.json:9`                     | `https://context.nathanwhyte.dev/mcp`                                         | Pi is off-LAN/off-Tailnet → public is correct                      | No change; verify Pi cannot reach Tailnet (else could use Tailscale)                              |

### B. Docs presenting `context.nathanwhyte.dev` as _the_ API/MCP endpoint (no NodePort/Tailscale)

| File:line                                                                                                    | Current                                                                               | Gap                                                                                                            |
| ------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| `homelab/CLAUDE.md` service routing + external routes tables                                                 | only `openviking.viking.svc:1933` (in-cluster) and `context.nathanwhyte.dev` (public) | Canonical reference is missing the LAN NodePort `192.168.1.19:31933` and Tailscale `100.95.215.105:31933` rows |
| `homelab/README.md:95`                                                                                       | "API at `context.nathanwhyte.dev`, console at `viking.nathanwhyte.dev`"               | No LAN/Tailscale mention                                                                                       |
| `viking/docs/2026-06-04-openviking-stack-implementation-report.md` (quick reference ~525-526, diag 19,45-46) | Main API → `context.nathanwhyte.dev`; MCP → `context.nathanwhyte.dev/mcp`             | No NodePort/Tailscale; diagram labels public as the API surface                                                |
| `dotfiles/hermes/skills/k8s-cluster-ops/references/openviking.md:12`                                         | `context.nathanwhyte.dev → openviking:1933 (REST + MCP + WebDAV)`                     | Public-only mapping; line 46 correctly gives in-cluster base URL — add LAN/Tailscale rows                      |

### C. `ov wait` drain references (deprecated pattern)

| File:line                           | Current                                                                      | Gap                                                                                                                                                                                                                     |
| ----------------------------------- | ---------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `viking/tools/ov-vlm.sh:77-104,122` | `drain()` tries `ov wait --timeout` first, falls back to `ov status` polling | `ov wait` is the known-broken v0.3.14 `/api/v1/system/wait` endpoint; compendium-sync.py already migrated fully to `ov status` polling. ov-vlm.sh still leads with the broken call and logs "known mismatch" each time. | Drop the `ov wait` attempt; poll `ov status` directly (mirror compendium-sync.py's `_queue_is_drain`). Update comments referencing "queue drain goes through `ov wait`" (lines 23,37). |
| `viking/openviking.md:197`          | "Wait for completion \| `ov reindex <uri> --wait`"                           | This is `ov reindex --wait`, a flag on reindex, NOT the broken `ov wait` endpoint. Likely fine — verify `ov reindex --wait` still works on v0.3.14/v0.4.x.                                                              |

### D. Stale infra references (parallel trio / ROCm / on-demand VLM)

| File:line                                             | Current                                               | Gap                                                                                                                                                                                                                                  |
| ----------------------------------------------------- | ----------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `viking/deploy-openviking-parallel.sh` (whole script) | deploys + smokes the coordinator/worker/merge trio    | Trio removed from cluster 2026-06-03; CLAUDE.md says manifests retained for restore, but this deploy script presents parallel mode as live. Mark as archived/restore-only or delete.                                                 |
| `backlog/nanochat/deploy-nanochat.sh:30,33-35,45-47`  | scales `llamacpp-rocm`, `ov-worker`, `ov-coordinator` | References retired `llamacpp-rocm` and removed trio. Backlog dir — lower priority, but stale.                                                                                                                                        |
| `GPU_AND_AI_REVIEW.md`                                | (flagged by prior fork)                               | Stale embedder-on-timmy / ROCm references; needs refresh.                                                                                                                                                                            |
| `viking/openviking.md:280`, impl-report `:59,206`     | VLM = `llamacpp-vlm.viking.svc` (generic Service)     | Consistent with current live state (generic service routes to `llamacpp-cuda-ov`). Not wrong, but the generic-vs-named (`llamacpp-cuda-llm`) ambiguity documented in CLAUDE.md (PR #10 manifest pending apply) isn't reflected here. |

### E. ov CLI config / auth doc gaps

| File:line                                                    | Current                                                      | Gap                                                                                                                                                                                                      |
| ------------------------------------------------------------ | ------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `dotfiles/hermes/skills/devops/openviking-query/SKILL.md:16` | "account/user: `hermes`, root role"                          | Hermes deployment env is `ACCOUNT=default`, `USER=noot`, `AGENT=hermes`. Verify whether the hermes-jump `ovcli.conf` actually uses account/user `hermes` or if the SKILL doc is stale vs the deployment. |
| `dotfiles/zsh/.zshenv:12`                                    | comment corrected by prior fork (`~/.openviking/ovcli.conf`) | OK — no gap.                                                                                                                                                                                             |
| `dotfiles/zsh/.zshrc` OPENVIKING block                       | LAN/Tailscale auto-fallback added                            | OK — but the block still exports a single `OPENVIKING_URL`; the `ov` CLI also reads `~/.openviking/ovcli.conf`. The two must stay in sync — already commented. No gap.                                   |

### F. `embedding.max_input_tokens` / nomic n_ctx gotcha

| File:line                                                             | Current                                                                | Gap                                                                                                                                                                            |
| --------------------------------------------------------------------- | ---------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `viking/manifests/openviking-standalone-configmap.yaml`               | `max_input_tokens: 1900` set                                           | OK — committed.                                                                                                                                                                |
| `viking/manifests/openviking-configmap.yaml` (worker-retained config) | no `max_input_tokens`                                                  | If workers are scaled back up (parallel trio restore), they will hit the same nomic `n_ctx=2048` overflow. Add the setting for parity, or note in CLAUDE.md restore procedure. |
| `viking/openviking.md`, impl-report                                   | no mention of `max_input_tokens=1900` or the nomic `n_ctx=2048` gotcha | Document the tuning + root cause so it isn't lost.                                                                                                                             |

### G. compendium-sync.py new features undocumented externally

| Item                            | Current                                                  | Gap                                                                                                                                                     |
| ------------------------------- | -------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--order interleave`            | exists in argparse + implemented; no runbook mentions it | The recommended large-sync mode (interleave + `--delay 5 --no-wait`) isn't documented outside the script. Add to compendium CLAUDE.md / a sync runbook. |
| `--wait-drain` (status polling) | implemented; `--wait-drain` help string exists           | OK in-script; cross-reference from any runbook.                                                                                                         |

## Suggested fix order (by leverage)

1. `homelab/CLAUDE.md` — add LAN NodePort + Tailscale rows to the service routing / external routes tables (canonical reference, everything else keys off it).
2. `viking/tools/ov-vlm.sh` — drop the `ov wait` attempt, poll `ov status` directly; update comments.
3. `viking/tools/example.py` + `viking/deploy-openviking.sh` (+ parallel) — fix endpoint defaults/labels.
4. `viking/docs/2026-06-04-openviking-stack-implementation-report.md` + `README.md` + `k8s-cluster-ops/references/openviking.md` — add NodePort/Tailscale rows.
5. `viking/manifests/openviking-configmap.yaml` — add `max_input_tokens: 1900` for worker-restore parity; document the nomic gotcha in openviking.md.
6. Verify `openviking-query/SKILL.md:16` account/user vs hermes deployment env.
7. `GPU_AND_AI_REVIEW.md` refresh (separate, larger effort).

## H. Dotfiles drift (`~/code/dotfiles`) — audited 2026-06-22

The dotfiles are a major drift source. Findings by file:

### `hermes/skills/k8s-cluster-ops/references/openviking.md` — heavily stale

| Line    | Current                                                                                     | Gap                                                                                            |
| ------- | ------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| 7       | "v0.3.14"                                                                                   | Now v0.4.4                                                                                     |
| 12      | `context.nathanwhyte.dev → openviking:1933 (REST + MCP + WebDAV)`                           | Public-only; no LAN NodePort `192.168.1.19:31933` or Tailscale `100.95.215.105:31933`          |
| 13      | `embedder-llamacpp:8080 (nomic-embed on wemby/GTX 1080)`                                    | **Wrong GPU** — embedder is on wemby/GTX **1060** (moved in IDEA-009 Phase 2)                  |
| 14      | `llamacpp-rocm-llm:80 (Qwen3-8B on timmy/RX 9070 XT) — VLM for L0/L1`                       | **Stale** — VLM is `llamacpp-cuda-ov` on manu/GTX 1080, always-on; ROCm retired                |
| 24-27   | Active Pods: embedder=GTX 1080, `llamacpp-rocm` scaled to 0, `llamacpp-cuda-ov` scaled to 0 | Embedder=1060; rocm retired/removed; cuda-ov is 1/1 always-on                                  |
| 156     | `vlm.max_concurrent: 4`                                                                     | Now 2 (dropped in IDEA-009 Phase 3)                                                            |
| 157     | `vlm.provider/model → llamacpp-rocm-llm`                                                    | Stale — routes via `llamacpp-vlm` → `llamacpp-cuda-ov`                                         |
| 162-171 | "Known Issues: VLM scaled to 0 → L0/L1 fails", fix `kubectl scale deploy llamacpp-rocm`     | Entire section stale — VLM always-on since 2026-06-11; rocm retired                            |
| 191-200 | "Scaling the VLM" — idle-scaled replicas=0, only needed during indexing                     | Stale — always-on; `ov-vlm.sh` retained for manual GPU release only                            |
| 202-208 | "Parallel Workers (dormant)" restore cmd                                                    | Missing `ov-merge` scale; framing OK but incomplete                                            |
| —       | (missing)                                                                                   | No `max_input_tokens: 1900` / nomic `n_ctx=2048` gotcha; no `ov wait`→`ov status` polling note |

### `hermes/skills/k8s-cluster-ops/SKILL.md` — partially stale

| Line    | Current                                                                                                  | Gap                                                                                                                                                                                                                                     |
| ------- | -------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 301     | "Embedder health (nomic-embed on GTX 1080)"                                                              | **Wrong GPU** — wemby/GTX 1060                                                                                                                                                                                                          |
| 309     | "llamacpp-rocm is retired (not in cluster). llamacpp-cuda-ov should be 1/1 always-on."                   | ✅ correct                                                                                                                                                                                                                              |
| 317     | "`llamacpp-vlm` having no endpoints is expected when VLM deployments are scaled to 0"                    | Stale — VLM always-on, endpoints expected present                                                                                                                                                                                       |
| 323     | "llamacpp-cuda-ov should be 1/1... llamacpp-rocm retired"                                                | ✅ correct                                                                                                                                                                                                                              |
| 479     | "embedder... `max_input_tokens: 4096` guardrail was removed in PR #12... OV handles chunking"            | **Actively wrong now** — contradicts the `max_input_tokens: 1900` fix: nomic reports `n_ctx=2048` regardless of `--ctx-size` and OV does NOT auto-truncate unless `max_input_tokens` is set; the guardrail removal premise is incorrect |
| 483-484 | rocm manifests "0 replicas, idle-scaled"                                                                 | Should say retired (manifest retained for rollback)                                                                                                                                                                                     |
| 490     | `openviking-ingress.yaml \| Ingress + Middleware (basicauth, HTTPS redirect) \| context.nathanwhyte.dev` | **Stale** — CLAUDE.md confirms NO IngressRoutes in live cluster; route via Cloudflare tunnel; `openviking-basicauth` middleware NOT deployed                                                                                            |

### `hermes/skills/devops/openviking-query/SKILL.md`

| Line   | Current                                                               | Gap                                                                                                                                                                                                                                                        |
| ------ | --------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 16, 35 | `--account hermes --user hermes`, "account/user: `hermes`, root role" | Hermes-agent deployment env is `ACCOUNT=default`, `USER=noot`, `AGENT=hermes`. This is the ov CLI profile on hermes-jump (separate consumer from the memory provider) — verify the jump pod's `ovcli.conf` actually uses account/user `hermes`, else stale |
| 10, 22 | install via `uv tool install openviking`                              | `auto-install.sh:98` uses `bun i -g @openviking/cli` — inconsistent install method across dotfiles                                                                                                                                                         |
| —      | (missing)                                                             | No off-cluster guidance — all examples use in-cluster `openviking.viking.svc.cluster.local:1933`; nothing covers LAN NodePort / Tailscale for `ov` CLI from the MacBook                                                                                    |

### `claude/.mcp.json` + `pi/agent/mcp.json` — MCP endpoint

| File:line              | Current                                                                     | Gap                                                                                                                                                                                                                                       |
| ---------------------- | --------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `claude/.mcp.json:13`  | `https://context.nathanwhyte.dev/mcp`, `Bearer ${OPENVIKING_KEY}` (env var) | Intentionally public for portability; uses env var for key — OK. Consider commented Tailscale alternative.                                                                                                                                |
| `pi/agent/mcp.json:9`  | `https://context.nathanwhyte.dev/mcp`                                       | Public is correct for Pi (off-LAN/off-Tailnet) — OK                                                                                                                                                                                       |
| `pi/agent/mcp.json:11` | `"Authorization": "Bearer f6137a40bdf6...08e717"`                           | ⚠️ **Hardcoded OV API key committed to the dotfiles repo** (the claude config uses `${OPENVIKING_KEY}` env var instead). Rotate + replace with env-var or file reference. Low blast radius (homelab OV key) but still a committed secret. |

### `zsh/.zshenv:12-13` + `zsh/.zshrc:153-172`

| Line             | Current                                                                                        | Gap                                                                                                                                                                                                                                             |
| ---------------- | ---------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `.zshenv:12-13`  | "OV CLI reads from `~/.openviking/ovcli.conf`... OPENVIKING_* values are exported from .zshrc" | Misleading — implies the `ov` CLI consumes `OPENVIKING_*` env vars; actually only `compendium-sync.py` (and the hermes memory provider) do. The `ov` CLI reads `ovcli.conf`. Conflates the two consumers → confusion about which config to edit |
| `.zshrc:153-172` | LAN/Tailscale auto-fallback for `OPENVIKING_URL`                                               | ✅ correct (committed in prior fork)                                                                                                                                                                                                            |

### Dotfiles fix priority

1. `k8s-cluster-ops/references/openviking.md` — rewrite: v0.4.4, embedder=1060, VLM=cuda-ov always-on, rocm retired, `vlm.max_concurrent=2`, drop "VLM scaled to 0" section, add NodePort/Tailscale rows, add `max_input_tokens` gotcha.
2. `k8s-cluster-ops/SKILL.md:479` — correct the `max_input_tokens` guardrail narrative (it is NOT removed; 1900 is set); fix embedder GPU (301); fix Ingress/basicauth row (490).
3. `pi/agent/mcp.json:11` — rotate the hardcoded key, switch to env-var/file reference.
4. `openviking-query/SKILL.md` — verify account/user; add off-cluster `ov` CLI guidance (LAN NodePort/Tailscale).
5. `auto-install.sh:98` vs `openviking-query` install method — reconcile.
6. `.zshenv:12-13` — clarify the two consumers (ov CLI = ovcli.conf; compendium-sync = OPENVIKING_* env vars).
