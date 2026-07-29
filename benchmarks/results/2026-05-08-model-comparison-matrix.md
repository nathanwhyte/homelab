# Local LLM model comparison matrix

Standing reference for locally-served models across pop (M5 Max, MLX/Ollama) and timmy (RX 9070 XT, Ollama/Vulkan). Compiled from `compendium` benchmark records — see the Sources table at the bottom for the record backing each row. Numbers are copied verbatim from their source record; nothing here is estimated or interpolated unless marked `(est.)`.

> **Gap closed (2026-07-07)**: prefill tok/s and TTFT for `qwen3.6:35b-mlx` are now measured directly (concurrency=1, raw completion, 500/2k/8k-token prompt ladder) — see the Throughput table and `ollama-pop-qwen36-35b-mlx-prefill-breakdown-20260707-181500/`. **Prefill is not slow**: ~1,200-1,700 tok/s, scaling roughly linearly with prompt length, so TTFT is a predictable function of context size (~0.3s at 400 tokens, ~3.4s at 5.6k tokens) rather than a fixed model-level penalty. `TASK-1015`, `TASK-1107`, `TASK-1111` remain open for the broader comparisons they were scoped for (27B quant comparison, 9B-vs-35B daily-driver quality axis, mlx-lm-server-vs-Ollama), but the specific prefill-speed question is answered. Do not infer prefill speed from the GGUF `qwen3.6:35b` row below — different format (Q4_K_M vs MLX) and different hardware (timmy RX 9070 XT vs pop M5 Max).
>
> **Methodology gotcha found while measuring this**: the harness's `edit_prediction_zeta` workload salts prompts by index only, so re-running it against the same model reproduces byte-identical prompts and Ollama's slot cache skips prefill entirely on the repeat (reported prefill jumps to 100,000+ tok/s — a tell, not a real result). `benchmarks/ollama/tools/prefill-size-breakdown.py` fixes this with a random per-invocation salt; use it (not a second `concurrency-bench.py` run with the same config) for any repeat prefill measurement against a model that's already been benchmarked this session.
>
> **Agentic coding-tag matrix added (2026-07-29)**: the six `:coding` tags scored on the hardened hidden-verifier harness (post homelab PR #61; 6 tasks × 3 repeats, `num_predict=16384`) — see `agentic-coding-2026-07-29-084913/NOTES.md`. Three-way ceiling at 18/18 (`gemma4:coding-26b`, `gemma4:coding-12b`, `qwen3.6:coding-gguf`); `laguna:coding` 13/18 (all failures whitespace), `nemotron3:coding` 8/18 (32% bad-tool-call rate), `qwen3.6:coding` MLX 7/18 — whitespace corruption strongly associated with the MLX-origin serving/artifact path (`BUG-1067`; engine vs conversion/quantization unresolved — the artifacts differ in digest, arch, params, and quant; thinking not required for the defect). The ceiling is a benchmark-scope limit (T4 tier needed); tool discipline (0%–32% bad calls) is the discriminator below it.
>
> **Small-MoE agentic matrix added (2026-07-13)**: four sub-35B models (`north-mini-code-1.0`, `nemotron3:33b`, `qwen3-coder:30b`, `hermes3:8b`) benchmarked against the `qwen3.6:35b-mlx` daily driver under one env (agentic workload, `num_ctx=32768`, `C=1..3`, NP=3) — see `report-2026-07-13-moe-matrix/`. **All four are faster than the baseline** (1.6-1.9× single-stream decode, 1.9-2.4× aggregate at C=3). Two takeaways: (1) **MLX does not continuous-batch** — the MLX models (`north-mini`, `qwen3.6`) show aggregate ≈ single-stream, while the GGUF/llama.cpp models scale with concurrency (`hermes3` +40%, `qwen3-coder` +33%); (2) the `qwen3.6:35b-mlx` **decode figure differs by workload** — 41 tok/s under this agentic-32K run vs the ~88-111 editpred-ladder number above. This is not a thinking artifact (decode t/s is content-independent) and not a 32K penalty (`north-mini`, same backend/ctx/active-params, hit 73). Both numbers are kept, workload-labelled; the canonical daily-driver decode rate warrants a follow-up. `laguna-xs-2.1` was in scope but dropped — upstream-acknowledged macOS/Metal empty-output bug (revisit after the fix).

---

## Throughput

Decode tok/s is single-stream, no batching, from each source record's own methodology (see Sources). Prefill tok/s and TTFT are left blank where no record measures them — do not fill with a guess.

| Model | Format | Type | Active params | Decode tok/s | Prefill tok/s | TTFT | Host |
|---|---|---|---|---|---|---|---|
| `qwen3.6:35b` | Q4_K_M GGUF | MoE | ~3 B | ~122 | — | — | timmy (RX 9070 XT) |
| `qwen3.6:35b-mlx` | MLX | MoE | ~3 B | ~88-111 (editpred ladder); **41 (agentic 32K, C=1 — see 2026-07-13 note)** | ~1,200-1,700 (scales linearly with prompt length, not flat) | 0.34s @400tok / 0.93s @1.6k tok / 3.4-3.5s @5.6k tok (P50, concurrency=1) | pop (M5 Max) |
| `north-mini-code-1.0:mlx-nvfp4` | MLX nvfp4 | MoE (Cohere) | ~3 B | 73 (agentic 32K, C=1) | — | 0.055s (P50, C=1) | pop (M5 Max) |
| `nemotron3:33b` | Q4_K_M GGUF | Hybrid Mamba MoE | ~3 B | 79 (agentic 32K, C=1) | — | 0.48s (P50, C=1) | pop (M5 Max) |
| `qwen3-coder:30b` | Q4_K_M GGUF | MoE | ~3.3 B | 67 (agentic 32K, C=1) | — | 0.35s (P50, C=1) | pop (M5 Max) |
| `hermes3:8b` *(tag no longer present locally, 2026-07-29 — historical record)* | Q4_0 GGUF | dense | 8 B | 78 (agentic 32K, C=1) | — | 0.41s (P50, C=1) | pop (M5 Max) |
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
| `:coding` tags, agentic tool-loop, hidden verifiers (6 tasks × 3 repeats) | `gemma4:coding-26b` 18/18 (0 bad calls, best style 4.83/5/5, but rewrote the fixture test suite in every T3 run); `gemma4:coding-12b` 18/18; `qwen3.6:coding-gguf` 18/18 (1 bad call in 120, 0 tampers — cleanest conduct); `laguna:coding` 13/18; `nemotron3:coding` 8/18; `qwen3.6:coding` (MLX) 7/18 (see Reliability). `solved` = final sandbox passed the verifier; one nemotron task is solved AND 500-carrying. | `agentic-coding-2026-07-29-084913/` |

## Reliability

| Model | Issue | Status | Record |
|---|---|---|---|
| `qwen3.6:35b-mlx` | Thinking not hard-disabled via the OpenAI `/v1/chat/completions` surface (used by Pi/Ollama-compat clients) even with `enable_thinking:false` — model emits chain-of-thought instead of visible output, client sees empty/truncated response, downstream agents enter write/repair loops on Python/YAML edits. Native `/api/chat` with `think:false` and Anthropic `/v1/messages` with `thinking.type=disabled` work correctly. | Open (partial mitigation: Zed default agent switched to `enable_thinking:false` fast-fix profile) | `BUG-1024` |
| `qwen3.6:coding` (MLX) | Off-by-one indentation (uniform +1 space, docstring-concentrated) in `write_file` tool-call content — logically correct files fail on syntax; 100%→39% solve-rate drop vs the GGUF counterpart. Strongly associated with the MLX-origin serving/artifact path; engine vs conversion/quantization unresolved. Thinking not required for the defect. Sibling MLX-path defect: `format` JSON schemas silently dropped (`INFO-1127`). | Investigating | `BUG-1067` |
| `nemotron3:coding` / qwen3.6 MLX | Ollama tool-call template parser 500s on malformed model tool syntax (6 occurrences across 2 models; class documented for qwen template drift in ollama#16383, nemotron root cause unestablished) — each 500 ends the task's loop. | Open upstream | `agentic-coding-2026-07-29-084913/NOTES.md` |
| `zeta2.1` (Zed edit prediction) | Broken by upstream changes — do not recommend for edit prediction until repaired. | Broken (reported 2026-07-29; no benchmark record yet) | — |

## Recommendation matrix (updated 2026-07-29 from the coding-tag matrix)

| Workload | Default | Why |
|---|---|---|
| Anything you'd use ChatGPT for | `qwen3.6:35b` (MoE, 122 tok/s, timmy) | Fastest quality-per-token at any latency budget |
| Agentic coding (multi-step tool loop) | `qwen3.6:coding-gguf`; `gemma4:coding-12b` for quality-per-GB (7.7 GB) | Both 18/18 on the hidden-verifier matrix. qwen-gguf has the cleanest conduct (1 bad call in 120, zero fixture tampering); `gemma4:coding-26b` also 18/18 with the top style scores but rewrote the fixture test suite in every T3 run — a habit to know about near tests. Supersedes the pre-`:coding`-tag `gemma4:e4b-mlx` row |
| Code-writing / schema-constrained output (engine rule, not a model pick) | GGUF-served tags only | The MLX path silently drops `format` JSON schemas (`INFO-1127`) and is associated with whitespace corruption in tool-call file content (`BUG-1067`) — reserve `-mlx`/MTP speed for prose and chat |
| Low-latency classification/parsing | `gemma4:e2b-mlx` (dense, 5.1B, 183 tok/s, pop) | Fastest; quality is fine for short outputs |
| Pop daily driver (agentic + compendium mgmt) | `qwen3.6:35b-mlx` (current) | No quality regression found vs a larger dense model (IDEA-1054); prefill measured fast and linear (~1,200-1,700 tok/s, see Throughput table) — not a bottleneck for this workload. For code-writing within a session, prefer the coding row above |

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
| `homelab/benchmarks/results/report-2026-07-13-moe-matrix/` | Pop small-MoE agentic matrix (PROJ-1003): `north-mini-code-1.0`, `nemotron3:33b`, `qwen3-coder:30b`, `hermes3:8b` decode/TTFT/aggregate vs the `qwen3.6:35b-mlx` baseline, one env, agentic 32K, C=1..3, NP=3 |
| `homelab/benchmarks/results/agentic-coding-2026-07-29-084913/` (`NOTES.md` + per-tag JSONs + judge files) | Coding-tag quality matrix (PROJ-1003, hardened harness per PR #61): hidden-verifier solve rates, tool-discipline counts, tamper counts, style-judge means for all six `:coding` tags + `--no-think` sensitivity rows; source for the 2026-07-29 recommendation update and `BUG-1067` |
| `compendium/tasks/PROJ-1003/TASK-1015-homelab-qwen3-6-27b-unsloth-mlx-vs-ollama-mlx-benchmark.md` | Scoped but unexecuted — would measure decode/prefill/TTFT for `qwen3.6:27b-mlx` (sibling, not the 35B) |
| `compendium/tasks/PROJ-1003/TASK-1107-homelab-qwen3-5-9b-mlx-vs-qwen3-6-35b-mlx-daily-driver-benchmark.md` | Scoped but unexecuted — would measure agentic + compendium-mgmt quality/latency for `qwen3.6:35b-mlx` vs `qwen3.5:9b-mlx` |
| `compendium/tasks/PROJ-1003/TASK-1111-homelab-benchmark-mlx-lm-server-vs-ollama-mlx-on-pop-m5-max.md` | Scoped but unexecuted — would measure tok/s, TTFT, memory for MLX-native serving vs Ollama MLX on pop |

## Next steps

1. `TASK-1015`, `TASK-1107`, `TASK-1111` remain open for their broader scope (27B quant comparison, 9B-vs-35B daily-driver quality axis, mlx-lm-server-vs-Ollama) — see `compendium/tasks/PROJ-1003/`. The narrow prefill/TTFT question they were each partially scoped to answer for `qwen3.6:35b-mlx` is now covered by the measurement above.
2. Backfill GGUF entries per `TASK-1014` — Qwen3-Coder-30B-A3B, nemotron3-nano, hermes3:8b, north-mini-code-1.0 landed 2026-07-13 (see note + `report-2026-07-13-moe-matrix/`); LFM2.5-8B-A1B still pending. `laguna-xs-2.1` blocked on the upstream macOS/Metal bug.
3. Consider running `prefill-size-breakdown.py` against `qwen3.5:9b-mlx` for a like-for-like prefill comparison feeding `TASK-1107`'s daily-driver decision.
