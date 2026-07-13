# Pop small-MoE benchmark matrix (2026-07-13)

Throughput/latency matrix of four sub-35B MoE/hybrid/dense models plus the current
daily-driver baseline, all run through the standardized `concurrency-bench.py`
agentic workload at 32K context under one environment on pop (M5 Max, 64 GB).
Motivated by PROJ-1003 / the "is anything leaner than `qwen3.6:35b-mlx`?" question.

- **Harness**: `benchmarks/ollama/tools/concurrency-bench.py` (agentic workload, `num_ctx=32768`, `num_predict=2048`, `C=1..3`, 3 requests/level, 3 repeats)
- **Runner**: `benchmarks/ollama/tools/run-pop-moe-matrix.sh` — `OLLAMA_NUM_PARALLEL=3`, one `ollama stop` + 30s cooldown between models
- **Env**: ollama 0.31.1 (brew), `OLLAMA_FLASH_ATTENTION=1`, `OLLAMA_KV_CACHE_TYPE=q8_0`, `OLLAMA_MAX_LOADED_MODELS=1`; MLX code-completion LaunchAgents spun down for the run
- **Sampling**: each model ran its **own recommended settings** (model-card first, shipped Modelfile second, harness default last) — see `pop-moe-recommended-params.txt`. Cross-model throughput comparisons carry that caveat.

## Results

Single-stream decode is p50 `gen_tps` at C=1; aggregate is `aggregate_tok_s` (sum
across concurrent streams). Prompts are short (~170-200 tokens), so every run is
**decode-dominated** — TTFT is small and GPU compute utilization sits ~70% (memory-
bandwidth-bound MoE decode), not a misconfiguration.

| Model | Arch | Backend | On-disk | Decode t/s (C=1, p50) | Agg t/s (C=3) | TTFT s (C=1 p50) | ITL s (C=3 p50) | Load (warmup TTFT) |
|---|---|---|---|---|---|---|---|---|
| `nemotron3:33b` | Hybrid Mamba MoE (~3B act) | Ollama GGUF Q4_K_M | 27 GB | **79.1** | 78.9 | 0.483 | 0.0124 | 18.2s |
| `hermes3:8b` | Dense 8B | Ollama GGUF Q4_0 | 4.7 GB | 77.6 | **99.6** | 0.412 | 0.0181 | 4.6s |
| `north-mini-code-1.0` | MoE Cohere (~3B act) | Ollama MLX nvfp4 | 19 GB | 73.2 | 77.2 | **0.055** | 0.0130 | 4.5s |
| `qwen3-coder:30b` | MoE coding (~3.3B act) | Ollama GGUF Q4_K_M | 18 GB | 67.3 | 84.8 | 0.349 | 0.0206 | 8.6s |
| `qwen3.6:35b-mlx` (baseline) | MoE (~3B act) | Ollama MLX nvfp4 | 21 GB | 41.4 | 41.5 | 0.116 | 0.0240 | 4.5s |

Speedup vs the `qwen3.6:35b-mlx` baseline (identical agentic-32K methodology):

| Model | Decode C=1 | Aggregate C=3 |
|---|---|---|
| `nemotron3:33b` | 1.91× | 1.90× |
| `hermes3:8b` | 1.87× | **2.40×** |
| `north-mini-code-1.0` | 1.77× | 1.86× |
| `qwen3-coder:30b` | 1.63× | 2.04× |

## Findings

- **Every leaner model beats the baseline** on both single-stream decode (1.6–1.9×) and aggregate throughput at C=3 (1.9–2.4×), under identical methodology. The answer to "is anything leaner than `qwen3.6:35b-mlx`?" is an unambiguous **yes** — all four.
- **Winner depends on the axis.** `nemotron3:33b` has the fastest single-stream decode (79 t/s) but the largest footprint (27 GB) and slowest load (18s). `hermes3:8b` (dense, 4.7 GB) has the best aggregate throughput (99.6 t/s at C=3) and smallest footprint, but it is an 8B dense model — different capability class. `north-mini-code-1.0` has by far the best TTFT (0.055s, MLX) and is the strongest MoE coding-oriented option. `qwen3-coder:30b` is the mid-pack coding specialist.
- **MLX does not continuous-batch; llama.cpp does.** The two MLX models (`north-mini`, `qwen3.6`) show aggregate ≈ single-stream (77.2 vs 73.2; 41.5 vs 41.4) — concurrency buys nothing. The GGUF/llama.cpp models scale with concurrency: `hermes3` 71→99.6 (+40%), `qwen3-coder` 63.8→84.8 (+33%). For a machine serving concurrent agentic requests, the GGUF models' batching is a real advantage.
- **Baseline decode discrepancy (flag, not a conclusion).** `qwen3.6:35b-mlx` decoded at 41 t/s here vs the "~88-111" recorded in the July editpred/prefill row of the comparison matrix. This is not a thinking-mode artifact (decode t/s is content-independent) — it is a workload/config difference (agentic 32K, shipped thinking params vs the July editpred ladder). Notably the other MLX MoE (`north-mini`, ~3B active, same 32K) hit 73 t/s, so 41 t/s is model-specific, not a 32K-context penalty. Worth a follow-up before treating either number as the model's canonical decode rate; the July prefill row is preserved unchanged in the matrix.

## Per-model sampling settings (comparability caveat)

Models did **not** run a uniform config — each used its recommended settings. Source
per the priority ladder in `pop-moe-recommended-params.txt`.

| Model | temperature | other options | source |
|---|---|---|---|
| `north-mini-code-1.0` | 1.0 | top_p 0.95 | shipped Modelfile |
| `nemotron3:33b` | 0.3 | (none) | harness default — no card/Modelfile rec |
| `qwen3-coder:30b` | 0.7 | top_p 0.8, top_k 20, repeat_penalty 1.05 | shipped Modelfile |
| `hermes3:8b` | 0.3 | (none) | harness default — Modelfile ships only stop tokens |
| `qwen3.6:35b-mlx` | 1.0 | top_p 0.95, top_k 20, repeat_penalty 1, min_p 0, presence_penalty 1.5 | shipped Modelfile |

## Not benchmarked

- **`laguna-xs-2.1:latest`** — dropped. Upstream-acknowledged macOS/Metal empty-output bug (poolside readme banner; card note that root cause is under investigation with the Ollama team). `/api/chat` and templated `/api/generate` return empty on Metal; the documented `raw:true` workaround yields degenerate repetition. Revisit after the upstream fix. See `pop-moe-recommended-params.txt`.
- No quality/correctness evals, no tool-call validation (workload run without `tool_validate`), no multimodal (nemotron3 text-only), no NP sweep (fixed NP=3), no mlx_lm.server comparison (TASK-1111).

## Artifacts

| File | Contents |
|---|---|
| `../ollama-pop-moe-north-mini-nvfp4-agentic-20260713-133644/` | north-mini results.json + CSV + summary |
| `../ollama-pop-moe-nemotron3-33b-agentic-20260713-134706/` | nemotron3 results |
| `../ollama-pop-moe-qwen3-coder-30b-agentic-20260713-135134/` | qwen3-coder results |
| `../ollama-pop-moe-hermes3-8b-agentic-20260713-135453/` | hermes3 results |
| `../ollama-pop-moe-qwen36-35b-mlx-agentic-20260713-141912/` | qwen3.6 baseline results |
| `../pop-moe-recommended-params.txt` | per-model sampling capture (incl. dropped laguna) |
| `../pop-moe-np3-env.json` | environment capture for the run window |
| `summary.csv` | flattened per-model / per-level table |
