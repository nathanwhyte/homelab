# OpenViking Organization Guide

This project uses [OpenViking](https://github.com/volcengine/OpenViking) (v0.3.14) as a persistent knowledge base for AI agents.

## Core principle: Progressive addition, not bulk indexing

OV is for **knowledge artifacts** — decisions, debugging insights, architecture rationale, and learned patterns. The codebase itself is NOT indexed into OV. Agents read the repo directly.

Knowledge enters OV through two paths:

1. **Automatic**: Session commits extract memories into `user/memories/` and `agent/memories/`
2. **Manual**: Agents add notable findings via `viking_add_text` during normal work

### Before adding anything, ask three questions

1. **Can't be derived from code or git history?** — If `git log`, `git blame`, or reading the file answers it, don't add it.
2. **Would save significant time if rediscovered?** — Non-obvious root causes, gotchas, and configuration traps pass. Routine changes don't.
3. **Remains relevant going forward?** — If a later change superseded it, it's history, not knowledge. Skip it.

All three must be true. If in doubt, don't add — a lean index with high signal beats a comprehensive one with noise.

### What gets added (and when)

| Trigger | Content | Target |
|---------|---------|--------|
| Non-obvious debugging discovery | Root cause + fix + why it was hard to find | `resources/{project}/{service}/` |
| Architecture decision | Decision + rationale + alternatives considered + trade-offs | `resources/{project}/{service}/` |
| New service or major config change | What it does, why it exists, key endpoints, gotchas | `resources/{project}/{service}/` |
| Deployment state after significant changes | Final config, design decisions, mistakes corrected | `resources/{project}/{service}/` |
| Project instruction files | CLAUDE.md, AGENTS.md (indexed on first session only) | `resources/{project}/` |

### What is NEVER added

- **Source code files** — the repo is the source of truth. Use `Read`/`Grep` directly.
- **Full codebase indexes** — bulk imports produce UUID dirs with empty abstracts that cripple search.
- **Command output or ephemeral state** — transient, no long-term value.
- **Duplicate content** — always `viking_search` before adding. Duplicates split retrieval scores.
- **Session logs or summaries** — use claude-mem for operational history. OV is for curated knowledge only.
- **Routine changes** — "updated config value X to Y" belongs in git, not OV. Only add if the *why* is non-obvious.
- **Content from other memory systems** — don't bulk-import from claude-mem or similar. Selectively evaluate individual items against the three questions above.

## How retrieval works

Understanding OV's retrieval mechanism is essential for organizing content effectively.

### Three content layers

| Layer | Name | File | Token Limit | Purpose |
|-------|------|------|-------------|---------|
| **L0** | Abstract | `.abstract.md` | ~100 tokens | Vector search, quick filtering |
| **L1** | Overview | `.overview.md` | ~2k tokens | Rerank, content navigation |
| **L2** | Detail | Original files/subdirs | Unlimited | Full content, on-demand loading |

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

| Feature | `find()` | `search()` |
|---------|----------|------------|
| Session context | Not needed | Required |
| Intent analysis | Not used | LLM analysis |
| Query count | Single query | 0-5 TypedQueries |
| Latency | Low | Higher |
| Best for | Simple lookups, scoped searches | Complex tasks, multi-intent queries |

**Always pass `scope=`** to narrow results. Example: `viking_find("VLM routing", scope="viking://resources/homelab/")`

## Official OV docs

- [Context Types](https://github.com/volcengine/OpenViking/blob/main/docs/en/concepts/02-context-types.md) — Resources, Memory, Skills
- [Context Layers](https://github.com/volcengine/OpenViking/blob/main/docs/en/concepts/03-context-layers.md) — L0/L1/L2 tiered loading
- [Viking URI](https://github.com/volcengine/OpenViking/blob/main/docs/en/concepts/04-viking-uri.md) — Scopes & path conventions
- [Retrieval](https://github.com/volcengine/OpenViking/blob/main/docs/en/concepts/07-retrieval.md) — Directory-recursive search, score propagation
- [Sessions](https://github.com/volcengine/OpenViking/blob/main/docs/en/concepts/08-session.md) — Session lifecycle & memory extraction

## Directory layout

Follow OV's default convention — one directory per project directly under resources:

```
viking://resources/config/                    ← global agent config/instructions
viking://resources/config/{agent-name}/       ← per-agent configs (optional)
viking://resources/{project}/                 ← one per repo
viking://resources/{project}/{service}/       ← mirrors repo directory names
```

### Config directory (`resources/config/`)

Cross-project agent instructions and shared configuration. Agents can `viking_search("agent instructions config")` to discover global conventions.

```
viking://resources/config/
  ├── global-claude-instructions    ← ~/.claude/CLAUDE.md
  ├── openviking-guide              ← viking/OPENVIKING.md (this file)
  └── {agent-name}/                 ← per-agent configs if needed
```

Per-project instruction files stay under their project:
```
viking://resources/homelab/homelab-claude    ← homelab/CLAUDE.md
viking://resources/homelab/homelab-agents    ← homelab/AGENTS.md
```

### Project directories

```
viking://resources/homelab/llama/
viking://resources/homelab/gpu/
viking://resources/dipdash/api/
```

### Directory structure rules

| Rule | Why | Example |
|------|-----|---------|
| **Real filenames only** | UUIDs/upload hashes produce zero L0 abstract signal, making children invisible to retrieval | `gpu-thermal-throttle.md` not `upload_a3f2.md` |
| **Mirror repo structure** | Predictable paths make scoped searches reliable | `llama/` in repo → `homelab/llama/` in OV |
| **3-4 levels max** below project root | Retriever converges after ~3 rounds; deeper paths rarely get explored | `resources/homelab/gpu/thermal-assessment` is fine; `resources/homelab/gpu/nvidia/thermal/2024/` is too deep |
| **Group related content under topic dirs** | Parent directory abstracts amplify all children's scores | `viking://resources/homelab/gpu/` with multiple GPU-related files under it |
| **Descriptive kebab-case names** | Names become part of the embedding; vague names hurt search | `dual-gpu-assessment` not `gpu-research-1` |
| **Use trailing slash for directories** | Distinguishes dirs from files in URI operations | `viking://resources/homelab/gpu/` (dir) vs `viking://resources/homelab/gpu.md` (file) |
| **Search before adding** | Duplicate content splits retrieval scores across copies | Always `viking_find` or `viking_search` first |

### Recommended homelab structure

```
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

| Method | When to use | How |
|--------|-------------|-----|
| `viking_add_text(content, name, target_dir)` | Single knowledge artifact at moment of discovery (default) | In-line during work via MCP tool |
| `ov add-resource <file> --to <uri> --wait` | Importing an existing file (docs, configs) | CLI, one-shot |
| `viking_session_commit` | End of multi-hour sessions with many interwoven findings | Batch extraction by OV |

### Upload workflow (API level)

Under the hood, `viking_add_text` performs two steps:

1. **Temp upload**: `POST /api/v1/resources/temp_upload` — uploads content
2. **Create resource**: `POST /api/v1/resources` — links temp file to a `viking://` URI

The MCP tool handles both steps automatically.

### Critical: avoid concurrent uploads

Always use `--wait` flag with CLI uploads. Without it, concurrent uploads compete for queue locks, causing semantic processing stalls (we hit this exact issue with VLM debugging).

Even with `--wait`, the semantic queue processor runs tasks concurrently based on `embedding.max_concurrent` and `vlm.max_concurrent`. When multiple tasks target the same AGFS subtree (e.g., bulk sync of `bugs/mage/`), RocksDB POINT/SUBTREE locks contend and time out. The fix: keep both concurrency values at `1` in `openviking-standalone-configmap.yaml` — this serializes all semantic processing and eliminates lock contention on local AGFS (2026-05-30).

For MCP-based uploads during a session, add one item at a time and verify it processes before adding the next.

## Maintenance best practices

### CLI commands

| Task | Command | When |
|------|---------|------|
| Reindex single resource | `ov reindex viking://resources/homelab/gpu/thermal.md` | After editing content |
| Force regenerate all abstracts | `ov reindex viking://resources/homelab/ -r` | After reorganizing directories |
| Wait for completion | `ov reindex <uri> --wait` | After bulk changes, before searching |
| Remove stale content | `ov rm viking://resources/volcengine/` | When content is no longer relevant |
| Move/rename | `ov mv viking://old/path viking://new/path` | Restructuring without losing abstracts |
| Check L0 abstracts | `ov abstract viking://resources/homelab/gpu/` | Verify abstracts are populated (not `[.abstract.md is not ready]`) |
| Check L1 overviews | `ov overview viking://resources/homelab/gpu/` | Verify overviews exist |
| Full tree audit | `ov tree viking://resources/ -L 3` | Monthly or after major restructuring |
| List directory | `ov ls viking://resources/homelab/` | Quick check of contents |
| Read file content | `ov read viking://resources/homelab/gpu/assessment.md` | View L2 full content |
| Search content | `ov grep "pattern" viking://resources/homelab/` | Pattern search within resources |

### API reindex (programmatic)

```
POST /api/v1/maintenance/reindex
{"uri": "viking://resources/homelab/", "regenerate": true, "wait": true}
```

### Health checks

- **Missing abstracts**: If `ov ls` shows `[.abstract.md is not ready]`, the VLM hasn't processed that content yet. Reindex with `ov reindex <uri> --wait`.
- **Stale content**: Periodically audit with `ov tree viking://resources/ -L 3`. Remove content that's been superseded by code changes.
- **Duplicate detection**: Search before adding. Use `viking_find(query, scope=...)` to check for existing coverage.

### Cleanup cadence

| Frequency | Task |
|-----------|------|
| Per session | Search before adding; save findings as they occur |
| Weekly | Audit tree for stale or duplicate content |
| Monthly | Full `ov reindex -r --wait` on project root after significant restructuring |
| After major changes | Reindex affected directories |

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
3. Extracted memories are stored in `viking://user/memories/` or `viking://agent/memories/`
4. Poll task status until `completed`

### `viking_add_text` vs `viking_session_commit`

| Method | Use when | How |
|--------|----------|-----|
| `viking_add_text` | Single finding at moment of discovery | Direct, immediate |
| `viking_session_commit` | End of multi-hour sessions with many interwoven findings | Batch extraction by OV |

**Never use `viking_session_commit` as a substitute for saving important findings as they occur.** It's a complement, not a replacement.

## Viking URI reference

### URI format

```
viking://{scope}/{path}
```

### Scopes

| Scope | Description | Lifecycle | Visibility |
|-------|-------------|-----------|------------|
| `resources` | Independent resources | Long-term | Global |
| `user` | User-level data | Long-term | Global |
| `agent` | Agent-level data | Long-term | Global |
| `session` | Session-level data | Session lifetime | Current session |
| `temp` | Temporary files | During parsing | Internal |
| `queue` | Processing queue | Temporary | Internal |

Only `resources`, `user`, `agent`, and `session` are addressable through the public API.

## Shared LLM infrastructure

- VLM: Qwen3-8B on `llamacpp-cuda-llm.viking.svc` (manu, NVIDIA GTX 1080)
- Embedder: nomic-embed-text-v1.5 on `embedder-llamacpp.viking.svc:8080` (timmy, CPU-only)
- Manual reindex: `POST /api/v1/content/reindex {"uri": "<dir>", "regenerate": true, "wait": true}`
