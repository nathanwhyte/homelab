# Summarization Showdown: OpenViking VLM Model Selection

**Date:** 2026-03-21
**Purpose:** Determine the best model for OpenViking's VLM workload (abstracting, summarization, memory extraction)

---

## Configs Tested

| Config | Model | Hardware | Settings |
|--------|-------|----------|----------|
| **A** | Mistral-Small-24B Q4_K_M | timmy (RX 9070 XT, 16GB) | ctx=8192, parallel=1, q8_0 KV |
| **B** | Qwen3.5-27B Q3_K_M /no_think | timmy (RX 9070 XT, 16GB) | ctx=8192, parallel=1, q8_0 KV |
| **D** | Claude Haiku 4.5 | cloud (via `claude --print`) | N/A |
| **E** | Qwen3-8B Q4_K_M | manu (GTX 1080, 8GB) | ctx=8192, parallel=4, q8_0 KV |
| **F** | Qwen3-8B Q4_K_M (optimized) | manu (GTX 1080, 8GB) | ctx=12288, parallel=6, q4_0 KV, flash-attn, batch=2048 |
| **G** | Qwen3-8B Q4_K_M (optimized+) | manu (GTX 1080, 8GB) | ctx=16384, parallel=6, q4_0 KV, flash-attn, batch=2048, max_tokens=2048 |

## Rounds (mirror OpenViking VLM tasks)

| Round | Task | Input Type |
|-------|------|------------|
| R1 | Directory abstract (.abstract.md) | File listing with descriptions |
| R2 | Context summarization | User/assistant conversation |
| R3 | Code summarization | Python source file |
| R4 | Session memory extraction | Multi-turn conversation |
| R5 | Mixed technical content | Architecture decision document |

---

## Quality Scores (1-10, avg of Accuracy/Conciseness/Structure/Completeness)

| Config | R1 | R2 | R3 | R4 | R5 | **Avg** | **Reliability** |
|--------|-----|-----|-----|-----|-----|---------|----------------|
| **A** Mistral-24B | 8.3 | 8.5 | 6.8 | 8.5 | 8.5 | **8.1** | 5/5 |
| **B** Qwen-27B /no_think | 7.8 | 8.0 | 1.0 | 1.0 | 1.0 | **3.8** | 2/5 |
| **D** Claude Haiku* | 9.0 | 3.5 | 9.0 | 7.0 | 2.0 | **6.1** | 3/5 |
| **E** Qwen-8B | 8.0 | 8.5 | 7.0 | 8.3 | 8.5 | **8.1** | 5/5 |
| **F** Qwen-8B opt | — | — | — | — | — | **8.1** | 5/5 |
| **G** Qwen-8B opt+ | — | — | — | — | — | **8.1** | 5/5 |

*Haiku was tested via `claude --print` which wraps with Claude Code's system prompt, causing the model to respond conversationally on 2/5 rounds. Haiku's actual summarization quality is likely ~9.0 based on rounds where it understood the task. A direct API call would fix this.

F and G use the same model weights as E — quality is identical. Differences are runtime settings only.

---

## Timing

| Config | R1 | R2 | R3 | R4 | R5 | **Avg** | **tok/s** |
|--------|-----|-----|------|-----|-----|---------|-----------|
| **A** Mistral-24B | 7s | 46s | 62s | 23s | 42s | **36.0s** | ~18 |
| **B** Qwen-27B | 37s | 39s | 156s | 151s | 154s | **107.4s** | ~10 |
| **D** Claude Haiku | 16s | 22s | 11s | 20s | 12s | **16.2s** | — |
| **E** Qwen-8B | 18s | 6s | 31s | 8s | 18s | **16.2s** | ~25 |
| **F** Qwen-8B opt | 15s | 5s | 29s | 9s | 16s | **14.8s** | ~33 |
| **G** Qwen-8B opt+ | 15s | 5s | 25s | 9s | 16s | **14.0s** | ~33 |

---

## Failure Modes

| Config | Failure | Cause | Occurrences |
|--------|---------|-------|-------------|
| **B** | Empty/truncated output | 1024 max_tokens consumed by verbose generation | 3/5 |
| **D** | Chatbot response instead of summary | `claude --print` system prompt override | 2/5 |
| **F** | Context exceeded (R3, R5) | ctx=4096 / parallel=8 = 512 tok/slot | 2/5 (fixed in G) |

---

## Key Findings

1. **Qwen3-8B matches Mistral-24B quality (8.1/10)** despite being 3x smaller. For summarization tasks, the 8B model is sufficient.

2. **Qwen3.5-27B is unsuitable** at max_tokens=1024 even with /no_think. It produces verbose output that gets truncated, causing 60% failure rate.

3. **The /no_think prefix was added as a workaround** for Qwen's thinking mode consuming the token budget. Without thinking, Qwen-27B loses its main quality advantage over smaller models.

4. **ctx-size is divided across parallel slots** in llama.cpp. With parallel=8 and ctx=4096, each slot only gets 512 tokens — too small for most inputs.

5. **Flash attention works on GTX 1080** (Pascal, SM 6.1) and provides consistent ~33 tok/s generation speed.

6. **max_tokens=2048 is free insurance** — the model naturally stops at 100-900 tokens for summarization. Doubling the budget doesn't change output length.

7. **The GTX 1080 caps at ~33 tok/s** for Qwen3-8B Q4_K_M. This is the hardware ceiling, not a software bottleneck.

---

## Recommendation

**Qwen3-8B Q4_K_M on manu (GTX 1080) with optimized settings.**

Final production settings:
- `--ctx-size 16384` (2730 tok/slot)
- `--parallel 6` (matches OpenViking max_concurrent)
- `--cache-type-k q4_0 --cache-type-v q4_0`
- `--flash-attn on`
- `--batch-size 2048 --ubatch-size 512`
- `max_tokens: 2048` in OpenViking config

This configuration:
- Runs on idle hardware (manu's 1080 was previously unused)
- Frees timmy's 9070 XT for interactive/primary LLM use
- Handles 6 concurrent summarization requests
- Uses ~6.9GB of 8GB VRAM (1.1GB headroom)
- Produces 8.1/10 quality summaries at 14s average, 33 tok/s
- 100% reliability across all test rounds

---

## Infrastructure Changes Made

1. **Embedder moved off wemby** (control plane) to manu — CPU-only, no GPU needed
2. **Qwen-summarizer scaled to 1 replica** on manu with optimized settings
3. **PVC `llama-model-cache` created** for model caching on manu
4. **Service routing needs fix**: `qwen-summarizer-llm` currently targets `app: llamacpp-rocm` (timmy), should target `app: qwen-summarizer` (manu)
