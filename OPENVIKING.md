# OpenViking Organization Guide

This project uses [OpenViking](https://github.com/volcengine/OpenViking) (v0.2.9) as a persistent knowledge base for AI agents.

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

## Official OV docs (how retrieval works)

- [Context Types](https://github.com/volcengine/OpenViking/blob/main/docs/en/concepts/02-context-types.md) — Resources, Memory, Skills
- [Context Layers](https://github.com/volcengine/OpenViking/blob/main/docs/en/concepts/03-context-layers.md) — L0/L1/L2 tiered loading
- [Viking URI](https://github.com/volcengine/OpenViking/blob/main/docs/en/concepts/04-viking-uri.md) — Scopes & path conventions
- [Retrieval](https://github.com/volcengine/OpenViking/blob/main/docs/en/concepts/07-retrieval.md) — Directory-recursive search, score propagation
- [Sessions](https://github.com/volcengine/OpenViking/blob/main/docs/en/concepts/08-session.md) — Session lifecycle & memory extraction

Key mechanic: `final_score = 0.5 * embedding_score + 0.5 * parent_directory_score`. Well-named directories amplify all children. UUID directories produce zero signal.

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
  ├── openviking-guide              ← OPENVIKING.md (this file)
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

### Rules

- **Real filenames only** — never UUIDs or `upload_*.md`
- **Mirror repo structure** — `llama/` in repo → `homelab/llama/` in OV
- **Descriptive names** for research: `dual-gpu-assessment`, not `gpu-research-1`
- **3-4 levels max** below project root — retriever converges after ~3 rounds
- **Group related content** under topic dirs for focused parent abstracts
- **Search before adding** — duplicates split retrieval scores

## Shared LLM infrastructure

- VLM: Qwen3-8B on `llamacpp-rocm-llm.viking.svc` (RX 9070 XT)
- Embedder: nomic-embed-text-v1.5 on `embedder-llamacpp.viking.svc:8080`
- Manual reindex: `POST /api/v1/content/reindex {"uri": "<dir>", "regenerate": true, "wait": true}`
