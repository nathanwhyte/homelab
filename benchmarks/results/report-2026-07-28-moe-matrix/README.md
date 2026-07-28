# Pop small-MoE benchmark matrix (2026-07-28) — IN PROGRESS

Re-run of the PROJ-1003 pop matrix on Ollama 0.32.5, with `laguna-xs-2.1`
restored (its 2026-07-13 macOS/Metal empty-output blocker was fixed upstream by
`#17291` / `#17237`) and a gemma4:12b MLX quant sweep added.

**This report is a stub. Do not treat it as final until the Open Questions
section below is resolved.**

- **Harness**: `benchmarks/ollama/tools/concurrency-bench.py` (agentic workload,
  `num_ctx=32768`, `num_predict=16384`, `C=1..3`, 3 requests/level, 3 repeats)
- **Runner**: `benchmarks/ollama/tools/run-pop-moe-matrix.sh` — `OLLAMA_NUM_PARALLEL=3`,
  one `ollama stop` + 30s cooldown between models
- **Env**: ollama 0.32.5 (brew), `OLLAMA_FLASH_ATTENTION=1`,
  `OLLAMA_KV_CACHE_TYPE=q8_0`, `OLLAMA_MAX_LOADED_MODELS=1`,
  `OLLAMA_CONTEXT_LENGTH=131072`
- **Sampling**: per-model recommended settings (model card first, shipped
  Modelfile second, harness default last)

## Differences from the 2026-07-13 run

Both are deliberate and both break direct comparability with that table:

1. **Runtime**: 0.31.1 then, 0.32.5 now.
2. **Output budget**: `num_predict` 2048 → 16384. A first 0.32.5 pass at 2048
   showed the cap, not the model, was setting the numbers —
   `qwen3.6:35b-mlx` truncated 7-8 of every 9 requests and its
   time-to-first-answer (36.9s) matched `2048 / 55.3 tok/s` (37.0s) almost
   exactly. See the `73ff5d9` commit message.

## Results

<!-- TODO: fill from the 14 result dirs once the matrix completes. -->

## Open Questions

### OQ-1 — Which models actually benefit from concurrency, and why? (BLOCKING)

**Status: unresolved. Must be answered before this report is final.**

The 2026-07-13 report attributed flat concurrency scaling to MLX
("MLX continuous-batching limitation; GGUF batching advantage"). The 07-13
numbers themselves are more nuanced than that summary:

| Model (07-13) | Backend | Decode C=1 | Agg C=3 | Scaling |
|---|---|---|---|---|
| `hermes3:8b` | GGUF Q4_0, dense | 77.6 | 99.6 | **1.28×** |
| `qwen3-coder:30b` | GGUF Q4_K_M, MoE | 67.3 | 84.8 | **1.26×** |
| `north-mini-code-1.0` | MLX nvfp4, MoE | 73.2 | 77.2 | 1.05× |
| `nemotron3:33b` | **GGUF** Q4_K_M, hybrid Mamba MoE | 79.1 | 78.9 | **1.00×** |
| `qwen3.6:35b-mlx` | MLX nvfp4, MoE | 41.4 | 41.5 | 1.00× |

`nemotron3:33b` is GGUF and did **not** scale, so "MLX vs GGUF" does not
explain the 07-13 data on its own. The 2026-07-28 run reproduces this: at
`num_predict=16384`, nemotron3 scales 1.02× and north-mini 1.01×.

Measurements on 2026-07-28 that constrain the answer — the mechanism is
**request serialization**, not KV allocation:

- `server_prefill_s` (server-reported `load_duration + prompt_eval_duration`,
  which is where KV allocation cost would appear) stays flat across
  concurrency: north-mini 0.059 → 0.092 → 0.119s, nemotron3 0.414 → 0.466 →
  0.458s — while **client-observed TTFT jumps to ~21s**.
- KV is eagerly pre-allocated for all `NUM_PARALLEL=3` slots at model load,
  which happens during warmup, before C=1 runs. No new allocation is needed
  at C=2.
- `OLLAMA_NUM_PARALLEL=3` verified in effect on the running server
  (`ps eww`), so this is not a parallelism misconfiguration.
- TTFT at C>1 lands on one request's solo decode time (north-mini
  1512 tok / 87.1 tps = 17.4s vs ttft 21.1s), i.e. a request's first token
  arrives as the previous one finishes.

**Candidate hypotheses**, none yet confirmed:

1. Backend property — Ollama's MLX runner does not batch, and llama.cpp does.
   Contradicted as a complete explanation by nemotron3 (GGUF, flat).
2. Architecture property — hybrid Mamba/SSM layers batch differently from pure
   transformers, so nemotron3 is flat for its own reason and MLX is flat for
   another. Would mean two distinct causes producing one symptom.
3. Something environmental that changed the effective slot count despite
   `NUM_PARALLEL=3` (e.g. memory-pressure-driven slot reduction on a 64 GB
   machine at 32K × 3 slots with q8_0 KV).

**What resolves it.** The 07-28 matrix contains a near-perfect controlled
experiment that the 07-13 matrix did not: **`laguna-xs-2.1` runs as three rows
of the same model on different backends** — `:latest` (GGUF Q4_K_M), `:mxfp8`
(MLX) and `:nvfp4` (MLX). Same architecture, same weights, same prompts, same
budget. If the GGUF row scales and the MLX rows do not, hypothesis 1 holds for
transformers and nemotron3 is explained separately by hypothesis 2. If all
three are flat, the cause is environmental or backend-wide and hypothesis 3
needs testing.

**Caveat on coverage.** `hermes3:8b`, `qwen3-coder:30b` and the plain-GGUF
`gemma4:12b` rows — the three that would otherwise have provided GGUF
transformer comparators — were dropped from this matrix (the first two models
were deleted from the machine; the gemma4:12b GGUF pair was scoped out).
**`laguna-xs-2.1:latest` is therefore the only plain-GGUF transformer row
left, and OQ-1 depends on it.** If that row is inconclusive, answering OQ-1
requires re-pulling one of the dropped models rather than re-reading this data.

### OQ-2 — Is per-request `num_ctx` honored on 0.32.5? (non-blocking)

`dotfiles/ollama/qwen36-claude.Modelfile` records that per-request
`options.num_ctx` was **ignored** on 0.31.1 (verified 2026-07-08), which is why
that tag bakes `num_ctx` into the Modelfile. On 0.32.5, `ollama ps` reports
models resident at **32768** during this run while the server default is
`OLLAMA_CONTEXT_LENGTH=131072` — suggesting the per-request value is now
honored. If confirmed, the Modelfile comment and the workaround it justifies
should be revisited.

### OQ-3 — This matrix measures decode, not prefill (SCOPE BOUNDARY)

**Status: not a question needing new measurement — a limit on what these
results may be used to claim. Must be stated in the conclusions.**

The agentic workload uses ~180-204 token prompts, so essentially all of each
request is decode:

| Row | prompt tok | output tok | prefill share of wall | decode share |
|---|---|---|---|---|
| `north-mini-code-1.0:mlx-nvfp4` | 180 | 1774 | 0.3% | **99.7%** |
| `nemotron3:33b` | 204 | 1676 | 2.1% | **97.9%** |

Prefill and decode load the GPU in opposite ways. Prefill consumes the whole
prompt in one compute-bound matrix-**matrix** pass with high arithmetic
intensity, and saturates the device. Decode emits one token at a time as a
memory-bound matrix-**vector** pass with a serial dependency chain, and at
batch size 1 cannot fill the ALUs. Observed 2026-07-28: the GPU sits ~55%
under this benchmark, against 90%+ when the same model serves Claude Code.

The daily-driver workload is the opposite shape. Claude Code prefills ~77.4k
tokens on turn one (PROJ-1003, recorded in
`dotfiles/ollama/qwen36-claude.Modelfile`). At a plausible 500-1500 tok/s
prefill rate against ~1500 output tokens at ~55 tok/s, that is **65-85%
prefill** — dominated by the phase this matrix barely exercises.

**Consequence.** These results rank models on decode throughput during
agentic generation, which is the right metric for "how fast do tokens come
out". They do **not** rank models for Claude-Code-shaped work, where prefill
dominates. Two rows could tie here and differ materially in daily use. Any
conclusion of the form "model X is the best daily driver on pop" is
unsupported by this data alone, and needs the prefill/TTFT work already
scoped in PROJ-1003 (TASK-1015, TASK-1107, TASK-1111).

The 2026-07-13 report noted the short prompts as a footnote explaining GPU
utilization. Stating it as a scope boundary on the conclusions is the sharper
form, and the one that stops the table being over-read.
