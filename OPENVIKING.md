# OpenViking Organization Guide

This project uses [OpenViking](https://github.com/volcengine/OpenViking) (v0.2.9) as a persistent context database for AI agents. OpenViking organizes all context as a virtual filesystem under `viking://` URIs.

## How retrieval works (why structure matters)

OpenViking's retrieval engine is **directory-recursive**: it scores directories by their L0 abstracts, enters the highest-scoring ones, and drills down. Understanding this is critical to organizing content effectively.

### L0/L1/L2 tiered loading

When you add a file, the semantic processor (Qwen3-8B) auto-generates metadata at each level:

| Layer | File | Size | Purpose |
|-------|------|------|---------|
| **L0** | `.abstract.md` | ~100 tokens | Vector search, quick directory scoring |
| **L1** | `.overview.md` | ~2k tokens | Rerank, content navigation, structure summary |
| **L2** | Original file | Unlimited | Full content, loaded on demand |

Every **directory** also gets its own L0/L1 — a composite abstract of its children. This means directory grouping directly affects retrieval quality.

### Directory recursive retrieval algorithm

```
Query → Intent Analysis → Hierarchical Retrieval → Rerank → Results
```

1. **Intent analysis**: LLM generates 0-5 typed queries from the user's request
2. **Initial positioning**: Vector search scores L0 abstracts to find high-score directories
3. **Refined exploration**: Secondary retrieval within that directory
4. **Recursive drill-down**: If subdirectories exist and score above threshold, recurse
5. **Convergence**: Stops when top-K results are stable for 3 rounds

### Score propagation

A child's final score = `0.5 * embedding_score + 0.5 * parent_score`. Files in well-described directories inherit a relevance boost. Poorly-named or overly-broad directories dilute this signal.

## Directory structure rules

### Mirror the repo layout

Each project gets its own top-level directory under `viking://resources/projects/`:

```
viking://resources/projects/
  ├── homelab/                ← personal homelab K8s cluster
  │   ├── llama/              ← matches repo directory names
  │   ├── viking/
  │   ├── gpu/
  │   └── ...
  ├── <work-project>/         ← each work project is separate
  │   ├── <service-dir>/
  │   └── ...
  └── ...
```

Within each project, mirror the repo's directory structure:

```
viking://resources/projects/<project-name>/
  ├── <service-dir>/          ← matches repo directory names
  │   ├── <filename>.md       ← actual repo files, indexed with their real names
  │   └── ...
  ├── <research-topic>/       ← standalone research/assessments
  └── <project-name>-claude   ← project CLAUDE.md (auto-indexed)
```

**Work projects must be separated from personal projects.** Never mix work codebases into `viking://resources/projects/homelab/`.

### Naming rules

- **Use real filenames** when indexing repo files, not UUIDs or `upload_*.md`
- **Match repo directory structure** — `llama/rocm-llamacpp-deployment.yaml` → `viking://resources/projects/homelab/llama/rocm-llamacpp-deployment`
- **Use descriptive names** for research/notes: `dual-gtx-1080-sli-assessment`, not `gpu-research-1`

Why: UUID filenames produce useless L0 abstracts. The retriever can't score them during directory scanning, so they only get found by accident via full-text search.

### Directory depth: 3-4 levels max below project root

The recursive retriever converges after ~3 rounds of stable top-K. Deep nesting (>4 levels below your project root) slows convergence without improving precision. Flat layouts lose the "lock directory → refine" benefit.

Good: `projects/homelab/llama/benchmarks/`
Avoid: `projects/homelab/llama/benchmarks/qwen8b/optimized/round2/`

### Group related content under topic directories

Instead of many flat sibling directories, group by topic so the parent directory gets a strong, focused L0 abstract.

**Before** (7 flat benchmark dirs dilute `llama/` abstract):
```
llama/bench-agentic/
llama/bench-haiku-direct/
llama/bench-qwen8b/
llama/showdown-report/
llama/showdown-report-comparison/
```

**After** (retriever can quickly enter or skip the whole subtree):
```
llama/benchmarks/
  ├── agentic/
  ├── haiku-direct/
  ├── qwen8b/
  └── showdown-reports/
```

Why: The parent directory `llama/benchmarks/` gets an abstract like "LLM performance benchmarks across models and configurations" — the retriever can decide in one step whether to drill in or skip the entire subtree, instead of evaluating 7 siblings individually.

### Consolidate duplicate topics

If two directories cover the same subject (e.g., `HARDWARE/` and `cluster-hardware-detailed-inventory/`), merge into one. Duplicate directories split retrieval scores — the retriever finds half the content in each and neither scores high enough to surface reliably.

## Cleanup cadence

- Before re-indexing a project, remove stale `index/` subdirectories and flat summary uploads from prior passes
- Remove directories that duplicate content now covered by direct file indexing (e.g., `homelab-deploy-scripts` is redundant when deploy scripts are indexed per-service)
- Keep standalone research/assessment directories — they contain unique analysis not in the codebase
- Rename any `upload_*.md` files to descriptive names

## What to index

- New infrastructure issues and their fixes
- Architecture decisions or changes to resource allocation
- New services, deploy scripts, or configurations
- Non-obvious debugging steps that would save time next session

## What NOT to index

- Routine command output or ephemeral state
- Information already in the codebase (it will be indexed directly from the repo)
- Duplicate content — use `viking_search` first before adding
- Flat summaries of things that are better indexed as the original files
- Vendored content (e.g., `garage/garage/doc/`) — index only homelab-authored files

## Shared LLM infrastructure

- OpenViking's VLM runs on the shared Qwen3-8B endpoint via `qwen-summarizer-llm.llama.svc`
- Embedder: **nomic-embed-text-v1.5** (768-dim, 2048 tokens/slot) on timmy + manu via `embedder-llamacpp.viking.svc:8080`. Uses YaRN RoPE scaling (`--rope-scaling yarn --rope-freq-scale 0.75`) for extended context.
- The agentic endpoint at `summarizer-api.llama.svc/v1/agent` provides tool-calling loops backed by OpenViking
- When indexing large batches, the VLM semantic processing queue runs concurrently (max_concurrent: 6 slots) — monitor with `kubectl logs -n llama deploy/llamacpp-rocm`
- **Bulk re-index**: Use `POST /api/v1/content/reindex` with `{"uri": "<dir-uri>", "regenerate": true, "wait": true}`. Must pass directory URIs, not file URIs. Run sequentially to avoid lock contention.
