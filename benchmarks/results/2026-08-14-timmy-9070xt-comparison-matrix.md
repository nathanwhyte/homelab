# timmy (RX 9070 XT) model comparison matrix

Standing reference for models served on timmy's RX 9070 XT (16 GB GDDR6, Ollama
0.31.1, llama.cpp Vulkan backend, K3s deployment, `OLLAMA_KV_CACHE_TYPE=q8_0`).
The timmy counterpart to `2026-05-08-model-comparison-matrix.md` (which remains
the cross-host reference); rows here come from the TASK-1186 re-benchmark on the
standardized harness (IMPR-1016) — native Ollama API, in-cluster execution, no
proxy, per-row provenance in each run directory. Numbers are copied verbatim
from their run artifacts; nothing is estimated.

> **Methodology**: `concurrency-bench.py`, C=1..8 sweep, 8 requests × 3 repeats
> per level, NP=8 single-model isolation (deployment patched per run and
> restored). "mixed" = `mixed_mem0` @ 16K ctx / 512 predict; "agentic" =
> the concurrency harness's canned agentic workload @ 32K ctx / 2048 predict —
> NOT the `agentic-coding-bench.py` scored pass, which is separate and pending.
> Every config passed a coherence gate at its benchmarked sampling before its
> throughput job ran.
>
> **Read the usable column before the throughput column.** Aggregate tok/s
> counts every generated token, thinking included. On the mixed workload's
> 512-token cap, a thinking-by-default model (gemma4) spends most of the budget
> reasoning: its mixed rows are valid **decode-rate** measurements but produced
> almost no complete answers. Rows with usable_rate 1.0 are the only ones that
> support end-to-end serving conclusions.

## Throughput (current harness, 2026-08-14 unless noted)

| Model | Quant | Workload | C=1 agg tok/s | C=2 | C=4 | C=8 | P50 TTFT C=1 | Usable (C=1 → C=8) | Scaling shape |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- |
| `gemma4:12b-it-qat` | Q4 QAT | agentic 32K | 58.1 | 65.4 | 92.4 | **126.2** | 11.8s | **24/24 → 24/24, zero truncation** | **Scales 2.2× — the clean scaling result** |
| `gemma4:12b-it-qat` | Q4 QAT | mixed | 57.0 | 73.3 | 125.0 | 186.8 | 9.0s | 4/24 → 1/24 (all `length`; thinking ate the 512 cap) | Scales 3.3× — **decode-rate only, answers unusable** |
| `qwen3.5:9b-q4_K_M` | Q4_K_M | mixed | 79.6 | 82.3 | 82.2 | 82.2 | 0.35s | 11/24 → 12/24 (mostly `length`) | Flat — serializes |
| `qwen3.5:9b-q4_K_M` | Q4_K_M | agentic 32K | 82.8 | 83.6 | 83.5 | 83.5 | 0.41s | 13/24 → 14/24 (P95 TTFT 160s @ C=8) | Flat — serializes |
| `nemotron-3-nano:4b-bf16` | BF16 | mixed (2026-08-11) | 68.8 | 70.8 | 70.7 | 70.7 | 0.32s | 24/24 → 24/24 (9–12 truncated, 0 empty) | Flat — `nemotron_h` serializes on Vulkan too |

## Findings

1. **The clean scaling result is gemma4 agentic: 2.2× at 8 slots with 24/24
   usable, zero-truncation answers at every level.** The mixed 3.3× figure is
   real decode-rate scaling but its answers were 96% empty/truncated at C=8
   (thinking + the 512-token cap) — quote it only as a decode ceiling, never
   as serving capacity. qwen3.5 and nemotron-nano both show the serialization
   signature (flat aggregate, constant ITL, TTFT growing with queue depth);
   gemma4 remains the only model measured that scales under concurrency.
2. **qwen3.5:9b wins solo latency** (79.6 tok/s single-stream, sub-second
   TTFT vs gemma4's ~9–12s) but serializes, and only ~46–63% of its answers
   were usable under these workloads — solo-latency pick, wrong for
   multi-session serving, and check output-budget fit before relying on it.
3. **vs July (INFO-1047), like-for-like**: ~+21% aggregate (47.0 → 56.95
   C=1) or ~+17% per-request generation (51.3 → 60.1). The earlier "+12%"
   compared aggregate against per-request speed — wrong pairing. The delta is
   also **confounded** (different harness, context size, NP posture, Ollama
   version, and ROCm→Vulkan backend since July), so it is a then-vs-now
   observation, not evidence that any single layer "got faster".
4. **Thinking-mode coherence at a real budget is qualified, not clean**
   (`coherence-think-budget-20260814/`, `--think true --num-predict 4096`):
   gemma4 passed 4/4 on first attempts — genuinely clean. qwen3.5 passed 4/4
   only via best-of-3 retries, and its "passing" weekday answer contains a
   **literal `</think>` leak plus a stray token** that the checker's
   substring matching missed — a thinking-separation defect, now caught by a
   dedicated think-tag-leak check in the gate. Conclusion: no *systematic*
   Vulkan thinking garbling; gemma4 clean; qwen3.5 shows think-tag leakage
   and sampling flakiness.
5. **Quality note — qwen3.5:9b arithmetic flakiness**: answered 17×23 wrong
   on 1 of 3 sampled coherence gates at temperature 0.3 (and again at 1.0).
   Sampling-level, not backend; the gate now runs best-of-3 attempts and
   records attempt counts.
6. `nemotron-3-nano:4b-bf16` runs on the 0.31.1 pod (the ≥0.32.9 requirement
   applies to nemotron-3.5-lightning, not nano) — the agentic think/nothink
   pair (pop-mirrored, NP=3) is runnable whenever scheduled.
7. **Workload-design caveat for thinking models**: `mixed_mem0`'s 512-token
   cap cannot benchmark a thinking-by-default model end-to-end. Future mixed
   rows for think-capable models need `think = false` in the config or a
   larger `num_predict`, with the choice recorded.

## Cross-host notes (vs pop, M5 Max)

- No same-model pairing with pop's `qwen3.6:35b-mlx` daily driver exists — the
  official qwen3.6 GGUF artifacts (~17–24 GB) exceed this card's VRAM. The
  planned byte-identical cross-host anchor is `gemma4:12b-it-qat` (library
  GGUF) pulled onto pop; that pop-side ladder run is still pending.
- pop's MLX path does not continuous-batch (aggregate ≈ single-stream); timmy's
  llama.cpp/Vulkan path demonstrably does — for gemma4-class models the dGPU
  wins multi-session workloads even where pop wins single-stream decode.
- nemotron family serializes on **both** backends (pop 0.32.9 MLX and timmy
  Vulkan GGUF): excellent single-stream, unsuitable for fan-out.

## Pending rows and passes (TASK-1186)

| Item | Status |
| --- | --- |
| **8K-ctx throughput ladder** (the task's pass 1) | not yet run — the rows above are 16K mixed / 32K agentic; the contract's `num_ctx=8192` ladder is still owed for every row |
| **Scored agentic pass** (`agentic-coding-bench.py` @ 32K) | not yet run — executes from pop with `--base` (macOS-only sandbox); distinct from the canned agentic workload above |
| Prefill pass (real-payload corpus, cold + warm-prefix) | corpus committed (`benchmarks/ollama/corpus/`), runs from pop over LAN |
| `gemma4:coding-12b` anchor | **excluded** — built `FROM gemma4:12b-mlx`, cannot run on timmy; replaced by the pop-side `gemma4:12b-it-qat` anchor ladder (pending) |
| `gemma4:12b` Q8_0, `gemma4:e2b` FP16 | quant ladder — pull pending (disk-budgeted batches; 37 GB free vs ~75 GB remaining pulls) |
| `ministral-3:14b-instruct-2512-q4_K_M`, `qwen2.5-coder:14b-instruct-q4_K_M` | pull pending |
| `nemotron-3-nano:4b-q8_0` | pulled; configs live (`cluster-vulkan-nemotron-nano-q8-*`) |
| nemotron bf16 agentic think/nothink pair | configs live, NP=3, run with `BENCH_NUM_PARALLEL=3` |
| Single-slot tier (`gpt-oss:20b`, `devstral:24b`, opt. `devstral-small-2:24b`) | NP=1 / 8K ctx only, labeled `np=1` |

## Sources

Exact artifacts backing each row (tracked in git; the enclosing `pod/` fetch
directories also contain stale co-copied artifacts from the shared results
volume — trust only the timestamped paths below):

| Row | Artifact |
| --- | --- |
| gemma4 mixed | `vulkan-20260814T164006Z/pod/ollama-cluster-vulkan-default-20260814-170111/results.json` |
| gemma4 agentic | `vulkan-20260814T164006Z/pod/ollama-cluster-vulkan-agentic-20260814-175618/results.json` |
| qwen3.5 mixed | `vulkan-20260814T175855Z/pod/ollama-cluster-vulkan-qwen35-9b-default-20260814-182426/results.json` |
| qwen3.5 agentic | `vulkan-20260814T182610Z/pod/ollama-cluster-vulkan-qwen35-9b-agentic-20260814-194422/results.json` |
| nemotron bf16 mixed | `vulkan-20260811T212335Z/pod/ollama-cluster-nemotron-nano-bf16-default-20260811-214623/results.json` |
| env / provenance / coherence per run | `cluster-vulkan-env.json`, `model-provenance-*.txt`, `coherence-*.json`, `backend-proof-*.log` in each run dir |
| Think-budget transcripts | `coherence-think-budget-20260814/{gemma12bqat,qwen35-9b}.json` |
| Vault records | TASK-1186 (methodology, selection), TASK-1013 (nemotron), INFO-1047 (July baseline), INFO-1140 (nemotron_h serialization on pop) |
