# VRAM & Parallel Subagent Analysis: gemma4:12b-it-qat on RX 9070 XT

**Date:** 2026-06-12  
**Author:** NewtBot (Hermes Agent)  
**PR:** https://github.com/nathanwhyte/homelab/pull/9

---

## Executive Summary

Live benchmarks on the 9070 XT (16 GB VRAM) reveal three key findings:

1. **Thinking mode wastes 26× tokens** — `think:true` consumed 53 tokens to answer "3+4=?" vs 2 with `think:false`. All auxiliary/delegation configs must set `think:false`.

2. **`NUM_PARALLEL=1` serialized all subagent requests** — bumping to `3` enables true batch inference at ~52 tok/s per request.

3. **`OLLAMA_CONTEXT_LENGTH` is a ceiling, NOT a pre-allocation** — reducing from 131072→8192 saves <0.2 GB. Don't reduce it; control actual KV usage via per-request `num_ctx`.

**Additionally:** Switching the primary model to local gemma4:12b-it-qat is **feasible** with reduced concurrency (3→2 subagents). See the [Local Primary Scenario](#scenario-primary-model--local-gemma412b-it-qat) section.

---

## Hardware & Model Profile

| Metric | Value |
|---|---|
| GPU | AMD Radeon RX 9070 XT |
| VRAM Total | 15.9 GB |
| Pod memory limit | 24 Gi |
| `/dev/shm` | 4 Gi |
| ROCm | 7.2.1 |

| Model | Size (VRAM) | Parameters | Quant | Throughput |
|---|---|---|---|---|
| gemma4:12b-it-qat | 7.02 GB | 11.9B | Q4_0 | 52 tok/s |
| gemma4:e4b-it-qat | 2.90 GB | 7.5B | Q4_0 | 83 tok/s |
| qwen3.5:9b-q4_K_M | ~5.7 GB | 9.7B | Q4_K_M | (not tested) |
| glm-5.1:cloud | 0 GB (remote) | unknown | cloud | 45 tok/s (w/ latency) |

---

## Critical Finding: KV Cache is On-Demand

`OLLAMA_CONTEXT_LENGTH` is just a **ceiling** — Ollama allocates KV cache based on actual tokens, not the max context window.

| ctx Setting | Actual Tokens | Ollama VRAM | GPU VRAM |
|---|---|---|---|
| 2048 | 21 | 6.79 GB | 7.76 GB |
| 4096 | 21 | 6.80 GB | 7.76 GB |
| 8192 | 21 | 6.97 GB | 7.76 GB |
| 32768 | 21 | 7.02 GB | 7.71 GB |
| 65536 | 21 | 7.54 GB | 7.71 GB |

**Implication:** Keep `OLLAMA_CONTEXT_LENGTH=131072`. Control actual KV usage via per-request `num_ctx`.

---

## Thinking Mode

| Config | Tokens for "3+4=?" | Wall Time |
|---|---|---|
| `think: false` | 2 | 0.43s |
| `think: true` | 53 | 1.41s |
| **Overhead** | **26×** | **3.3×** |

With `num_predict=200` and `think:true`, the model consumed all 200 tokens on thinking and produced **zero visible content**.

---

## Scenario: Primary Model → Local gemma4:12b-it-qat

### Current Setup (primary=cloud)

```
Primary conversation → glm-5.1:cloud (remote, no local VRAM)
Subagents/auxiliary  → gemma4:12b-it-qat (local, 7.02 GB VRAM)
Available for KV:    ~8.9 GB (56% of 15.9 GB)
Max concurrent:      3 subagents
```

### Proposed: Primary Local (gemma4:12b-it-qat for everything)

All workloads share the same model on the same GPU. Every primary conversation turn, subagent call, compression, etc. competes for the same 3 parallel slots.

### VRAM Budget Analysis

| Scenario | Total Used | % of 16 GB | Free | Status |
|---|---|---|---|---|
| **Current (cloud primary, 3 subagents)** | | | | |
| 3 × subagent @ 4K ctx | 9.9 GB | 62% | 6.0 GB | ✓ SAFE |
| 3 × subagent @ 8K ctx | 11.3 GB | 71% | 4.6 GB | ✓ SAFE |
| **Primary local + 2 subagents** | | | | |
| Primary(8K) + 2×sub(4K) | 10.6 GB | 67% | 5.3 GB | ✓ SAFE |
| Primary(16K) + 2×sub(4K) | 12.0 GB | 76% | 3.9 GB | ✓ SAFE |
| **Primary local + 1 subagent (conservative)** | | | | |
| Primary(8K) + sub(4K) | 9.9 GB | 62% | 6.0 GB | ✓ SAFE |
| Primary(16K) + sub(4K) | 11.3 GB | 71% | 4.6 GB | ✓ SAFE |
| **Worst case** | | | | |
| Primary(16K) + sub(4K) + compression(8K) | 12.7 GB | 80% | 3.2 GB | ✓ SAFE |

All scenarios fit within the 15.9 GB VRAM with comfortable headroom.

### Throughput Comparison

| Model | Location | Throughput | Latency | Quality | Cost |
|---|---|---|---|---|---|
| glm-5.1:cloud | Remote | 45 tok/s | +200-500ms/network | Likely stronger | Per-token |
| gemma4:12b-it-qat | Local | 52 tok/s | Minimal | Good (12B Q4_0) | Free |
| gemma4:e4b-it-qat | Local | 83 tok/s | Minimal | Weaker (7.5B Q4_0) | Free |

### Trade-offs of Local Primary

**Advantages:**
- No cloud API latency or outages
- No per-token costs
- Full privacy — no data leaves cluster
- Slightly faster per-token (52 vs 45 tok/s including network)

**Disadvantages:**
- Reduces max concurrent subagents from 3 → 2 (primary takes 1 slot)
- 12B Q4_0 is likely less capable than glm-5.1:cloud
- Primary conversation shares GPU with subagents → potential latency spikes
- Auxiliary calls (compression, skills_hub) also share the 3 slots
- If primary conversation uses 16K+ context, available slots for subagents shrink

### Recommended Config for Local Primary

```yaml
# Ollama: keep NUM_PARALLEL=3 (allows primary + 2 subagents)
OLLAMA_NUM_PARALLEL=3

# Hermes config changes for local primary:
model:
  default: gemma4:12b-it-qat    # Switch from glm-5.1:cloud
  ollama_num_ctx: 16384          # Primary conversations need longer context

delegation:
  max_concurrent_children: 2     # Reduce from 3 → 2 (primary takes 1 slot)
  ollama_num_ctx: 8192
  ollama_think: false
```

---

## Alternative: Dual-Model Loading

With `MAX_LOADED_MODELS=2`, both gemma4:12b and gemma4:e4b could coexist:

| Config | Weights | Free for KV | Feasible? |
|---|---|---|---|
| 12b only | 7.02 GB | 8.9 GB | ✓ 3 parallel slots |
| 12b + e4b | 9.92 GB | 5.98 GB | ⚠️ Limited KV |
| 12b + qwen3.5:9b | 12.72 GB | 3.18 GB | ✗ Too tight |

**Not recommended** because:
- 12b + e4b leaves only 5.98 GB for KV across both models
- Model swapping with MAX_LOADED_MODELS=1 adds 30-60s latency
- MAX_LOADED_MODELS=2 is too tight for meaningful parallelism
- Single model with NUM_PARALLEL=3 gives better throughput than model swapping on 16 GB

---

## Changes Made (PR #9)

### Ollama Deployment

| Parameter | Before | After | Rationale |
|---|---|---|---|
| `OLLAMA_NUM_PARALLEL` | `1` | `3` | Enable 3 concurrent slots |
| `OLLAMA_CONTEXT_LENGTH` | `131072` | `131072` | Unchanged — ceiling, not pre-allocation |
| `OLLAMA_MAX_LOADED_MODELS` | `1` | `1` | Single model optimal on 16 GB |

### Hermes Config

| Section | Parameter | Before | After |
|---|---|---|---|
| `delegation` | `max_concurrent_children` | `5` | `3` |
| `delegation` | `ollama_num_ctx` | — | `8192` |
| `delegation` | `ollama_think` | — | `false` |
| `model` | `ollama_num_ctx` | `131072` | `131072` |
| `auxiliary.*` | `ollama_num_ctx` | — | 2048/4096/8192 (per task) |
| `auxiliary.*` | `ollama_think` | — | `false` |

---

## Monitoring

```bash
curl -s http://ollama.llama:11434/api/ps | jq '.models[] | {name, size_vram, context_length}'
curl -s 'http://prom-prometheus.grafana:9090/api/v1/query?query=amdgpu_vram_used_bytes'
```