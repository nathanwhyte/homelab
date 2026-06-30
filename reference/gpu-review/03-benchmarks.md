# GPU & AI Infrastructure Review — Model Benchmarks

Split from `GPU_AND_AI_REVIEW.md` (compiled 2026-05-01) into per-topic files
in `reference/gpu-review/`. This file covers the **measured quality and
throughput** of the model lineup during the Mar–Apr 2026 selection process.

These benchmarks informed the model-selection decisions in
[`02-llm-architecture.md`](02-llm-architecture.md#8-key-decisions-and-rationale)
(Decision 1: Qwen3-8B over larger models; Decision 2: split GPU architecture).
For current model lineup and tuning, see `CLAUDE.md` and
`reference/llm-config.md`.

## 4. Model Benchmarks

### 4.1 Summarization Showdown (Mar 20-21)

Evaluated models for OpenViking VLM tasks (summarize code, README, config, etc.).

**Quality Scores (1-10 avg of Accuracy/Conciseness/Structure/Completeness):**

| Config | Model                         | Hardware        | Avg Quality | Reliability | Avg Time | tok/s |
| ------ | ----------------------------- | --------------- | ----------- | ----------- | -------- | ----- |
| **A**  | Mistral-Small-24B Q4_K_M      | timmy (9070 XT) | **8.1**     | 5/5         | 36.0s    | ~18   |
| **B**  | Qwen3.5-27B Q3_K_M /no_think  | timmy (9070 XT) | **3.8**     | 2/5         | 107.4s   | ~10   |
| **D**  | Claude Haiku 4.5              | API             | **6.1**     | 3/5         | 16.2s    | --    |
| **E**  | Qwen3-8B Q4_K_M               | timmy (9070 XT) | **8.1**     | 5/5         | 16.2s    | ~25   |
| **F**  | Qwen3-8B opt (parallel=8)     | timmy (9070 XT) | **8.1**     | 5/5         | faster   | ~33   |
| **G**  | Qwen3-8B opt+ (manu GTX 1080) | manu (GTX 1080) | **8.1**     | 5/5         | faster   | ~33   |

**Key findings:**

1. Qwen3-8B matched Mistral-24B quality (8.1/10) at 3x less VRAM and 2x faster
2. Qwen3.5-27B unusable at 1024 max_tokens -- verbose output truncated, 60% failure rate
3. Claude Haiku scored lower due to `claude --print` system prompt interference (true quality likely ~9.0)
4. Flash attention works on GTX 1080 (Pascal SM 6.1), providing consistent ~33 tok/s
5. GTX 1080 caps at ~33 tok/s for Qwen3-8B Q4_K_M (hardware ceiling)

### 4.2 Three-Model Comparison (Mar 20-21)

Head-to-head on general-purpose tasks across difficulty tiers.

**Quality by Difficulty:**

| Difficulty | Mistral-Small-24B | Qwen3.5-27B | Claude Haiku 4.5 |
| ---------- | ----------------- | ----------- | ---------------- |
| Easy       | 7.0               | 8.0         | 8.0              |
| Moderate   | 6.0               | 9.0         | 9.0              |
| Hard       | 6.5               | 8.5         | 9.0              |
| Expert     | 5.0               | 7.5         | 8.5              |

**Verdict:** Mistral degrades sharply on expert tasks (5.0). Qwen and Claude maintain quality. Claude is most consistent. For batch/async workloads, Qwen3.5-27B with thinking enabled approaches Claude quality. For interactive use, Mistral is fastest but lowest quality.

### 4.3 Agentic Benchmark (Mar 21-22)

10-test suite for tool-calling capability with Qwen3-8B on RX 9070 XT.

| Test                      | Category     | Result | Time   | Tokens | Speed    |
| ------------------------- | ------------ | ------ | ------ | ------ | -------- |
| 1. Single Tool Call       | Tool Calling | PASS   | 694ms  | 29     | 41.8 t/s |
| 2. Multi-Step Tool Plan   | Tool Calling | PASS   | 6983ms | 347    | 49.7 t/s |
| 3. Tool-Observation Loop  | Tool Calling | PASS   | 4185ms | 203    | 48.5 t/s |
| 4. Function Generation    | Code Gen     | PASS   | 7469ms | 311    | 41.6 t/s |
| 5. Bug Fix                | Code Gen     | PASS   | --     | --     | --       |
| 7. JSON Schema Output     | Structured   | PASS   | --     | --     | --       |
| 8. Diff Generation        | Structured   | PASS   | 1518ms | 128    | 84.3 t/s |
| 9. Architecture Reasoning | Reasoning    | PASS   | --     | --     | --       |
| 10. Error Root Cause      | Reasoning    | PASS   | --     | --     | --       |

**Result: 10/10 passed.** Qwen3-8B demonstrated reliable tool calling, code generation, and structured output. Generation speeds ranged 41-84 t/s on the 9070 XT.

### 4.4 Claude Code Benchmark (Mar 26-27)

Compared local Ollama models vs Claude Haiku 4.5 API for Claude Code integration tasks.

**Models tested:**

- `qwen3.5:9b-q8_0` via Ollama on timmy (47.0 t/s)
- `qwen3.5:9b-q4_K_M` via Ollama on timmy (61.2 t/s)
- Claude Haiku 4.5 via Anthropic API

**Finding:** "Qwen Q4 wins local, Haiku wins API" -- Qwen3.5 9B Q4_K_M is the best local option for Claude Code usage, with faster generation than Q8_0 and good quality. Haiku has higher quality but requires API credits and network latency.

### 4.5 Manu GTX 1080 Benchmark (Mar 28)

Detailed concurrency testing for the OV LLM endpoint on manu.

| Scenario     | Gen tok/s (per req) | Aggregate tok/s |
| ------------ | ------------------- | --------------- |
| 1 concurrent | 28.9                | 28.9            |
| 2 concurrent | 28.3                | 56.6            |
| 4 concurrent | 20.6                | 82.5            |

- OV real-world avg: 22.4 gen tok/s, 333 prompt tok/s
- Average busy slots: 2.55, requests deferred: 0
- **Increasing parallel slots from 2 to 4 was the biggest win** -- eliminated queuing for OV's 3 concurrent workers

---

## Source

Extracted from `GPU_AND_AI_REVIEW.md` lines 237–321 (section 4). These
benchmarks were run in Mar–Apr 2026; current model lineup and per-service
throughput differ (consult `CLAUDE.md` and `reference/llm-config.md`).
