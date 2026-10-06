# TASK-1218 evidence (2026-09-22/23): provenance

These files are TASK-1218's 2026-09-23 embedder evidence: the GTX 1080 and GTX 1060 runs and the pop whole-entry quality comparison. They were written in the uncommitted `task1218-quality` worktree (branch `scan/task1218-quality`, head `3f9942c07`). The 2026-10-04 worktree cleanup removed that worktree after archiving its untracked files to `~/.hermes/archive/worktree-cleanup-20261004-105520/task1218-quality/` on pop. They were restored from that archive on 2026-10-06 **byte for byte**. `diff -r` against the archive is empty, and the repository's pre-commit hooks changed nothing.

## What is here

| File | What it is |
| --- | --- |
| `task1218-local-quality-20260923.md` | Report: pop whole-entry quality, Qwen3-Embedding-4B Q8_0 vs 0.6B F16, unprefixed |
| `task1218-1080-slots-20260923.md`, `task1218-1080-slots-plan.md` | Report and plan: GTX 1080 component latency, slots, throughput |
| `results-local/task1218-20260923-qwen4b.json`, `-qwen06.json` | Compact quality results (per-query rankings, scores, input and artifact hashes) |
| `results-local/task1218-20260923-gtx1080.json` | Compact 1080 results for the three arms (`4b-s1`, `06b-s1`, `06b-s4`) |
| `results-local/task1218-20260923-gtx1060-partial.json`, `-gtx1060-retry95.json` | Compact 1060 probe results (both stopped by the thermal guard) |
| `benchmark_local_quality.py`, `benchmark_1080_slots.py`, `probe_cuda_embeddings.py`, `summarize_1080_slots.py`, `restore_1080_experiment.py`, `test_benchmark_1080_slots.py`, `results-local/task1218-1080-initial-runner.py` | The runners |
| `cluster/task1218-1080-model-seed.yaml`, `cluster/task1218-qwen4b-probe.yaml` | The one-off cluster manifests the runs used. Not part of any kustomization. |

## Checked against TASK-1218 before committing

| Check | Value in these files | Matches |
| --- | --- | --- |
| Quality corpus | SHA-256 `2c5256873ecd70175a0c0b7bf0a6e74d43d3b149f46c4af9979b6273d619e732`, 1,280 entries | `corpus.jsonl` on pop today |
| Ground truth | `33a171c79bac1d996ee17ffa8f271bd4fccc50c9df4be420d12d08131dc4b3dd` | `eval_groundtruth_2026-07-04.json` on `main` |
| pop quality | 4B 18/34 top-1, 24/34 top-5; 0.6B 16/34, 23/34 | TASK-1218 note, 2026-09-23 |
| 1080 | 4B ~698–701 tok/s; 0.6B 2,010 at one slot and 1,983 at four; VRAM 3,983 → 6,671 MiB; CUDA 4B 18/34 and 25/34; 0.6B stopped at 894/1,280 | TASK-1218 notes |
| 1060 | partial: 20 requests, medians 603 / 2,035 ms, minimum cosine 0.9997415. retry95: 23 requests, 607 / 2,023 / 7,083 ms, minimum cosine 0.9996807. | TASK-1218 notes |
| 44-text sample / historical vectors | `ed0654415fc5…` / `830e377601b1…` | `~/ov-pilot-20260921/06-trial/embedder-exp/sample.json` and `vectors-1slot-cram0-final.json` on pop |

## What is missing

`scratch/task1218-20260923/` held the raw vectors, telemetry and server logs: `gtx1080-slots/`, `gtx1080-four-slot/`, `gtx1060-probe/` and `gtx1060-retry95/`. It is gitignored (`.gitignore`: `scratch/`), so the cleanup did not archive it, and no copy exists on pop. The compact JSON files still carry the raw files' SHA-256 values (`raw_evidence`, `artifact_hashes`, `raw_vectors_sha256`), so they record exactly what was lost. The figures above can be reproduced only by re-running, not recomputed from raw vectors.

The reports refer to the old worktree path `~/code/homelab/task1218-quality` and to `scratch/`. Those references are kept as written; this file is the correction.
