# Model serving/inference parameters (TASK-1122)

Settings each candidate must use so the A/B compares models, not misconfigurations.
Grounded in the model cards + our corpus profile. Companion to `prefix-behavior.md`.

## Corpus profile (measured 2026-07-04, 1097 entries, est. tokens = chars/4)

| p50   | p90   | p95   | p99   | max    | > 2048 tok | > 4096 tok | > 8192 tok           |
| ----- | ----- | ----- | ----- | ------ | ---------- | ---------- | -------------------- |
| ~1375 | ~3400 | ~4101 | ~6545 | ~12050 | 28.8%      | 5.0%       | **0.2% (2 entries)** |

**Implications:**

- **Context window = 8192 is the right shared cap.** It embeds 99.8% of entries whole and matches OV production (`embedding.max_input_tokens: 8192`). Only 2 entries truncate; chunk those two or accept the tail loss. **Qwen's 32k context is irrelevant for this corpus** — a correction to the candidate-research doc, which over-weighted long-context as an advantage here.
- **A 2048 cap would silently truncate ~29% of the corpus.** This is the exact IMPR-1005 "embedding context overflow" failure (old `embedder-llamacpp` ran `--ctx-size 16384 --parallel 8` → 2048/slot; docs >2047 tok failed). Any nomic arm MUST be rope/yarn-scaled to 8192 or ~316 entries are truncated — which would unfairly depress nomic (or faithfully reproduce a prod bug; run both to tell them apart).

## Shared benchmark settings (all candidates)

| Setting          | Value                                                | Why                                                                                                                                                                     |
| ---------------- | ---------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Corpus           | all 1097 compendium entries                          | realistic distractor set (what OV searches)                                                                                                                             |
| Granularity      | one vector per entry                                 | ground truth is entry-level; note OV chunks internally, so the pgvector arm isolates embedder quality at entry granularity rather than reproducing OV's chunk retrieval |
| Context cap      | **8192 tokens**                                      | covers 99.8%; matches OV prod                                                                                                                                           |
| Dimension        | each model's **native full** dim (no MRL truncation) | quality ceiling per model; MRL is a later storage-cost sensitivity run                                                                                                  |
| Normalization    | **L2-normalize every vector**; score = cosine        | pgvector `<=>` cosine; consistent across models                                                                                                                         |
| Query/doc prefix | per-model policy (below + `prefix-behavior.md`)      | asymmetric encoding matters for nomic/Qwen                                                                                                                              |

## Per-model parameters

### Qwen3-Embedding-4B — llama.cpp GGUF, wemby CUDA (current OV embedder)

| Param            | Value                                                                                                                                                                               |
| ---------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| quant            | Q8_0 (prod parity; ~4 GB weights on the 6 GB 1060)                                                                                                                                  |
| flags            | `--embedding --pooling last --ctx-size 8192 --ubatch-size 8192`                                                                                                                     |
| **ubatch rule**  | `--ubatch-size` **must be ≥ longest sequence** (8192) — llama.cpp embeds all tokens of a sequence in one ubatch (non-causal). Too small → truncation.                               |
| **per-slot ctx** | `--ctx-size ÷ --parallel ≥ 8192`. Live deploy uses ctx 32768 / parallel 4 = 8192/slot ✓                                                                                             |
| dimension        | 2560 (native)                                                                                                                                                                       |
| document         | raw text (no instruction)                                                                                                                                                           |
| query            | `Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery: {q}` — **run prefixed AND unprefixed** (prod omits it; card says omitting costs 1–5%) |

### Qwen3-Embedding-0.6B — llama.cpp GGUF

Same as 4B except: **use F16** (weights ~1.2 GB, fits the 1060 with headroom → no quant loss), dimension **1024**, `--pooling last`, same 8192/ubatch/instruction policy.

### nomic-embed-text-v1.5 — Ollama (or llama.cpp), baseline

| Param       | Value                                                                                                                                                                                                      |
| ----------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| precision   | F16 GGUF                                                                                                                                                                                                   |
| pooling     | `mean`                                                                                                                                                                                                     |
| **context** | native 2048 → **must yarn-scale to 8192**: llama.cpp `--rope-scaling yarn --rope-freq-scale 0.75 --ctx-size 8192`; Ollama `num_ctx 8192` (verify the modelfile yarn-scales — else 29% of corpus truncates) |
| dimension   | 768 (native; MRL to 512 costs only 0.3 MTEB if storage matters)                                                                                                                                            |
| prefix      | **REQUIRED**: `search_document: {doc}` / `search_query: {q}`. Run prefixed (correct) + unprefixed (reproduces the current prod/omnipendium mis-prefix, a BUG-1016 suspect)                                 |

### bge-m3 — Ollama (dense) + FlagEmbedding (sparse/hybrid, offline CPU)

| Param         | Value                                                                                                                                                                               |
| ------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| precision     | `use_fp16=True`                                                                                                                                                                     |
| pooling       | CLS                                                                                                                                                                                 |
| context       | `max_length 8192`                                                                                                                                                                   |
| dimension     | 1024 dense                                                                                                                                                                          |
| prefix        | none (dense or sparse)                                                                                                                                                              |
| dense         | `encode(...)['dense_vecs']`, L2-normalize                                                                                                                                           |
| sparse        | `return_sparse=True` → lexical weights → hybrid score `w·dense + (1-w)·sparse`                                                                                                      |
| hybrid weight | start `w=0.5`; card's default across modes is dense 0.4 / sparse 0.2 / colbert 0.4 — **we omit colbert** (multi-vector, expensive), renormalizing dense/sparse ≈ 0.67/0.33; sweep w |

## Gotcha checklist (before any run)

1. **ubatch ≥ ctx** for every llama.cpp arm, or long docs silently truncate.
2. **per-slot ctx = ctx-size / parallel ≥ 8192** — the IMPR-1005 overflow bug.
3. **nomic yarn-scaled to 8192** — default 2048 truncates ~29% of corpus.
4. **Qwen instruction** — bench prefixed + unprefixed; prod runs unprefixed.
5. **L2-normalize everything** before cosine.
6. **Whole-entry vectors** (pgvector arm) ≠ OV chunk retrieval — expected; isolates embedder quality.
