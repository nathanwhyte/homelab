# Claude Code Agent Benchmark — Full Results (2026-03-28)

## Setup

- **Hardware**: timmy RX 9070 XT (16GB VRAM, 32GB RAM)
- **Ollama config**: flash attention, q4_0 KV cache, 32K ctx, num_batch 1024, keep-alive infinite
- **Test suite**: 20 tests across 7 categories, 2 runs each
- **Categories**: Tool Calling (T1-T3), Code Gen (T4-T6), Code Understanding (T7-T9), Reasoning (T10-T12), Hard Code Gen (T13-T15), Hard Tool Calling (T16-T18), Agentic App Building (T19-T20)

## Results

| Model | Pass | Avg ms | t/s | Tool | Code | Edit | Plan | Hard | HdTool | Agent |
|-------|------|--------|-----|------|------|------|------|------|--------|-------|
| Claude Haiku 4.5 | **31/40** | **3,568** | API | **6/6** | **6/6** | **6/6** | 5/6 | 2/6 | **6/6** | 0/4 |
| Claude Sonnet 4.6 | 30/40 | 8,843 | API | **6/6** | **6/6** | 5/6 | 5/6 | 2/6 | **6/6** | 0/4 |
| Qwen3-Coder 30B-A3B MoE | 27/40 | 7,762 | 52 | 2/6 | **6/6** | **6/6** | **6/6** | 2/6 | 3/6 | **2/4** |
| Qwen3.5 9B Q4_K_M (base) | 27/40 | 16,491 | 59 | **6/6** | 4/6 | 5/6 | **6/6** | 2/6 | 3/6 | 1/4 |
| Qwen-Claude (tuned, /no_think) | 25/40 | 15,365 | 59 | **6/6** | 5/6 | 3/6 | 5/6 | 2/6 | 3/6 | 1/4 |
| Qwen3 14B | 17/40 | 25,996 | 52 | **6/6** | 2/6 | 3/6 | 4/6 | 0/6 | 2/6 | 0/4 |

## Key Findings

### 1. Haiku 4.5 dominates for Claude Code tool-calling
Best quality (31/40), fastest (3.5s), perfect tool routing (6/6 basic + 6/6 hard). At ~$0.001/call, hard to justify local models purely on performance.

### 2. Sonnet 4.6 is slower than Haiku for this workload
8.8s avg vs 3.5s, with marginally lower quality (30 vs 31). The extra reasoning capability doesn't help on tool-selection tasks. Not worth the cost premium for Claude Code subagent work.

### 3. /no_think hurts more than it helps
The tuned qwen-claude model (with SYSTEM "/no_think") scores 25/40 vs 27/40 for the base qwen3.5. Key difference: T7 (bug detection) — base model passes consistently, tuned model fails every time. The thinking suppression prevents the model from reasoning through subtle bugs.

### 4. Qwen3-Coder MoE is the best local coder but unreliable tool caller
Perfect on code gen and understanding (6/6 each), but only 2/6 on basic tool calling — it often returns text instead of structured tool calls. It's also the only model that successfully builds complete apps (T19/T20). If Ollama fixes its tool-calling pipeline for this model, it would be the clear local winner.

### 5. Qwen3 14B is a trap
Bigger model, worse results. At 14B, thinking tokens consume the entire output budget (num_predict=2048), producing empty or truncated responses for code gen and hard tasks. 52 t/s decode speed makes it slower than the 9B models too.

### 6. The speed optimizations work but don't change t/s
q4_0 KV cache, num_batch 1024, and 32K ctx don't measurably change decode tok/s (both tuned and base hover at 59 t/s). The 9070 XT appears compute-bound at this model size, not memory-bandwidth-bound. The main benefit is VRAM headroom for larger contexts.

### 7. Hard tests expose real gaps
T13 (multi-file architecture), T15 (debug stack trace with diagnosis), and T19/T20 (app building) fail for nearly all models. These require combining tool use with substantial code generation in a single turn — a fundamentally different task than simple tool routing.

## Recommendations

1. **For Claude Code**: Use Haiku 4.5 as the subagent model. If local is required, use qwen3.5:9b-q4_K_M without /no_think.
2. **Drop /no_think from qwen-claude Modelfile** — it reduces quality without measurably improving speed.
3. **Keep the Ollama speed tunings** (q4_0 KV, num_batch 1024, 32K ctx) — they save VRAM even if t/s is unchanged.
4. **Watch qwen3-coder** — if Ollama fixes tool-call routing for this model, re-benchmark. It could be the best local option.
5. **Don't use qwen3:14b** — strictly worse than 9B variants on this hardware.
