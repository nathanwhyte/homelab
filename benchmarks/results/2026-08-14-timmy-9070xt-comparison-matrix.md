# timmy (RX 9070 XT) model comparison matrix

Standing reference for models served on timmy's RX 9070 XT (16 GB GDDR6, Ollama
0.31.1, llama.cpp Vulkan backend, K3s deployment, `OLLAMA_KV_CACHE_TYPE=q8_0`).
The timmy counterpart to `2026-05-08-model-comparison-matrix.md` (which remains
the cross-host reference); rows here come from the TASK-1186 re-benchmark on the
standardized harness (IMPR-1016) — native Ollama API, in-cluster execution, no
proxy, per-row provenance in each run directory. Numbers are copied verbatim
from their run logs; nothing is estimated.

> **Methodology**: `concurrency-bench.py`, C=1..8 sweep, 8 requests × 3 repeats
> per level, NP=8 single-model isolation (deployment patched per run and
> restored). "mixed" = `mixed_mem0` @ 16K ctx / 512 predict; "agentic" =
> agentic workload @ 32K ctx / 2048 predict. Every config passed a coherence
> gate at its benchmarked sampling before its throughput job ran.

## Throughput (current harness, 2026-08-14 unless noted)

| Model | Quant | Workload | C=1 agg tok/s | C=2 | C=4 | C=8 | P50 TTFT C=1 | Scaling shape |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| `gemma4:12b-it-qat` | Q4 QAT | mixed | 57.0 | 73.3 | 125.0 | **186.8** | 9.0s | **Scales 3.3×** |
| `gemma4:12b-it-qat` | Q4 QAT | agentic 32K | 58.1 | 65.4 | 92.4 | 126.2 | 11.8s | Scales 2.2× |
| `qwen3.5:9b-q4_K_M` | Q4_K_M | mixed | 79.6 | 82.3 | 82.2 | 82.2 | 0.35s | **Flat — serializes** |
| `qwen3.5:9b-q4_K_M` | Q4_K_M | agentic 32K | 82.8 | 83.6 | 83.5 | 83.5 | 0.41s | Flat — serializes (P95 TTFT 160s @ C=8) |
| `nemotron-3-nano:4b-bf16` | BF16 | mixed (2026-08-11) | 68.8 | 70.8 | 70.7 | 70.7 | 0.32s | Flat — `nemotron_h` serializes on Vulkan too |

## Findings

1. **gemma4 is the only model measured so far that scales under concurrency**
   on this card — 3.3× aggregate at 8 slots (mixed), 2.2× (agentic). qwen3.5
   and nemotron-nano both show the serialization signature: flat aggregate,
   constant ITL, TTFT growing linearly with queue depth. Decisive input for
   multi-session serving choices (TASK-1187).
2. **qwen3.5:9b wins solo latency** (79.6 tok/s single-stream vs gemma4's
   57.0, sub-second TTFT) but is the wrong pick for concurrent traffic.
3. **gemma4 solo is up ~12% vs the July figure** (INFO-1047 measured ~51
   tok/s solo at the time) — the serving stack got faster.
4. **No thinking-mode garbling on Vulkan**: the think-budget coherence pass
   (`--think true --num-predict 4096`, `coherence-think-budget-20260814/`)
   passed 8/8 probes on both models. Earlier think-probe failures were
   entirely the 512-token probe budget.
5. **Quality note — qwen3.5:9b arithmetic flakiness**: answered 17×23 wrong on
   1 of 3 sampled coherence gates at temperature 0.3 (and again at 1.0).
   Sampling-level, not backend; the gate now runs best-of-3 attempts.
6. `nemotron-3-nano:4b-bf16` runs on the 0.31.1 pod (the ≥0.32.9 requirement
   applies to nemotron-3.5-lightning, not nano) — the agentic think/nothink
   pair (pop-mirrored, NP=3) is runnable whenever scheduled.

## Cross-host notes (vs pop, M5 Max)

- No same-model pairing with pop's `qwen3.6:35b-mlx` daily driver exists — the
  17–22 GB qwen3.6 artifacts exceed this card's VRAM. The planned byte-identical
  cross-host anchor is `gemma4:12b-it-qat` (library GGUF) pulled onto pop; that
  pop-side ladder run is still pending.
- pop's MLX path does not continuous-batch (aggregate ≈ single-stream); timmy's
  llama.cpp/Vulkan path demonstrably does — for gemma4-class models the dGPU
  wins any multi-session workload even where pop wins single-stream decode.
- nemotron family serializes on **both** backends (pop 0.32.9 MLX and timmy
  Vulkan GGUF): excellent single-stream, unsuitable for fan-out.

## Pending rows (TASK-1186)

| Row | Status |
| --- | --- |
| `gemma4:12b` Q8_0, `gemma4:e2b` FP16 | quant ladder — pull pending (disk-budgeted batches; 37 GB free vs ~75 GB remaining pulls) |
| `ministral-3:14b-instruct-2512-q4_K_M`, `qwen2.5-coder:14b-instruct-q4_K_M` | pull pending |
| `nemotron-3-nano:4b-q8_0` | pulled; configs live (`cluster-vulkan-nemotron-nano-q8-*`) |
| nemotron bf16 agentic think/nothink pair | configs live, NP=3, run with `BENCH_NUM_PARALLEL=3` |
| Single-slot tier (`gpt-oss:20b`, `devstral:24b`, opt. `devstral-small-2:24b`) | NP=1 / 8K ctx only, labeled `np=1` |
| Prefill pass (real-payload corpus, cold + warm-prefix) | corpus committed (`benchmarks/ollama/corpus/`), runs from pop over LAN |
| pop-side `gemma4:12b-it-qat` anchor ladder | pending (cross-host pairing) |

## Sources

| Run | Contents |
| --- | --- |
| `vulkan-20260814T164006Z/` | gemma4:12b-it-qat mixed + agentic (coherence transcripts, backend proof, provenance, env capture) |
| `vulkan-20260814T175855Z/` | qwen3.5:9b mixed |
| `vulkan-20260814T182610Z/` | qwen3.5:9b agentic |
| `vulkan-20260811T212335Z/` | nemotron-3-nano:4b-bf16 mixed (TASK-1013 session) |
| `coherence-think-budget-20260814/` | think=true probes at num_predict=4096, both models |
| Vault: TASK-1186 (methodology, selection), TASK-1013 (nemotron), INFO-1047 (July baseline), INFO-1140 (nemotron_h serialization on pop) | |
