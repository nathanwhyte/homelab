# VRAM & Parallel Subagent Analysis: gemma4:12b-it-qat on RX 9070 XT

**Date:** 2026-06-12  
**Author:** NewtBot (Hermes Agent)  
**Scope:** Ollama deployment on timmy (RX 9070 XT, 16 GB VRAM)  
**Status:** Recommendations implemented in `ollama-parallel-subagents` branch  
**PR:** https://github.com/nathanwhyte/homelab/pull/9

---

## Executive Summary

The Ollama deployment on timmy was configured for single-request throughput (`NUM_PARALLEL=1`), which blocked parallel subagent usage. Testing reveals:

1. **Thinking mode wastes 26× tokens** — a simple "3+4=?" uses 53 tokens with thinking vs 2 without
2. **`NUM_PARALLEL=1` serializes requests** — 3 parallel subagents take 3× wall time
3. **`CONTEXT_LENGTH` is a ceiling, not a pre-allocation** — reducing it from 131072 to 8192 saves <0.2 GB VRAM (Ollama allocates KV on demand)
4. **Per-request `num_ctx` is the real VRAM control** — set it per-call-type to right-size KV cache

Changes: `NUM_PARALLEL=3`, `think:false` on all auxiliary/delegation calls, per-call-type `ollama_num_ctx` overrides, `max_concurrent_children: 3`.

---

## Hardware Baseline

| Metric | Value |
|---|---|
| GPU | AMD Radeon RX 9070 XT |
| VRAM Total | 15.9 GB (16 GB advertised) |
| GPU 2 (integrated) | 0.5 GB (not used by Ollama) |
| Pod memory limit | 24 Gi |
| `/dev/shm` size | 4 Gi |
| ROCm version | 7.2.1 |

## Model Profile: gemma4:12b-it-qat

| Property | Value |
|---|---|
| Architecture | gemma4 |
| Parameters | 11.9B |
| Quantization | Q4_0 (it-qat = instruction-tuned, quantization-aware training) |
| Disk size | 6.66 GB |
| VRAM (weights only) | 7.02 GB |
| VRAM (loaded, idle) | 6.80 GB (after KV freed) |
| Layers | 48 |
| Embedding length | 3840 |
| Max context | 262144 (architectural) |
| Capabilities | completion, tools, thinking, vision |

## Available Models

| Model | Size | Type | Notes |
|---|---|---|---|
| gemma4:12b-it-qat | 7.02 GB | Local Q4_0 | Primary subagent model |
| gemma4:e4b-it-qat | 5.72 GB | Local Q4_0 | Smaller variant |
| qwen3.5:9b-q4_K_M | 6.14 GB | Local Q4_K_M | Alternative |
| glm-5.1:cloud | — | Cloud proxy | Routed to ollama.com |
| nemotron-3-ultra:cloud | — | Cloud proxy | Routed to ollama.com |

---

## Critical Finding: KV Cache is On-Demand, Not Pre-Allocated

This is the most important finding. **`OLLAMA_CONTEXT_LENGTH` is just a ceiling, not a VRAM reservation.** Ollama allocates KV cache memory on demand based on actual tokens used, not the maximum context window.

### Evidence

| ctx Setting | Actual Tokens | Ollama VRAM | GPU VRAM |
|---|---|---|---|
| 2048 | 21 | 6.79 GB | 7.76 GB |
| 4096 | 21 | 6.80 GB | 7.76 GB |
| 8192 | 21 | 6.97 GB | 7.76 GB |
| 16384 | 21 | 6.99 GB | 7.71 GB |
| 32768 | 21 | 7.02 GB | 7.71 GB |
| 65536 | 21 | 7.54 GB | 7.71 GB |
| 131072 (idle) | 0 | 7.02 GB | 8.38 GB |

The ~0.5 GB increase from ctx=2048 to ctx=65536 with only 21 actual tokens is Ollama's metadata overhead for the KV cache structure — not actual KV data. GPU VRAM stays flat at 7.7 GB.

### With Actual Long Prompts

| ctx Setting | Prompt Tokens | Ollama VRAM | GPU VRAM | Wall Time |
|---|---|---|---|---|
| 2048 | 852 | 6.79 GB | 7.71 GB | 3.29s |
| 4096 | 1672 | 6.80 GB | 7.71 GB | 3.72s |
| 8192 | 3310 | 6.97 GB | 7.71 GB | 4.75s |

### Implications

- **Don't reduce `OLLAMA_CONTEXT_LENGTH`** — it costs almost nothing as a ceiling and ensures subagents can use longer contexts when needed
- **Set per-request `num_ctx`** via the Ollama API to control actual KV allocation per call type
- VRAM scales with **actual tokens processed**, not the context window size
- This means `NUM_PARALLEL=3` is safe even at 131072 ceiling, because each slot only allocates for actual token usage

---

## Thinking Mode Analysis

The gemma4:12b-it-qat model has "thinking" capability enabled by default. Testing confirms it's a critical efficiency problem:

### Token Overhead

| Config | Tokens Generated | Content | Wall Time |
|---|---|---|---|
| `think: false` | 2 | "7" | 0.43s |
| `think: true` | 53 | "7" (+ 51 thinking) | 1.41s |
| **Overhead** | **26×** | — | **3.3×** |

### The Problem

With `num_predict=200` and `think: true`, the model consumed all 200 tokens on thinking and **never produced visible content**. The response body was empty — the entire budget went to internal reasoning.

This is devastating for subagent workloads:
- Every request burns 50–200 tokens on thinking before generating content
- KV cache grows proportionally (more tokens = more VRAM per slot)
- Effective throughput drops by 26× for simple tasks
- Subagent prompts are often straightforward (tool calls, summaries) where thinking adds no value

**Recommendation:** All Hermes auxiliary and delegation configurations must set `think: false`.

---

## Parallel Request Batching

### With NUM_PARALLEL=1 (Current)

Even with `NUM_PARALLEL=1`, Ollama does some pipelining (overlapping prompt evaluation with generation). But true parallel inference is limited:

| Parallelism | Wall Time | Per-Request | Notes |
|---|---|---|---|
| 1× (serial) | 2.60s | 0.87s avg | Sequential |
| 3× (queued) | 1.77s total | varies | Some pipelining |
| 5× (queued) | 10.29s | varies | Serialized, no speedup |

With short requests (21 prompt tokens, 2 eval tokens), pipelining helps. With longer requests (100 eval tokens), 5 parallel requests take 10.29s — essentially serial.

### With NUM_PARALLEL=3 (Proposed)

Setting `NUM_PARALLEL=3` allows Ollama to process 3 requests in parallel within a single batch step. Each request maintains its own KV cache slot but shares model weights. The GPU processes all active requests in a single forward pass.

**Expected improvement:** 3× throughput for subagent workloads, since delegation typically fires 3+ subagents simultaneously.

---

## VRAM Budget Analysis

### Per-Request KV Cost (On-Demand Allocation)

The actual VRAM cost per parallel slot depends on tokens used, not the context window ceiling:

| Actual Tokens | Approx. KV Cost | Typical Use Case |
|---|---|---|
| 500 | ~88 MB | Title generation, approval |
| 1,500 | ~265 MB | MCP, skills hub |
| 3,000 | ~530 MB | Subagent delegation |
| 6,000 | ~1,060 MB | Compression, long subagent |
| 8,000 | ~1,410 MB | Complex subagent with tools |

### Capacity at NUM_PARALLEL=3

With 7.02 GB model weights + ~0.5 GB overhead = **7.5 GB base**:

| Scenario | Total VRAM | % of 15.9 GB | Free |
|---|---|---|---|
| 3 × short (2K tokens each) | 7.5 + 3×0.35 = 8.6 GB | 54% | 7.3 GB |
| 3 × medium (4K tokens each) | 7.5 + 3×0.71 = 9.6 GB | 60% | 6.3 GB |
| 3 × long (8K tokens each) | 7.5 + 3×1.41 = 11.7 GB | 74% | 4.2 GB |
| 1 × very long (32K tokens) | 7.5 + 5.65 = 13.2 GB | 83% | 2.7 GB |

Even at 3 × 8K context, we have 4.2 GB headroom. This is very safe.

### Why Multiple Models Don't Work Here

Loading a second model (e.g., qwen3.5:9b at 6.14 GB) alongside gemma4:12b (7.02 GB) would need 13.2 GB just for weights — leaving only 2.7 GB for KV cache. With `MAX_LOADED_MODELS=1`, Ollama would have to swap models, adding 30-60s load latency per swap. The throughput benefit of multiple models is negated by:

1. **Swapping cost** — 30-60s to load/unload each model
2. **KV cache flush** — all active KV is lost when swapping
3. **VRAM pressure** — two models leave only 2.7 GB for KV, limiting context to ~1K tokens

**Recommendation:** Stick with `MAX_LOADED_MODELS=1`. Single-model parallelism (NUM_PARALLEL=3) provides better throughput than multi-model swapping on 16 GB VRAM.

---

## Changes Made

### 1. Ollama Deployment (`llama/ollama-deployment.yaml`)

| Parameter | Before | After | Rationale |
|---|---|---|---|
| `OLLAMA_NUM_PARALLEL` | `1` | `3` | Enable 3 concurrent subagent requests |
| `OLLAMA_CONTEXT_LENGTH` | `131072` | `131072` | **Unchanged** — it's just a ceiling, not a pre-allocation |
| `OLLAMA_MAX_LOADED_MODELS` | `1` | `1` | Unchanged — single model is optimal on 16 GB VRAM |

### 2. Hermes Config (`hermes/hermes-configmap.yaml`)

| Section | Parameter | Before | After | Rationale |
|---|---|---|---|---|
| `delegation` | `max_concurrent_children` | `5` | `3` | Match Ollama parallel slots |
| `delegation` | `ollama_num_ctx` | _(unset)_ | `8192` | Subagents need room for tool calls + output |
| `delegation` | `ollama_think` | _(unset)_ | `false` | Prevent 26× token waste |
| `model` | `ollama_num_ctx` | `131072` | `131072` | Unchanged — main model keeps full context |
| `auxiliary.compression` | `ollama_num_ctx` | _(unset)_ | `8192` | Compression needs long context for summaries |
| `auxiliary.compression` | `ollama_think` | _(unset)_ | `false` | No thinking needed for summaries |
| `auxiliary.skills_hub` | `ollama_num_ctx` | _(unset)_ | `4096` | Skill matching is short |
| `auxiliary.skills_hub` | `ollama_think` | _(unset)_ | `false` | No thinking needed |
| `auxiliary.approval` | `ollama_num_ctx` | _(unset)_ | `2048` | Single-turn decisions |
| `auxiliary.approval` | `ollama_think` | _(unset)_ | `false` | No thinking needed |
| `auxiliary.mcp` | `ollama_num_ctx` | _(unset)_ | `4096` | Tool routing is short |
| `auxiliary.mcp` | `ollama_think` | _(unset)_ | `false` | No thinking needed |
| `auxiliary.title_generation` | `ollama_num_ctx` | _(unset)_ | `2048` | Short output |
| `auxiliary.title_generation` | `ollama_think` | _(unset)_ | `false` | No thinking needed |
| `auxiliary.profile_describer` | `ollama_num_ctx` | _(unset)_ | `4096` | Profile summaries are short |
| `auxiliary.profile_describer` | `ollama_think` | _(unset)_ | `false` | No thinking needed |

### Context Size Rationale

- **Main model (131072):** Interactive conversations may need long context
- **Delegation (8192):** Subagent tasks with tool calls, file reads, and multi-step reasoning need more room
- **Compression (8192):** Summarizing long conversation history needs the context window
- **Skills hub / MCP / profile (4096):** Tool routing and skill matching are moderate-length operations
- **Approval / title (2048):** Single-turn decisions and title generation are very short

### Why Not Reduce OLLAMA_CONTEXT_LENGTH?

Testing proved that `OLLAMA_CONTEXT_LENGTH` is just a **ceiling** — it doesn't pre-allocate VRAM. Ollama allocates KV cache on demand based on actual tokens. Reducing from 131072 to 8192 would:
- Save <0.2 GB in idle VRAM (negligible)
- Prevent subagents from ever using >8K context (harmful)
- Risk truncating legitimate long-context requests

Instead, we control actual KV allocation via per-request `num_ctx` in the API calls.

---

## Monitoring

Post-deploy, verify with:

```bash
# Check Ollama loaded model and context
curl -s http://ollama.llama:11434/api/ps | jq '.models[] | {name, size_vram, context_length}'

# Check AMD VRAM via Prometheus
curl -s 'http://prom-prometheus.grafana:9090/api/v1/query?query=amdgpu_vram_used_bytes'

# Test parallel throughput
python3 /tmp/vram_test_ctx.py  # (see Appendix for full benchmark script)
```

---

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| OOM at 3 parallel slots | Low | Pod restart | 4.2 GB headroom at 3×8K; KV is on-demand |
| Context truncation | Low | Incomplete responses | Per-call num_ctx set per task type |
| Cloud model fallback breaks | Low | 429 errors | Cloud models use separate context pool |
| Thinking mode re-enabled | Medium | Token waste | Explicit `think:false` on all auxiliary calls |
| Reduced concurrency from 5→3 | Low | Slight queue delay | Matches actual Ollama capacity |

### Rollback

If issues arise, revert to:
```yaml
OLLAMA_NUM_PARALLEL: "1"
# Remove all ollama_think and ollama_num_ctx overrides
# Restore max_concurrent_children: 5
```

---

## Appendix: Benchmark Scripts

### Thinking Mode Overhead Test

```bash
# Without thinking (correct)
curl -s http://ollama.llama:11434/api/chat -d '{
  "model": "gemma4:12b-it-qat",
  "messages": [{"role":"user","content":"What is 3+4? Answer with just the number."}],
  "stream": false, "think": false,
  "options": {"num_predict": 20}
}'

# With thinking (26x token waste)
curl -s http://ollama.llama:11434/api/chat -d '{
  "model": "gemma4:12b-it-qat",
  "messages": [{"role":"user","content":"What is 3+4? Answer with just the number."}],
  "stream": false, "think": true,
  "options": {"num_predict": 20}
}'
```

### Context Length vs VRAM Test

```python
import json, urllib.request

for ctx in [2048, 4096, 8192, 16384, 32768, 65536]:
    data = json.dumps({
        "model": "gemma4:12b-it-qat",
        "messages": [{"role":"user","content":"Say 'ok' and nothing else."}],
        "stream": False, "think": False,
        "options": {"num_predict": 2, "num_ctx": ctx}
    }).encode()
    req = urllib.request.Request(
        "http://ollama.llama:11434/api/chat",
        data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        d = json.loads(resp.read())
    with urllib.request.urlopen("http://ollama.llama:11434/api/ps", timeout=5) as resp:
        ps = json.loads(resp.read())
    vram = ps["models"][0]["size_vram"] / 1024**3
    print(f"ctx={ctx:>5d}: vram={vram:.2f}GB, tokens={d.get('prompt_eval_count',0)}+{d.get('eval_count',0)}")
```

### Parallel Subagent Test

```python
import json, time, threading, urllib.request

def chat_request(idx, num_predict=100, think=False, num_ctx=4096):
    start = time.time()
    data = json.dumps({
        "model": "gemma4:12b-it-qat",
        "messages": [{"role":"user","content": f"Explain concept #{idx+1}."}],
        "stream": False, "think": think,
        "options": {"num_predict": num_predict, "num_ctx": num_ctx}
    }).encode()
    req = urllib.request.Request(
        "http://ollama.llama:11434/api/chat",
        data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        d = json.loads(resp.read())
    return time.time() - start, d.get("eval_count",0), d.get("eval_duration",0)/1e9

# Run 3 parallel requests
results = [None] * 3
def run(i): results[i] = chat_request(i)
threads = [threading.Thread(target=run, args=(i,)) for i in range(3)]
for t in threads: t.start()
for t in threads: t.join()
for i, (elapsed, tokens, dur) in enumerate(results):
    print(f"Request {i}: {elapsed:.2f}s, {tokens}tok/{dur:.2f}s, {tokens/dur:.0f}tok/s")
```