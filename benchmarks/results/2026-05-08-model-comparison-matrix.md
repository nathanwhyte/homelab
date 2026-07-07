# Local LLM model comparison matrix

Standing reference for locally-served models across pop (M5 Max, MLX/Ollama) and timmy (RX 9070 XT, Ollama/Vulkan). Compiled from `compendium` benchmark records — see the Sources table at the bottom for the record backing each row. Numbers are copied verbatim from their source record; nothing here is estimated or interpolated unless marked `(est.)`.

> **Gap closed (2026-07-07)**: prefill tok/s and TTFT for `qwen3.6:35b-mlx` are now measured directly (concurrency=1, raw completion, 500/2k/8k-token prompt ladder) — see the Throughput table and `ollama-pop-qwen36-35b-mlx-prefill-breakdown-20260707-181500/`. **Prefill is not slow**: ~1,200-1,700 tok/s, scaling roughly linearly with prompt length, so TTFT is a predictable function of context size (~0.3s at 400 tokens, ~3.4s at 5.6k tokens) rather than a fixed model-level penalty. `TASK-1015`, `TASK-1107`, `TASK-1111` remain open for the broader comparisons they were scoped for (27B quant comparison, 9B-vs-35B daily-driver quality axis, mlx-lm-server-vs-Ollama), but the specific prefill-speed question is answered. Do not infer prefill speed from the GGUF `qwen3.6:35b` row below — different format (Q4_K_M vs MLX) and different hardware (timmy RX 9070 XT vs pop M5 Max).
>
> **Methodology gotcha found while measuring this**: the harness's `edit_prediction_zeta` workload salts prompts by index only, so re-running it against the same model reproduces byte-identical prompts and Ollama's slot cache skips prefill entirely on the repeat (reported prefill jumps to 100,000+ tok/s — a tell, not a real result). `benchmarks/ollama/tools/prefill-size-breakdown.py` fixes this with a random per-invocation salt; use it (not a second `concurrency-bench.py` run with the same config) for any repeat prefill measurement against a model that's already been benchmarked this session.

---

## Throughput

Decode tok/s is single-stream, no batching, from each source record's own methodology (see Sources). Prefill tok/s and TTFT are left blank where no record measures them — do not fill with a guess.

| Model | Format | Type | Active params | Decode tok/s | Prefill tok/s | TTFT | Host |
|---|---|---|---|---|---|---|---|
| `qwen3.6:35b` | Q4_K_M GGUF | MoE | ~3 B | ~122 | — | — | timmy (RX 9070 XT) |
| `qwen3.6:35b-mlx` | MLX | MoE | ~3 B | ~88-111 (varies with prompt length) | ~1,200-1,700 (scales linearly with prompt length, not flat) | 0.34s @400tok / 0.93s @1.6k tok / 3.4-3.5s @5.6k tok (P50, concurrency=1) | pop (M5 Max) |
| `gemma4:26b-mlx` | MLX | MoE | ~3.8 B | ~114 | — | — | pop (M5 Max) |
| `gemma4:e4b-mlx` | MLX | dense | ~8 B | ~116 | — | — | pop (M5 Max) |
| `gemma4:e2b-mlx` | MLX | dense | ~5.1 B | ~183 | — | — | pop (M5 Max) |
| `gemma4:31b-nvfp4` | NVFP4 | dense | ~31 B | ~23 | — | — | pop (M5 Max) |
| `gemma4:12b-mlx` | MLX | dense (MTP) | ~12 B | 104.6 (single-slot) | — | 0.07s (P95, np=1) | pop (M5 Max) |
| `deepseek-coder-v2:16b` | Q4_K_M GGUF | MoE | ~16 B | ~60-80 | — | — | timmy (RX 9070 XT) |
| `codestral:22b` | Q4_K_M GGUF | dense | ~22 B | ~50-70 | — | — | timmy (RX 9070 XT) |
| `starcoder2:15b` | Q4_K_M GGUF | dense | ~15 B | ~70-90 | — | — | timmy (RX 9070 XT) |
| `qwen3.5-128k` | Q4_K_M GGUF | dense | — | ~40-60 | — | — | timmy (RX 9070 XT) |
| `qwen3.5` | Q4_K_M GGUF | dense | — | ~80-100 | — | — | timmy (RX 9070 XT) |
| `mistral-nemo` | Q4_K_M GGUF | dense | ~12 B | ~80-100 | — | — | timmy (RX 9070 XT) |
| `devstral:24b` | Q4_K_M GGUF | dense | ~24 B | ~40-60 | — | — | timmy (RX 9070 XT) |

## Quality (task-level, not synthetic benchmarks)

| Comparison | Result | Record |
|---|---|---|
| `qwen3.6:35b-mlx` (MoE, 5bit) vs `Llama-3.3-70B-Instruct-4bit` (dense) | No case favored the 70B on 15 real omnipendium tasks (taxonomy calls, intake classification, frontmatter fill, cross-link suggestion). 35B won 2 cases outright, tied the rest. 70B repeatedly hallucinated implementation nouns (tool names, `Kubernetes`, `Grafana`) as `customers:` entities — the 35B never did. | `IDEA-1054` |

## Reliability

| Model | Issue | Status | Record |
|---|---|---|---|
| `qwen3.6:35b-mlx` | Thinking not hard-disabled via the OpenAI `/v1/chat/completions` surface (used by Pi/Ollama-compat clients) even with `enable_thinking:false` — model emits chain-of-thought instead of visible output, client sees empty/truncated response, downstream agents enter write/repair loops on Python/YAML edits. Native `/api/chat` with `think:false` and Anthropic `/v1/messages` with `thinking.type=disabled` work correctly. | Open (partial mitigation: Zed default agent switched to `enable_thinking:false` fast-fix profile) | `BUG-1024` |

## Recommendation matrix (unchanged from throughput table — no new decision made)

| Workload | Default | Why |
|---|---|---|
| Anything you'd use ChatGPT for | `qwen3.6:35b` (MoE, 122 tok/s, timmy) | Fastest quality-per-token at any latency budget |
| Agentic coding (multi-step, reasoning) | `gemma4:e4b-mlx` (dense, 8B, pop) | Better instruction-following, tool-use, JSON |
| Low-latency classification/parsing | `gemma4:e2b-mlx` (dense, 5.1B, 183 tok/s, pop) | Fastest; quality is fine for short outputs |
| Pop daily driver (agentic + compendium mgmt) | `qwen3.6:35b-mlx` (current) | No quality regression found vs a larger dense model (IDEA-1054); prefill measured fast and linear (~1,200-1,700 tok/s, see Throughput table) — not a bottleneck for this workload |

---

## Sources

| Record | What it contributes |
|---|---|
| `compendium/info/knowledge-base/INFO-1004-robots-ollama-model-throughput-reference.md` | GGUF decode tok/s table (timmy), active-param reference, MoE/dense rule of thumb |
| `compendium/ideas/homelab/IDEA-1054-homelab-70b-vs-smaller-model-quality-benchmark-for-omnipendium-tasks.md` | Quality-only benchmark, `qwen3.6:35b-mlx` vs `Llama-3.3-70B-Instruct-4bit`, 15 real tasks |
| `compendium/bugs/homelab/BUG-1024-homelab-qwen3-6-agent-garbled-output-write-loops.md` | Thinking-suppression reliability issue specific to `qwen3.6:35b-mlx` |
| `homelab/benchmarks/results/report-2026-07-01/README.md` | Pop MTP single-slot throughput and P95 TTFT for `gemma4:12b-mlx` (included for hardware/format contrast, not a qwen3.6 measurement) |
| `homelab/benchmarks/results/ollama-pop-qwen36-35b-mlx-prefill-breakdown-20260707-181500/results.json` | Direct prefill/TTFT/decode measurement for `qwen3.6:35b-mlx` at 500/2k/8k-token prompt sizes, concurrency=1, raw completion (`benchmarks/ollama/tools/prefill-size-breakdown.py`) |
| `homelab/benchmarks/results/ollama-pop-qwen36-35b-mlx-editpred-20260707-180725/` | Pooled TTFT/decode aggregate across the same prompt ladder (mixed sizes, `concurrency-bench.py`) — coarser than the size-bucketed breakdown above |
| `compendium/tasks/PROJ-1003/TASK-1015-homelab-qwen3-6-27b-unsloth-mlx-vs-ollama-mlx-benchmark.md` | Scoped but unexecuted — would measure decode/prefill/TTFT for `qwen3.6:27b-mlx` (sibling, not the 35B) |
| `compendium/tasks/PROJ-1003/TASK-1107-homelab-qwen3-5-9b-mlx-vs-qwen3-6-35b-mlx-daily-driver-benchmark.md` | Scoped but unexecuted — would measure agentic + compendium-mgmt quality/latency for `qwen3.6:35b-mlx` vs `qwen3.5:9b-mlx` |
| `compendium/tasks/PROJ-1003/TASK-1111-homelab-benchmark-mlx-lm-server-vs-ollama-mlx-on-pop-m5-max.md` | Scoped but unexecuted — would measure tok/s, TTFT, memory for MLX-native serving vs Ollama MLX on pop |

## Next steps

1. `TASK-1015`, `TASK-1107`, `TASK-1111` remain open for their broader scope (27B quant comparison, 9B-vs-35B daily-driver quality axis, mlx-lm-server-vs-Ollama) — see `compendium/tasks/PROJ-1003/`. The narrow prefill/TTFT question they were each partially scoped to answer for `qwen3.6:35b-mlx` is now covered by the measurement above.
2. Backfill GGUF entries per `TASK-1014` (LFM2.5-8B-A1B, Qwen3-Coder-30B-A3B, nemotron3-nano) once those benchmarks run.
3. Consider running `prefill-size-breakdown.py` against `qwen3.5:9b-mlx` for a like-for-like prefill comparison feeding `TASK-1107`'s daily-driver decision.
