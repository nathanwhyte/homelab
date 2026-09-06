# Embedding / retrieval benchmark — plan, methodology & results (TASK-1122)

Standalone, self-contained record of the OpenViking / omnipendium embedding
benchmark: what we tested, how, on what hardware, and what we found. Companion
references (kept separate for depth): [`model-parameters.md`](model-parameters.md)
(serving params + gotchas), [`prefix-behavior.md`](prefix-behavior.md) (query/doc
prefix policy). Compendium implementation plan (phased, task-tracking):
`~/code/compendium/docs/plans/2026-07-04-TASK-1122-embedding-model-benchmark.md`.

## Objective

Validate — or challenge — the current OV production embedder (Qwen3-Embedding-4B)
with real numbers on our corpus, pick omnipendium's embedder deliberately, and
close BUG-1016 with lever-level evidence. The embedder is one of four retrieval
levers (embedder, reranker, `query_planner`, `find`/retrieval params); this doc
covers the **embedder A/B** (Phase 1–3). The OV-config levers (Phase 4) are
tracked in the compendium plan.

## Methodology

- **Substrate:** scratch pgvector (disposable `embedbench-pg`, one table per
  `(model, dimension)`), driven by [`benchmark_embedders.py`](benchmark_embedders.py).
  Isolates embedder quality at **entry granularity** — one vector per entry, not
  OV's internal chunking. bge-m3 runs offline via FlagEmbedding
  ([`benchmark_bge_m3.py`](benchmark_bge_m3.py)).
- **Corpus:** all compendium entries (`export_corpus.py` → `corpus.jsonl`) — the
  realistic distractor set. Mac arms exported 1097; the later cluster export grew
  to 1099 (2 new entries, 0.2% — immaterial at this resolution). Profile: p50
  ~1375 tok, p95 ~4101, only 2 entries > 8192.
- **Ground truth:** `eval_groundtruth_2026-07-04.json` (v2). The June
  `kb-benchmark.py` set referenced pre-`+1000`-merge IDs that drifted, so it was
  **content-based re-curated** against the live vault: **38 questions (34
  positive + 4 negative)**, 5 June questions dropped as orphaned. Each carries a
  `match_fragment` (lowercased entry id — appears in both OV URIs and pgvector
  ids, so scoring is stack-agnostic) + `vault_path` (validated by
  `validate_groundtruth.py`) + a `confidence` field.
- **Scoring:** rank by pgvector cosine (`<=>`, L2-normalized), **exact search, no
  ANN index** (1097 rows → exact is fast + accurate; also sidesteps pgvector's
  2000-dim hnsw cap, which Qwen-4B's 2560 exceeds). The 6 small-dim arms were
  originally run with an hnsw index (approximate); on 1097 rows the ranking
  difference vs exact is well under one query. `classify_hit`
  → TP/MISS/FP/TN/FN. Headline metric = **top-1 / top-5 at threshold 0.0**
  (pure ranking, cross-model comparable regardless of score scale). Thresholds
  0.50/0.55/0.60 also recorded (negative-control / OV-parity precision).
- **Prefix handling:** each model gets its documented policy AND a production-parity
  unprefixed variant, because prod embeds queries unprefixed today (see
  `prefix-behavior.md`). nomic → `search_*:`; Qwen → query instruction; bge-m3 → none.

## Serving matrix (as run)

| Model                 | Stack           | Hardware                           | Pooling | Ctx        | Dim  | Quant |
| --------------------- | --------------- | ---------------------------------- | ------- | ---------- | ---- | ----- |
| Qwen3-Embedding-4B    | llama.cpp ROCm  | **timmy RX 9070 XT** (bench-only³) | last    | 8192       | 2560 | Q8_0  |
| Qwen3-Embedding-0.6B  | llama.cpp ROCm  | **timmy RX 9070 XT**               | last    | 8192       | 1024 | F16   |
| nomic-embed-text-v1.5 | sentence-transf | Mac CPU                            | mean    | 2048+8192¹ | 768  | F16   |
| bge-m3 (dense+sparse) | FlagEmbedding   | Mac CPU                            | CLS     | 4096²      | 1024 | fp16  |

¹ nomic run via sentence-transformers at **both** 2048 (production-representative
— the prod GGUF clamps to 2048; matches the committed llama.cpp@2048 number
exactly) **and** 8192 (fair-context control), so the ctx effect is isolated from
the serving stack. **317/1099 entries (28.8%) exceed 2048 tokens.** ² bge capped
at 4096 (MPS/CPU attention OOM at 8192) — 55 entries (5.0%) truncated. The
fair-context nomic arm shows the 8192-ctx advantage is _negative_ for mean-pooling
here (Finding 6), so these caps are not a confound against Qwen.

³ The 4B is served on a **temporary isolated ROCm deployment on timmy's 9070 XT**
— the **planned target backend** (2026-07-05 direction, chosen after wemby's GTX
1060 proved fragile). It is _not the current_ production backend: prod today is
`embedder-qwen-cuda` on wemby's GTX 1060 (CUDA llama.cpp, since 2026-06-29). Both
are Q8_0 + `--pooling last`, so quality should be backend-invariant, but quant
parity with the wemby GGUF should be confirmed before finalizing the 4B verdict.
The smaller/offline arms are GPU-invariant on quality.

## Results

Top-1 / top-5 at threshold 0.0 (rank), 34 positive queries. `exact-id` column =
top-1 hits on the 5 ID-lookup queries.

> **Resolution limit:** with 34 positives, one query ≈ **2.9 pp** of top-1.
> Deltas under ~6 pp (two queries) are within noise — e.g. Qwen-0.6B 50.0 vs
> nomic@2048 55.9 is a two-query gap, and Qwen-0.6B-domain +2.9pp is a single
> query. The +14.7 pp Qwen-0.6B prefix effect (5 queries) and the **−20.6 pp
> nomic 2048→8192 drop (7 queries)** are the only large, comfortably-solid
> per-arm deltas. Read the 4B-vs-nomic verdict with the resolution caution.

Qwen arms are the fresh in-cluster 9070-XT run (1099-entry corpus); nomic arms
are same-stack sentence-transformers controls (1099); bge arms are the earlier
Mac run (1097). Corpus differs by 2 entries (0.2%) — immaterial at this
resolution. Qwen@8192 reproduced its Mac/Metal numbers exactly (cross-backend
validation), and nomic-st@2048 (55.9) matches the committed llama.cpp@2048 nomic.

| Arm                        | ctx  | pool | top-1     | top-5     | exact-id | note                                     |
| -------------------------- | ---- | ---- | --------- | --------- | -------- | ---------------------------------------- |
| **Qwen3-4B** (prefixed)    | 8192 | last | **55.9%** | 73.5%     | 0/5      | target backend — **ties best nomic**     |
| Qwen3-4B-domain            | 8192 | last | 55.9%     | 73.5%     | 1/5      | domain prefix = **no gain** over generic |
| Qwen3-4B-unprefixed        | 8192 | last | 50.0%     | 70.6%     | 1/5      | prod parity; generic prefix +5.9pp (~2q) |
| **nomic @2048** (prefixed) | 2048 | mean | **55.9%** | 73.5%     | 0/5      | **production config** — ties 4B          |
| nomic @8192 (fair-ctx)     | 8192 | mean | 35.3%     | 50.0%     | 0/5      | long ctx **dilutes** mean-pool −20.6pp   |
| Qwen3-0.6B (prefixed)      | 8192 | last | 50.0%     | 73.5%     | 0/5      | —                                        |
| Qwen3-0.6B-domain          | 8192 | last | 52.9%     | 73.5%     | 0/5      | domain prefix +2.9pp (1q — noise edge)   |
| Qwen3-0.6B-unprefixed      | 8192 | last | 35.3%     | 67.7%     | 0/5      | generic prefix +14.7pp (5q — solid)      |
| bge-m3 dense               | 4096 | CLS  | 47.1%     | 70.6%     | 0/5      | worst dense arm                          |
| **bge-m3 hybrid** (w=0.5)  | 4096 | CLS  | 44.1%     | **82.3%** | **1/5**  | best recall; sparse cracks exact-id      |

_bge-m3 ColBERT + RRF-fusion arms were **dropped**, not run: multi-vector
late-interaction is far too slow to serve at OV indexing throughput and OV will
never run it in production, so the numbers are non-actionable. The hybrid arm
above already demonstrates the only decision-relevant bge property (sparse
recovers exact-ID). exact-ID structural fix already shipped via omnipendium PR #14._

## Findings

**Headline — at their production configs Qwen3-4B and nomic tie (55.9%); at
_equal_ context Qwen3-4B wins decisively.** The fair-context arm resolves the
prior open question. nomic@2048 (its production, GGUF-clamped config) and
Qwen3-4B@8192 both hit **55.9% top-1** — a tie. But that tie exists _because
nomic benefits from truncation_: pushed to a fair 8192 context, nomic **collapses
to 35.3%** (−20.6 pp, 7 queries — the single largest delta in the study), while
Qwen3-4B at the same 8192 holds 55.9%. So the two are equivalent _only_ at
nomic's favorable short-context setting; on modeling power at equal context,
Qwen3-4B (last-pooling) is clearly stronger and more robust.

**Practical read for OV:** on _quality at production settings_ it's still a tie,
so the decision rightly turns on the non-quality axes — nomic's 768-dim vs 4B's
2560-dim (3.3× storage/compute in `ov-vectordb`), serving footprint, and the
fact that exact-ID needs a lexical layer regardless of embedder. Those favor the
smaller model **for this corpus, whose signal is front-loaded**. The caveat that
would flip it: any move to longer, less front-loaded documents, where Qwen's
context-robustness becomes a real advantage. Remaining decider: the **OV-level
re-measure** (Phase 4) — pgvector-entry ≠ OV-chunked.

1. **exact-ID is a structural wall for dense retrieval** — every pure-dense model
   scores **0/5**. Only bge-m3 hybrid (sparse+dense) moves it (1/5 top-1, 2/5
   top-5). BUG-1016's dominant failure is a retrieval-_method_ problem: the fix is
   a lexical / ID-routing / reranker layer, not a bigger embedding model.
   **Partially shipped already**: omnipendium PR #14 (PRO-242, merged 2026-07-05)
   added exactly this — a two-layer signature-matcher + semantic-tier retrieval —
   on the omnipendium side; the Phase 4 reranker hypothesis should measure OV
   with that layer's existence in mind.
2. **Ground-truth staleness + missing prefixes bound the model's contribution to
   BUG-1016.** The June baseline was 4.7–10.3% top-1; the _same_ nomic on
   refreshed ground truth with correct prefixes hits 55.9%. Caution: three
   variables changed, not two — the June numbers were measured against _live OV_
   (internal chunking, 0.50 threshold, session-artifact contamination) while
   55.9% is entry-granularity pgvector at threshold 0. This bounds what the
   embedder can do; it does **not** show OV would score 55.9% with fixes
   applied. An OV-level re-measure (Phase 4) is required before closing
   BUG-1016.
3. **Prefixing matters (up to +14.7pp on the 0.6B)** and production runs
   unprefixed — a config fix independent of model choice. A **domain-tailored**
   query instruction adds nothing over a generic one (4B: 0.0pp; 0.6B: +2.9pp, a
   single query): the generic Qwen instruction prefix already captures the gain.
4. **bge-m3 hybrid wins recall** (top-5 82.3%, best of all) even where its top-1
   dips — exactly what a reranker would then promote (Phase 4 hypothesis).
5. **Qwen3-0.6B underperforms both the 4B and nomic@2048** (50.0 vs 55.9, a
   two-query gap): the small Qwen is not a free efficiency win; the 4B carries
   Qwen's case.
6. **RESOLVED — the retrieval signal is front-loaded; long context hurts
   mean-pooling.** The fair-context arm (same sentence-transformers stack, same
   1099 corpus, only `max_seq_length` 2048→8192) drops nomic from 55.9% to 35.3%
   top-1. So nomic's production 2048 clamp is not a handicap — for this corpus
   (frontmatter + summary lead every entry) it is effectively **optimal**, and
   mean-pooling the full 8192-token body dilutes the signal toward a corpus
   centroid (scores collapse; similarities compress into a high, threshold-invariant
   band). Qwen3-4B's **last-token pooling is immune** to this — it holds 55.9% at
   8192 — which is the real architectural reason to prefer it if OV ever indexes
   longer, less front-loaded documents.

## Reproduce

```bash
docker compose up -d && docker compose exec -T db psql -U bench -d embedbench < schema.sql
python3 export_corpus.py && python3 validate_groundtruth.py
./serve_local.sh qwen06   # or nomic / qwen4b ; ROCm 4B via scratch/embedder-rocm-bench-v2.yaml + port-forward :8083
uv run benchmark_embedders.py --model <name>
uv run benchmark_bge_m3.py
```

## Infra notes / lessons

- **9070 XT serving:** Ollama can't serve the Qwen embedding GGUF (packaged
  completion-only → "server does not support embeddings"), and serving-consistency
  requires llama.cpp `--pooling last` anyway → temporary ROCm llama.cpp deployment
  on timmy (isolated `bench` namespace, fresh PVC, **no `--mlock`**).
- **llama.cpp embedding gotchas (all hit):** need **both** `--batch-size` and
  `--ubatch-size` ≥ ctx; `--parallel 1` for full per-slot context; shrink-to-fit
  retry for over-context docs.
- **2026-07-05 incident (unrelated to the benchmark's design):** wemby lost power
  mid-run → its pods flooded manu (68) + timmy, Longhorn rebuild + Tailscale relay
  degraded the control plane. No OOM. Lesson recorded: never put benchmark load
  (esp. `--mlock`) on the control-plane node during an incident. Additional
  context from the same evening: OV's semantic-queue backlog (~230 embedding
  items from the v0.4.7 rollout + pointer syncs) was hammering wemby's
  `embedder-qwen` in parallel, so wemby-side latency observations from this
  window are doubly contaminated (power event + OV backlog).
- **Pooling verification (done):** `pooling_check.py` run against the 9070-XT 4B
  endpoint returned the model card's reference similarities exactly — matching
  0.750 (card ~0.75), non-match 0.095 (card ~0.11) → last-token pooling confirmed.
  The nomic (mean) and 0.6B (last) arms use the same llama.cpp `--pooling`
  mechanism with explicit flags; bge-m3 uses FlagEmbedding's native CLS. A silent
  pooling mismatch is the classic way this benchmark class produces garbage, so
  this gate is load-bearing.

## Cluster-native overnight run (as run — partial)

Built and ran as a disposable `bench` namespace: in-cluster pgvector (Deployment +
30Gi Longhorn PVC + Service), two llama.cpp **ROCm embedders on timmy's 9070 XT**
(Qwen3-4B + 0.6B, `--pooling last`, ctx 8192 / parallel 1), and a runner **K8s Job**
whose initContainers clone the compendium and run `export_corpus.py` onto the PVC,
then `run-all.sh` drives every arm error-isolated, results → PVC. Manifests:
`cluster/overnight.yaml` + `cluster/apply.sh`. The corpus/GT are delivered via a
ConfigMap + a PVC seeded by the clone/export initContainers.

**Outcome: 6 of 9 arms completed in-cluster before manu (the runner's node) went
`NodeReady=Unknown` at 05:22Z, ~31 min into the run** — a **power/hardware event
on the NVIDIA agent nodes** (manu _and_ wemby both crashed overnight; the loaded
AMD control-plane node, timmy, never wavered — a pattern that fingers shared power,
not the workload). The Job died at `backoffLimit: 0`. All completed arms persisted
to the PVC (each arm writes its JSON immediately; `run.log` is tee'd), so nothing
was lost — the 6 Qwen arms above are that recovered set.

**The two lost arms (nomic + bge) were finished on the MacBook** against the exact
1099-entry corpus pulled off the PVC — both are pure-CPU/offline and need no cluster
GPU, so cluster instability was irrelevant to them. This is also the better home for
them: nomic-st isolates ctx from stack, and bge is FlagEmbedding-CPU either way.

**Lessons banked:**

- **Don't pin an unattended long job to a flaky node.** The runner sat on manu
  (`nodeSelector: manu`) for CPU headroom; manu was exactly what fell over. A
  cluster Job that must survive the night should tolerate node loss (higher
  `backoffLimit` + idempotent, resumable arms that skip already-written results)
  or run on the proven-stable node.
- **CPU-only arms don't belong in the cluster overnight run at all** — they gain
  nothing from cluster GPUs and add a long, fragile tail (bge ColBERT was budgeted
  4h). Run them on the Mac; reserve the cluster Job for the GPU-served arms.
- The 9070-XT co-residence with Ollama held up fine (both Qwen embedders + Ollama,
  ~5 GB combined) — timmy was the _only_ stable node all night.

## Follow-ups

- ✅ **Done** — Full-8192 nomic (sentence-transformers) fair-context arm: it
  _loses_ 20.6 pp vs 2048 (Finding 6). Settled.
- ⏭️ **Dropped** — bge-m3 ColBERT + RRF: non-actionable (OV won't serve
  multi-vector late-interaction); the hybrid arm already covers the decision.
- Phase 4: reranker + `query_planner` + `score_propagation_alpha` on live OV.
- Tune hybrid fusion (higher sparse weight / dedicated ID-routing) to push exact-id
  past 1/5.
- Confirm 4B quant parity between the ROCm bench arm (Q8_0) and the production
  wemby CUDA deployment's GGUF before finalizing the 4B verdict.
- Add a threshold-0.50 precision row for the leading arms — ties the table back
  to BUG-1016's original symptom (nothing above 0.50) and the OV-parity framing.
