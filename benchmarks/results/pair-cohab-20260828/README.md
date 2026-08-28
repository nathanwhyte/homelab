# Pairing measurements — `qwen2.5-coder:fim` + partner, two runners (2026-08-28)

The three re-runs IDEA-1090 needed before locking a FIM + agent pairing on timmy's RX 9070 XT (Ollama **0.33.1**, Vulkan, production env except `OLLAMA_MAX_LOADED_MODELS=2` for the duration, restored to 1 afterwards; deepseek warm hook re-loaded at the end). Orchestrator: `run.py` (three attempts, see below). One local-model lane throughout.

## 1. Slot probe (`n_seq_max`) on 0.33.1 — `slot-probe.json`

| Model               | Slots granted with `OLLAMA_NUM_PARALLEL=2` |
| ------------------- | ------------------------------------------ |
| `qwen3.5:9b-q4_K_M` | **1** (`n_seq_max = 1`, one `new slot`)    |
| `gemma4:e4b-it-qat` | 2                                          |
| `gemma4:12b-it-qat` | 2                                          |

**Cause (found the same day):** an unconditional architecture blocklist in Ollama's `server/sched.go` — `mllama, qwen3vl, qwen3vlmoe, qwen35, qwen35moe, qwen3next, lfm2, lfm2moe, nemotron_h, nemotron_h_moe, nemotron_h_omni` → `numParallel = 1` before the runner launches, logged as `model architecture does not currently support parallel requests architecture=qwen35` (present in timmy's pod log). It guards a llama.cpp hybrid-attention crash (ggml-org/llama.cpp#20222) that was fixed upstream on 2026-03-08 (#20232); ollama/ollama#17144 removes `qwen35`/`qwen35moe` from the list, is validated on Vulkan/CUDA/ROCm by third parties, and is **unmerged** as of 2026-08-26. No env var or Modelfile knob bypasses it. Same list explains nemotron-3-nano's single slot (INFO-1106/1142) and would cap `lfm2.5`. Irrelevant for a single-lane agent; decisive if two lanes are ever wanted (gemma4 can, qwen3.5 cannot on stock Ollama).

## 2. The 3B FIM side alone on 0.33.1 — `fim-ttft-probe.log`, `fim-smoke.log`

| Measure                                        | `qwen2.5-coder:fim` (3.1B, resident **2.56 GiB @16K**)                                                                                |
| ---------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| TTFT p50 / p95, 1 KiB prefix (8 salted reps)   | **0.074 s / 0.142 s**                                                                                                                 |
| TTFT p50 / p95, 6 KiB prefix                   | **0.271 s / 0.355 s**                                                                                                                 |
| 8-case accept-substring smoke (`fim-smoke.py`) | 7/8 — the one miss is the harness: `sql-where` produced `id IN (1, 2, 3)`, valid SQL my accept list did not include. No marker leaks. |

For reference the deepseek 16B tag measured 0.25 s p50 idle on the same day (2 KiB prefix, `fim-contention-20260828-v0.33.1`).

## 3. Cohabitation — `cohab-<partner>.log/.json`, `vram-pairs.json`

`fim-contention-probe.py` with `FIM_MODEL=qwen2.5-coder:fim`, `BG_MODEL=<partner>`, `BG_NUM_CTX=16384`; the background load runs on the **partner runner**, the FIM probes on the FIM runner. Conditions as in the shared-runner probe: A idle, B one 400-token decode loop, C two, D one ~9k-token-prefill loop, E two.

| Partner (resident @16K)        | Pair VRAM    | A idle      | B 1 decode  | C 2 decode  | D 1 × 9k prefill | E 2 × 9k prefill |
| ------------------------------ | ------------ | ----------- | ----------- | ----------- | ---------------- | ---------------- |
| `qwen3.5:9b-q4_K_M` (5.38 GiB) | **7.9 GiB**  | 0.12 / 0.12 | 0.15 / 0.17 | 0.17 / 0.18 | 0.22 / 0.30      | 0.23 / 0.30      |
| `gemma4:e4b-it-qat` (3.07 GiB) | **5.6 GiB**  | 0.12 / 0.12 | 0.14 / 0.15 | 0.14 / 0.17 | 0.13 / 0.23      | 0.17 / 0.25      |
| `gemma4:12b-it-qat` (7.48 GiB) | **10.0 GiB** | 0.12 / 0.12 | 0.17 / 0.18 | 0.16 / 0.18 | 0.27 / 0.30      | 0.17 / 0.29      |

Cells are FIM TTFT p50 / p95 in seconds, 8 salted probes each. Partner decode while FIM probes ran: qwen3.5 73 tok/s (82 alone), gemma4:e4b 88 (104 alone), gemma4:12b 52 (60 alone) — a 10–15 % partner-side cost.

Compare the same load shapes on the **shared** deepseek runner earlier today: C 2.31 s p50, D 3.26 s p95, E 4.91 s p50. Two runners on discrete VRAM turn slot contention into bandwidth sharing; the worst FIM p95 across all fifteen cells is **0.30 s**, under the 1.62 s IDEA-1071 measured with a chat partner in July and well under Minuet's interactive budget.

Every pair fits with ≥6 GiB headroom — including `gemma4:12b`, which the earlier "single-tenant only" claim had excluded.

## Attempts (why there are three)

| Dir / files                            | What happened                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| -------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `run-attempt1-slotprobes-then-400.log` | Slot probes completed (the table above), then `keep_alive: "-1"` as a _string_ was rejected by `/api/generate` (400). Fixed to an integer.                                                                                                                                                                                                                                                                                                          |
| `attempt2-partner-reloaded-at-131072/` | FIM side + cohabitation ran, but the probe's background requests carried no `num_ctx`, so each partner was **reloaded at the server default 131072** on its first request (5–15 s partner TTFTs, two FIM outliers of 7.8 s and 11.5 s in condition B, and the 128K KV of `gemma4:12b` evicted the FIM runner). The post-reload cells agree with the clean run; kept as evidence of the reload trap (pre-flight S2: a runner is keyed by `num_ctx`). |
| top level (`run.log`, `cohab-*`)       | Clean run with `BG_NUM_CTX=16384`. This is the table above.                                                                                                                                                                                                                                                                                                                                                                                         |

## Observations worth carrying forward

- **Pin `num_ctx` on every request to a tag that has none baked**, or the first request reloads the runner at `OLLAMA_CONTEXT_LENGTH` (131072 here) — a 5–15 s stall and, at 128K KV, an eviction of the co-resident model. The paired tags (IDEA-1090 step 6) bake it for exactly this reason.
- gemma4 on `/api/generate` streams nothing until the response completes (background TTFT ≈ wall in B/C for both gemma4 tags; qwen3.5 streams normally). Consistent with ERR-1004's `/api/generate` behaviour for gemma4 — use `/api/chat` for gemma4 agents.
- The `qwen35` slot cap is a scheduler decision with a merge-pending fix, not a Vulkan or VRAM effect; re-check `sched.go` when the next Ollama ships.

## Verdict

All three candidate pairs pass the cohabitation gate with FIM TTFT ≤ 0.30 s p95 under an agentic prefill-heavy partner. The pairing choice therefore rests on the partner-side gate (fidelity: qwen3.5:9b ≈ gemma4:e4b > gemma4:12b) and on the slot cap (gemma4 tags can take a second lane; qwen3.5 cannot on stock Ollama). Recommendation unchanged from IDEA-1090: `qwen2.5-coder:fim` + `qwen3.5:9b` for a single-lane agent, with `gemma4:e4b` the alternate that keeps a two-lane option open.
