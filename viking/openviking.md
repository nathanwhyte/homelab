# OpenViking Organization Guide

This project uses [OpenViking](https://github.com/volcengine/OpenViking) (v0.4.20) as a persistent knowledge base for AI agents.

## Core principle: Progressive addition, not bulk indexing

OV is for **knowledge artifacts** — decisions, debugging insights, architecture rationale, and learned patterns. The codebase itself is NOT indexed into OV. Agents read the repo directly.

Knowledge enters OV through two paths:

1. **Automatic**: Session commits extract memories into `viking://user/noot/memories/` (the same path without the `noot` user-id segment is rejected as `INVALID_URI` since v0.4.17). Agent-scoped extraction has not been observed on this deployment, so no agent destination is given here
2. **Manual**: Agents add notable findings via `viking_add_text` during normal work

### Before adding anything, ask three questions

1. **Can't be derived from code or git history?** — If `git log`, `git blame`, or reading the file answers it, don't add it.
2. **Would save significant time if rediscovered?** — Non-obvious root causes, gotchas, and configuration traps pass. Routine changes don't.
3. **Remains relevant going forward?** — If a later change superseded it, it's history, not knowledge. Skip it.

All three must be true. If in doubt, don't add — a lean index with high signal beats a comprehensive one with noise.

### What gets added (and when)

| Trigger                                    | Content                                                     | Target                           |
| ------------------------------------------ | ----------------------------------------------------------- | -------------------------------- |
| Non-obvious debugging discovery            | Root cause + fix + why it was hard to find                  | `resources/{project}/{service}/` |
| Architecture decision                      | Decision + rationale + alternatives considered + trade-offs | `resources/{project}/{service}/` |
| New service or major config change         | What it does, why it exists, key endpoints, gotchas         | `resources/{project}/{service}/` |
| Deployment state after significant changes | Final config, design decisions, mistakes corrected          | `resources/{project}/{service}/` |
| Project instruction files                  | CLAUDE.md, AGENTS.md (indexed on first session only)        | `resources/{project}/`           |

### What is NEVER added

- **Source code files** — the repo is the source of truth. Use `Read`/`Grep` directly.
- **Full codebase indexes** — bulk imports produce UUID dirs with empty abstracts that cripple search.
- **Command output or ephemeral state** — transient, no long-term value.
- **Duplicate content** — always `viking_search` before adding. Duplicates split retrieval scores.
- **Session logs or summaries** — use claude-mem for operational history. OV is for curated knowledge only.
- **Routine changes** — "updated config value X to Y" belongs in git, not OV. Only add if the _why_ is non-obvious.
- **Content from other memory systems** — don't bulk-import from claude-mem or similar. Selectively evaluate individual items against the three questions above.

## How retrieval works

Understanding OV's retrieval mechanism is essential for organizing content effectively.

### Three content layers

| Layer  | Name     | File                   | Token Limit | Purpose                         |
| ------ | -------- | ---------------------- | ----------- | ------------------------------- |
| **L0** | Abstract | `.abstract.md`         | ~100 tokens | Vector search, quick filtering  |
| **L1** | Overview | `.overview.md`         | ~2k tokens  | Rerank, content navigation      |
| **L2** | Detail   | Original files/subdirs | Unlimited   | Full content, on-demand loading |

The VLM auto-generates L0 and L1 for both files and directories. Well-written directory abstracts amplify all children's scores.

### Directory-recursive search algorithm

OV uses hierarchical retrieval, not flat vector search:

1. **Intent analysis** — LLM decomposes query into 0-5 TypedQueries (only with `search()`)
2. **Initial positioning** — Vector search scores L0 abstracts to locate high-scoring directories
3. **Recursive descent** — For each high-scoring directory, scores children using: `final_score = alpha * embedding_score + (1 - alpha) * parent_directory_score` (default alpha = 0.5)
4. **Convergence detection** — Stops when top-K results stabilize for 3 consecutive rounds
5. **Rerank** — L1 overviews used for final result scoring

**Key implication**: A well-named, well-abstracted parent directory amplifies all children's scores. A UUID-named directory with no abstract produces zero signal, making all its children invisible to search.

### `find()` vs `search()`

| Feature         | `find()`                        | `search()`                          |
| --------------- | ------------------------------- | ----------------------------------- |
| Session context | Not needed                      | Required                            |
| Intent analysis | Not used                        | LLM analysis                        |
| Query count     | Single query                    | 0-5 TypedQueries                    |
| Latency         | Low                             | Higher                              |
| Best for        | Simple lookups, scoped searches | Complex tasks, multi-intent queries |

**Always pass `scope=`** to narrow results. Example: `viking_find("VLM routing", scope="viking://resources/homelab/")`

## Official OV docs

- [Context Types](https://github.com/volcengine/OpenViking/blob/main/docs/en/concepts/02-context-types.md) — Resources, Memory, Skills
- [Context Layers](https://github.com/volcengine/OpenViking/blob/main/docs/en/concepts/03-context-layers.md) — L0/L1/L2 tiered loading
- [Viking URI](https://github.com/volcengine/OpenViking/blob/main/docs/en/concepts/04-viking-uri.md) — Scopes & path conventions
- [Retrieval](https://github.com/volcengine/OpenViking/blob/main/docs/en/concepts/07-retrieval.md) — Directory-recursive search, score propagation
- [Sessions](https://github.com/volcengine/OpenViking/blob/main/docs/en/concepts/08-session.md) — Session lifecycle & memory extraction

## Directory layout

Follow OV's default convention — one directory per project directly under resources:

```text
viking://resources/config/                    ← global agent config/instructions
viking://resources/config/{agent-name}/       ← per-agent configs (optional)
viking://resources/{project}/                 ← one per repo
viking://resources/{project}/{service}/       ← mirrors repo directory names
```

### Config directory (`resources/config/`)

Cross-project agent instructions and shared configuration. Agents can `viking_search("agent instructions config")` to discover global conventions.

```text
viking://resources/config/
  ├── global-claude-instructions    ← ~/.claude/CLAUDE.md
  ├── openviking-guide              ← viking/openviking.md (this file)
  └── {agent-name}/                 ← per-agent configs if needed
```

Per-project instruction files stay under their project:

```text
viking://resources/homelab/homelab-claude    ← homelab/CLAUDE.md
viking://resources/homelab/homelab-agents    ← homelab/AGENTS.md
```

### Project directories

```text
viking://resources/homelab/llama/
viking://resources/homelab/gpu/
viking://resources/dipdash/api/
```

### Directory structure rules

| Rule                                       | Why                                                                                         | Example                                                                                                      |
| ------------------------------------------ | ------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| **Real filenames only**                    | UUIDs/upload hashes produce zero L0 abstract signal, making children invisible to retrieval | `gpu-thermal-throttle.md` not `upload_a3f2.md`                                                               |
| **Mirror repo structure**                  | Predictable paths make scoped searches reliable                                             | `llama/` in repo → `homelab/llama/` in OV                                                                    |
| **3-4 levels max** below project root      | Retriever converges after ~3 rounds; deeper paths rarely get explored                       | `resources/homelab/gpu/thermal-assessment` is fine; `resources/homelab/gpu/nvidia/thermal/2024/` is too deep |
| **Group related content under topic dirs** | Parent directory abstracts amplify all children's scores                                    | `viking://resources/homelab/gpu/` with multiple GPU-related files under it                                   |
| **Descriptive kebab-case names**           | Names become part of the embedding; vague names hurt search                                 | `dual-gpu-assessment` not `gpu-research-1`                                                                   |
| **Use trailing slash for directories**     | Distinguishes dirs from files in URI operations                                             | `viking://resources/homelab/gpu/` (dir) vs `viking://resources/homelab/gpu.md` (file)                        |
| **Search before adding**                   | Duplicate content splits retrieval scores across copies                                     | Always `viking_find` or `viking_search` first                                                                |

### Recommended homelab structure

```text
viking://resources/
├── config/                          ← global agent config
│   ├── global-claude-instructions
│   └── openviking-guide
└── homelab/                         ← project root
    ├── homelab-claude               ← CLAUDE.md (indexed once)
    ├── homelab-agents               ← AGENTS.md (indexed once)
    ├── gpu/                         ← GPU-related knowledge
    │   ├── thermal-assessment
    │   └── dual-gpu-failover
    ├── llama/                       ← Ollama/LLM knowledge
    │   └── model-management
    ├── openviking/                  ← OV ops knowledge
    │   ├── vlm-dashscope-fix
    │   └── queue-stale-lock-fix
    └── k8s/                         ← cluster/infra knowledge
        └── storage-pv-migration
```

## Uploading best practices

### Two upload paths

| Method                                       | When to use                                                | How                              |
| -------------------------------------------- | ---------------------------------------------------------- | -------------------------------- |
| `viking_add_text(content, name, target_dir)` | Single knowledge artifact at moment of discovery (default) | In-line during work via MCP tool |
| `ov add-resource <file> --to <uri> --wait`   | Importing an existing file (docs, configs)                 | CLI, one-shot                    |
| `viking_session_commit`                      | End of multi-hour sessions with many interwoven findings   | Batch extraction by OV           |

### Upload workflow (API level)

Under the hood, `viking_add_text` performs two steps:

1. **Temp upload**: `POST /api/v1/resources/temp_upload` — uploads content
2. **Create resource**: `POST /api/v1/resources` — links temp file to a `viking://` URI

The MCP tool handles both steps automatically.

### Critical: avoid concurrent uploads

Always use `--wait` flag with CLI uploads. Without it, concurrent uploads compete for queue locks, causing semantic processing stalls (we hit this exact issue with VLM debugging).

Even with `--wait`, the semantic queue processor runs tasks concurrently based on `embedding.max_concurrent` and `vlm.max_concurrent`. When multiple tasks target the same AGFS subtree (e.g., bulk sync of `bugs/mage/`), AGFS EXACT/TREE path locks (logged as POINT/SUBTREE) contend and time out. Note: this is the AGFS transaction layer, not the VectorDB — distinct from the embedded LevelDB VectorDB process lock. Current production concurrency: `vlm.max_concurrent=1` (homelab #108, 2026-09-16 — set for the Ollama account-wide cap under BUG-1146 and kept for the local `gemma4:vlm` primary; the earlier `4` matched the retired llama.cpp VLM's `--parallel 4`), `embedding.max_concurrent=1` (lowered from 6 by IMPR-1074 to match the embedder's single `--parallel 1` slot; the earlier 6 dated to IDEA-1042 Phase 3 when the Qwen embedder briefly ran `--parallel 4`. **Kept at 1 when the embedder moved to two slots (homelab #156, 2026-09-22):** the semaphore is per event loop — the embedding queue worker and the API/recall loop each hold their own — so `1` already permits one background embed plus one recall embed in flight, which is the pairing the second slot exists for; raising it to 2 would let indexing occupy both slots and reserve nothing for recall. The IMPR-1074 rule is that the client fan-out must not exceed the slot count (excess only defers server-side into `llamacpp:requests_deferred`), not that the two numbers must be equal). Lock contention was resolved by moving AGFS to S3/Garage (process-local locks, not shared-filesystem locks). The 2026-05-30 `vlm.max_concurrent=1` guidance was about AGFS lock contention and became moot with the S3 cutover; today's `1` is about the VLM endpoint's capacity, not the locks.

**Lock configuration** (BUG-017 mitigations, applied 2026-06-16 — **superseded on v0.4.20**: both `lock_timeout` spellings are deprecated and ignored, the runtime wait is fixed at 0.0 s, and the ConfigMap deliberately sets none; see the comment atop `openviking-standalone-configmap.yaml`): `lock_timeout: 30.0` (was 5.0 — too low for 40s+ VLM calls), `v2_lock_retry_interval_seconds: 0.5`, `v2_lock_max_retries: 10`. These convert infinite-retry stalls into bounded, predictable failures and prevent premature lock expiry mid-VLM-call. The upstream architectural fix (GH #2015 — splitting source write from semantic refresh) is still pending.

**Embedder configuration** (IDEA-1042 cutover 2026-06-22, commit `2621fca`; relocated to manu's GTX 1080 CUDA 2026-07-17 by IMPR-1077): primary is now `embedder-qwen-cuda` on manu's GTX 1080 (CUDA) — `--parallel 2 --ctx-size 16384 --batch-size 512 --ubatch-size 512` as of homelab #156 (2026-09-22; the live value is whatever the Deployment's last apply set — check `n_slots` in the pod log): two 8192-token slots so a short recall query can join the batch beside a long overview chunk (order-dependent on this llama.cpp build — see the Deployment comment); the batch buffer dropped from 2048 (homelab #125, TASK-1217) to 512 to pay for the second slot's 1,152 MiB KV cache on the 8 GB card, a 4 % throughput cost; the Deployment manifest carries the VRAM arithmetic and the four-flag back-out (8192 tokens/slot, Qwen3-Embedding-4B Q8_0 GGUF, 2560-dim, last-token pooling). The backend-neutral `embedder-qwen` Service selects `app=embedder-qwen-cuda` since the cutover (IMPR-1040 keeps the Service name node/backend-invariant). The timmy RX 9070 XT ROCm deployment (`embedder-qwen`, ROCm, `--batch-size 4096`) is retained at replicas=0 as the rollback path — scale it to 1 and repoint the Service selector back to `app=embedder-qwen` to roll back. IMPR-1077 relocated the embedder off the 9070 XT to free that card for agentic workloads (superseding IMPR-1068's in-place Vulkan plan); backend-invariance was validated by TASK-1122 (Qwen-4B on the 9070 XT reproduced the wemby/Metal numbers exactly). Cutover/rollback runbook: `viking/docs/2026-07-06-embedder-migration-wemby-to-timmy.md`. Per-slot ctx must equal `embedding.max_input_tokens` or chunks above the slot size are rejected with "exceeds available context size" — caught the hard way during the original cutover when `--ctx-size 8192 --parallel 6` produced a 1536-token per-slot budget against 8192-token OV chunks (hence `--parallel 1`).

**`max_input_tokens` configuration** (IDEA-1042, commit `d34f683`): `embedding.max_input_tokens: 8192` in `openviking-standalone-config`. Qwen3-Embedding has a native 32K context so this is conservative chunk-safety, not a workaround. _(History: the prior nomic-embed-text-v1.5 setup required `max_input_tokens: 1900` because `embedder-llamacpp` reported `n_ctx: 2048` to clients regardless of the `--ctx-size` flag — a nomic-specific quirk. That guardrail no longer applies under Qwen.)_

For MCP-based uploads during a session, add one item at a time and verify it processes before adding the next.

### Large compendium sync (`compendium-sync.py`)

For bulk re-sync of a vault (hundreds of entries), use `viking/tools/compendium-sync.py` with the modes tuned to avoid same-parent SUBTREE-lock write conflicts during parent-overview refresh:

```bash
# Recommended large-sync mode (round-robins across parent dirs)
uv run viking/tools/compendium-sync.py \
  --order interleave --delay 5 --no-wait
```

- `--order interleave` — round-robins across parent directories instead of walking one directory at a time. Two entries in the same parent (e.g., `bugs/BUG-007.md`, `bugs/BUG-008.md`) both need the same parent's `.overview.md` regenerated, which serializes on the subtree lock. Interleaving spreads the same-parent clashes out in time so the lock is usually free by the time the second entry's parent-refresh runs.
- `--delay 5` — 5s pause between submissions, a soft rate cap that keeps the embedding/semantic queues from saturating.
- `--no-wait` — do not block on `ov wait` after each entry; submit and move on. The long-poll `ov wait` endpoint (`/api/v1/system/wait`) was broken on v0.3.14 (returned "Network error" after 60s even when healthy; not re-verified on v0.4.4 or v0.4.7), so `--no-wait` skips it. Use `--wait-drain` at the end of the run to block until the queue is empty instead.

Queue-drain options (both poll `ov status --output json` — the `status["result"]["components"]["queue"]["status"]` TOTAL row — rather than the broken `ov wait`):

- `--wait-drain` — after submitting all entries, poll until pending + in-progress = 0 (the script's `_queue_is_drain` / `wait_for_drain` logic). Use this when you need to know the sync fully settled before proceeding.
- `--no-wait` — fire-and-forget; check `ov status` yourself later. Right for unattended bulk syncs where you'll verify the queue separately.

Personal-band entries (IDs ≥ 1000) live in the same unified vault (`COMPENDIUM_ROOT=~/code/compendium`) and sync to the same default target, `viking://resources/compendium/` — no `OV_TARGET_BASE` override is needed. The former `viking://resources/personal/` target was retired at the IDEA-034 merge (see `reference/compendium-index.md`, linked from `CLAUDE.md` § OpenViking knowledge base).

## Maintenance best practices

### CLI commands

| Task                           | Command                                                | When                                                                                                    |
| ------------------------------ | ------------------------------------------------------ | ------------------------------------------------------------------------------------------------------- |
| Reindex single resource        | `ov reindex viking://resources/homelab/gpu/thermal.md` | After editing content                                                                                   |
| Force regenerate all abstracts | `ov reindex viking://resources/homelab/ -r`            | After reorganizing directories                                                                          |
| Wait for completion            | `ov reindex <uri> --wait`                              | After bulk changes, before searching                                                                    |
| Remove stale content           | `ov rm viking://resources/volcengine/`                 | When content is no longer relevant                                                                      |
| Move/rename                    | `ov mv viking://old/path viking://new/path`            | ⚠️ **DESTRUCTIVE on v0.4.20 — see the warning below.** Restructuring without losing abstracts (v0.4.10) |
| Check L0 abstracts             | `ov abstract viking://resources/homelab/gpu/`          | Verify abstracts are populated (not `[.abstract.md is not ready]`)                                      |
| Check L1 overviews             | `ov overview viking://resources/homelab/gpu/`          | Verify overviews exist                                                                                  |
| Full tree audit                | `ov tree viking://resources/ -L 3`                     | Monthly or after major restructuring                                                                    |

> ⚠️ **`ov cp` and `ov mv` delete data on v0.4.20. Do not use them after the upgrade.**
>
> Measured on the v0.4.20 rehearsal candidate against a restored production
> corpus (2026-09-15): `ov mv` onto an existing target returns `500 INTERNAL`,
> **deletes the pre-existing target**, and leaves the source in place. The
> upgrade plan predicted "overwrite, not failure" — the real behaviour is worse
> than that, and worse than the v0.4.10 behaviour this table was written for.
>
> Cause: `copy_uri_mapping` raises `Failed to fetch complete vector records`
> when the source is still half-indexed. That is easy to hit, because
> `ov write --wait` returns a client-side timeout and exit 1 **while the write
> actually lands** — so a script that moves a file it just wrote, and that
> trusts exit status, hits exactly this window.
>
> Two consequences worth remembering beyond `mv` itself:
>
> - **`ov` exit status is not a reliable write signal under `--wait`.** Verify
>   the read-back instead. (`_scripts/compendium-sync.py` already does this —
>   it excludes `--wait` from retry and verifies after.)
> - Nothing in this estate calls `cp`/`mv` from code; this table row is the only
>   place it is recommended, which is why the warning lives here.
>
> Production has run v0.4.20 since the 2026-09-15 cutover, so this warning is
> in effect now (compendium BUG-1141).

| List directory | `ov ls viking://resources/homelab/` | Quick check of contents |
| Read file content | `ov read viking://resources/homelab/gpu/assessment.md` | View L2 full content |
| Search content | `ov grep "pattern" viking://resources/homelab/` | Pattern search within resources |

### API reindex (programmatic)

```http
POST /api/v1/maintenance/reindex
{"uri": "viking://resources/homelab/", "regenerate": true, "wait": true}
```

### Health checks

- **Missing abstracts**: If `ov ls` shows `[.abstract.md is not ready]`, the VLM hasn't processed that content yet. Reindex with `ov reindex <uri> --wait`.
- **Stale content**: Periodically audit with `ov tree viking://resources/ -L 3`. Remove content that's been superseded by code changes.
- **Duplicate detection**: Search before adding. Use `viking_find(query, scope=...)` to check for existing coverage.

### Cleanup cadence

| Frequency           | Task                                                                        |
| ------------------- | --------------------------------------------------------------------------- |
| Per session         | Search before adding; save findings as they occur                           |
| Weekly              | Audit tree for stale or duplicate content                                   |
| Monthly             | Full `ov reindex -r --wait` on project root after significant restructuring |
| After major changes | Reindex affected directories                                                |

## Session management

### Lifecycle

Sessions follow: Create → Interact → Commit.

- **Create**: `session = client.session(session_id="...")`
- **Interact**: Add messages, record used contexts/skills
- **Commit**: Archives conversation + triggers background memory extraction

### Memory extraction

After `session.commit()`, OV runs async memory extraction:

1. Session messages are archived
2. VLM analyzes conversation for memorable insights
3. Extracted memories are stored in `viking://user/noot/memories/` (observed 2026-09-21: `events/<yyyy>/<mm>/<dd>/` and `preferences/user/`). No agent-scoped destination has been demonstrated by a live write
4. Poll task status until `completed`

### `viking_add_text` vs `viking_session_commit`

| Method                  | Use when                                                 | How                    |
| ----------------------- | -------------------------------------------------------- | ---------------------- |
| `viking_add_text`       | Single finding at moment of discovery                    | Direct, immediate      |
| `viking_session_commit` | End of multi-hour sessions with many interwoven findings | Batch extraction by OV |

**Never use `viking_session_commit` as a substitute for saving important findings as they occur.** It's a complement, not a replacement.

## Viking URI reference

### URI format

```text
viking://{scope}/{path}
```

### Scopes

| Scope       | Description           | Lifecycle        | Visibility      |
| ----------- | --------------------- | ---------------- | --------------- |
| `resources` | Independent resources | Long-term        | Global          |
| `user`      | User-level data       | Long-term        | Global          |
| `agent`     | Agent-level data      | Long-term        | Global          |
| `session`   | Session-level data    | Session lifetime | Current session |
| `temp`      | Temporary files       | During parsing   | Internal        |
| `queue`     | Processing queue      | Temporary        | Internal        |

Only `resources`, `user`, `agent`, and `session` are addressable through the public API.

## Shared LLM infrastructure

- VLM: local `gemma4:vlm` (`FROM gemma4:12b-it-qat`, `num_ctx 32768`, `llama/ollama/gemma4-vlm.Modelfile`) on timmy's host Ollama, reached through `chat-ollama.llama.svc:11434/v1` — the shim that injects `reasoning_effort: none`; a raw Ollama endpoint returns empty abstracts. `vlm.max_concurrent 1`, `vlm.timeout 600`, no backup credential. Live since homelab #131 (2026-09-18), replacing `gemma4:31b-cloud` (cloud, 2026-07-25 → 09-18) which replaced the llama.cpp `llamacpp-cuda-ov` VLM (`llamacpp-vlm.viking.svc` now has zero endpoints, BUG-1145; the 1080 serves the embedder)
- Embedder: Qwen3-Embedding-4B Q8_0 on `embedder-qwen.viking.svc:8080` → `embedder-qwen-cuda` on manu's NVIDIA GTX 1080 (CUDA) since the 2026-07-17 IMPR-1077 cutover; 2560-dim, last-token pooling, `--parallel 1 --ctx-size 8192 --batch-size 2048 --ubatch-size 2048` (2048 since homelab #125, 2026-09-17), `embedding.max_concurrent 1`. Replaced nomic-embed-text-v1.5 in the IDEA-1042 cutover (2026-06-22); ran on wemby CUDA 2026-06-29→07-06 and on timmy's RX 9070 XT (ROCm) 07-06→07-17. `embedder-qwen-rocm` (timmy, replicas=0) is the rollback path; `embedder-llamacpp` (nomic 768-dim) was deleted from the cluster on 2026-07-04 (manifest kept in repo for history only). `reranker-bge` was retired on 2026-09-18 (#128).
- Manual reindex: `POST /api/v1/content/reindex {"uri": "<dir>", "regenerate": true, "wait": true}`
