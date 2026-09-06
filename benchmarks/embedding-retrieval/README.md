# Embedding / retrieval benchmark (TASK-1122)

Four-lever attribution benchmark for OpenViking / omnipendium retrieval. Validates
the Qwen3-Embedding-4B choice for OV, picks omnipendium's embedder, and closes
BUG-1016 with lever-level evidence.

> **📄 The consolidated plan, methodology & results live in [`benchmark.md`](benchmark.md).**
> This README is a quick status/usage index. Compendium implementation plan (phased,
> task-tracking): `~/code/compendium/docs/plans/2026-07-04-TASK-1122-embedding-model-benchmark.md`.

## Phase 0 status (setup — 2026-07-04)

| Item                                     | Artifact                            | Status                                                                          |
| ---------------------------------------- | ----------------------------------- | ------------------------------------------------------------------------------- |
| Ground-truth refresh (re-curated v2 set) | `eval_groundtruth_2026-07-04.json`  | ✅ 38 questions (34 positive, 4 negative); 5 June questions dropped as orphaned |
| Ground-truth validator                   | `validate_groundtruth.py`           | ✅ checks every `vault_path` resolves                                           |
| Prefix-behavior determination            | `prefix-behavior.md`                | ✅ config-level answer (prod runs unprefixed); live probe still TODO            |
| Scratch pgvector                         | `docker-compose.yml` + `schema.sql` | ✅ scaffold ready (not yet brought up)                                          |
| Harness (embed → load → score)           | `benchmark_embedders.py`            | ⬜ Phase 1                                                                      |

### Ground-truth re-curation (why v2, not the June set)

The June frozen set (`compendium/docs/kb-benchmark.py`) referenced pre-merge
personal-vault IDs that drifted through the `+1000` IDEA-034 merge and later
renumbering — neither `+1000` nor identity recovers them. This set is a
**content-based re-curation** against the live 2026-07-04 vault. Dropped as
orphaned (no current entry): Gmail label-color (U3), OV embedder crash-on-startup
(D2), jump-pod worktrees (D5), Gmail-project tasks (X4), audit-log pattern (C3).
Rows carry a `confidence` field — `low`/`medium` rows are the adjudicated drifts;
review before treating any single row as gospel. June's 4.7% / 10.3% are
directional only (different embedder + old tree), so re-freezing costs no
comparability.

## Usage

```bash
# validate the ground truth resolves against the live vault (Phase 0 gate)
python3 validate_groundtruth.py

# bring up the scratch pgvector (Phase 1)
docker compose up -d
docker compose exec -T db psql -U bench -d embedbench < schema.sql
# ... run benchmark_embedders.py per candidate (Phase 1) ...
docker compose down -v   # disposable — drops the volume
```

## Candidate set (v1)

nomic-embed-text-v1.5 (baseline, Ollama) · Qwen3-Embedding-4B (current OV,
llama.cpp `--pooling last`) · Qwen3-Embedding-0.6B (efficiency, llama.cpp) ·
bge-m3 (alt family — dense + offline sparse/hybrid via FlagEmbedding). Dropped
from v1: embeddinggemma (2k ctx), Qwen3-8B (serving-disqualified). See the
candidate research doc: `compendium/docs/research/2026-07-04-TASK-1122-embedding-model-candidate-research.md`.
