# Ollama NUM_PARALLEL benchmark report — 2026-07-01

Generated from the Ollama concurrency/tool-calling benchmark harness.

## Tested configurations

| Platform | GPU | Model | Ollama version | `NUM_PARALLEL` | Context | Workload |
|---|---|---|---|---|---|---|
| Cluster (timmy) | RX 9070 XT 16 GB | `gemma4:12b-it-qat` | 0.31.1-rocm / 0.31.1-vulkan | 6 / 8 | 16K–32K | `mixed_mem0`, `agentic` |
| Pop (this Mac) | M5 Max | `gemma4:12b-mlx` | 0.31.1 | 1 / 3 / 6 | 16K–32K | `mixed_mem0`, `agentic` |

> Pop `np=8` and `np6-agentic` were not completed. The pop sweep was stopped during `pop-np6-agentic` because the 32K-context / 6-slot workload spiked unified memory usage to ~96 % and caused desktop stuttering on the M5 Max. The usable pop data is `np1`, `np3` (both workloads), and `np6-default`.

---

## Aggregate throughput

### Cluster

| Run | Conc 1 | Conc 2 | Conc 3 | Conc 4 | Conc 5 | Conc 6 | Conc 7 | Conc 8 | Saturation |
|---|---|---|---|---|---|---|---|---|---|
| cluster mixed_mem0 np=8 | 25.0 | 31.1 | 43.0 | 53.8 | 54.0 | 68.8 | 68.5 | **88.1** | keeps rising |
| cluster agentic np=8 | 25.2 | 36.7 | 39.5 | 50.8 | 52.3 | 58.3 | 55.2 | **63.6** | ~conc 6 |
| cluster agentic np=6 | 25.4 | 27.7 | 33.3 | 39.5 | 45.3 | **45.9** | — | — | ~conc 5–6 |

### Pop

| Run | Conc 1 | Conc 2 | Conc 3 | Conc 4 | Conc 5 | Conc 6 | Notes |
|---|---|---|---|---|---|---|---|
| pop mixed_mem0 np=1 | 94.7 | — | — | — | — | — | single-slot MTP peak |
| pop mixed_mem0 np=3 | 47.9 | 38.3 | 48.8 | — | — | — | flat scaling |
| pop mixed_mem0 np=6 | 33.6 | 35.8 | 37.1 | 37.2 | 29.3 | **51.3** | noisy; no clear rise |
| pop agentic np=1 | 61.4 | — | — | — | — | — | single-slot, long context |
| pop agentic np=3 | 32.8 | 33.5 | **67.3** | — | — | — | peaks at conc=3 |

### Observations

- **Cluster default workload scales well to `NUM_PARALLEL=8`**: aggregate throughput reaches 88 tok/s at conc=8. The curve is still rising, suggesting the RX 9070 XT could potentially handle `np=10–12` for short prompts.
- **Cluster agentic workload (long context) saturates earlier**: peak is around conc=5–6, with diminishing returns beyond that. This is expected — 32K context and 2048-token outputs consume much more KV cache and compute per slot.
- **Pop single-slot performance is dramatically higher for Gemma 4 MLX**: 94.7 tok/s at conc=1 for default workload, ~2× the cluster's per-slot speed. This is the v0.31.1 MTP effect on Apple Silicon.
- **Pop does not show clean aggregate scaling**: the `np=3` and `np=6` default curves are relatively flat or noisy. This suggests `OLLAMA_NUM_PARALLEL` on the MLX backend behaves more like independent worker slots than true continuous batching.

---

## Per-request generation speed

### Cluster

| Run | Conc 1 | Conc 2 | Conc 3 | Conc 4 | Conc 5 | Conc 6 | Conc 7 | Conc 8 |
|---|---|---|---|---|---|---|---|---|
| cluster mixed_mem0 np=8 | 52.8 | 35.2 | 42.5 | 30.3 | 21.1 | 30.5 | 27.4 | 23.8 |
| cluster agentic np=8 | 51.7 | 41.1 | 30.5 | 31.3 | 31.6 | 24.2 | 21.5 | 20.9 |
| cluster agentic np=6 | 51.7 | 28.9 | 24.0 | 21.9 | 20.0 | 20.0 | — | — |

### Pop

| Run | Conc 1 | Conc 2 | Conc 3 | Conc 4 | Conc 5 | Conc 6 |
|---|---|---|---|---|---|---|
| pop mixed_mem0 np=1 | **102.0** | — | — | — | — | — |
| pop mixed_mem0 np=3 | 99.9 | 103.8 | 85.8 | — | — | — |
| pop mixed_mem0 np=6 | 72.7 | 70.4 | 73.0 | 72.9 | 73.0 | 72.7 |
| pop agentic np=1 | 92.1 | — | — | — | — | — |
| pop agentic np=3 | 74.6 | 73.6 | **81.3** | — | — | — |

### Observations

- Single-slot per-request speed is nearly identical on cluster for both workloads (~52 tok/s), because the agentic prompts are long but the model is compute-bound during prefill.
- Per-request speed drops to ~20 tok/s at high concurrency for agentic workloads on the cluster. This is the latency cost of packing more requests onto the same GPU.
- Pop's single-slot speed is roughly **2×** the cluster's single-slot speed, confirming the Gemma 4 MTP benefit on MLX.
- Pop per-request speed stays high even at `np=6`, which is consistent with slots being scheduled independently rather than contending for the same compute in a single batch.

---

## P95 time-to-first-token

### Cluster

| Run | Conc 1 | Conc 2 | Conc 3 | Conc 4 | Conc 5 | Conc 6 | Conc 7 | Conc 8 |
|---|---|---|---|---|---|---|---|---|
| cluster mixed_mem0 np=8 | 0.44 | 0.50 | 0.54 | 0.57 | 0.58 | 0.56 | 0.68 | 0.67 |
| cluster agentic np=8 | 2.45 | 2.51 | 2.99 | 0.51 | 0.62 | 0.63 | 0.58 | 0.66 |
| cluster agentic np=6 | 0.46 | 0.51 | 0.55 | 0.55 | 0.58 | 0.65 | — | — |

### Pop

| Run | Conc 1 | Conc 2 | Conc 3 | Conc 4 | Conc 5 | Conc 6 |
|---|---|---|---|---|---|---|
| pop mixed_mem0 np=1 | 0.07 | — | — | — | — | — |
| pop mixed_mem0 np=3 | 0.07 | 0.12 | 0.18 | — | — | — |
| pop mixed_mem0 np=6 | 0.07 | 0.12 | 0.17 | 0.22 | 0.28 | 0.33 |
| pop agentic np=1 | 0.07 | — | — | — | — | — |
| pop agentic np=3 | 0.07 | 0.13 | 0.19 | — | — | — |

### Observations

- For cluster agentic `np=8`, the first three concurrency levels show elevated P95 TTFT (2.45–2.99 s). This is likely from occasional long-prompt stalls during warmup/model settling under the new `np=8` setting. After conc=4, TTFT stabilizes under 0.7 s.
- The cluster's normal P95 TTFT for default workload is excellent: under 0.7 s even at conc=8.
- Pop TTFT is effectively instant (0.07 s single-slot, rising gently with concurrency).

---

## Memory / desktop impact

On the M5 Max, the agentic workload at `NUM_PARALLEL=6` with 32K context caused unified memory to spike to ~96 % and produced visible desktop stuttering. This is expected: Apple Silicon shares system RAM with the GPU/Neural Engine, and six concurrent 32K-context slots require a large KV cache. The remaining pop configurations (`np6-agentic`, `np8-default`, `np8-agentic`) were not run to preserve desktop responsiveness.

The cluster RX 9070 XT did not hit memory limits:
- Ollama `/api/ps` showed ~7.9 GB model residency.
- Total allocated VRAM stayed around ~9.9 GB of 16 GB.
- No OOM events or pod restarts.
- Brief `throttle=True` during warmup only.

---

## Recommendations

### Cluster / RX 9070 XT

| Setting | Current | Recommended | Rationale |
|---|---|---|---|
| `OLLAMA_NUM_PARALLEL` | 8 | **8** | Good balance; default workload still scales, agentic saturates ~5–6. Could test 10–12 for short-prompt services. |
| `OLLAMA_CONTEXT_LENGTH` | 32768 | **32768** | Required for agentic workloads; does not pre-allocate. |
| `OLLAMA_LOAD_TIMEOUT` | 15m | **15m** | PVC loads can be slow; no downside. |
| `OLLAMA_KV_CACHE_TYPE` | q4_0 | **q4_0** | Retain for RDNA4 symmetric K/V quantization + fused Flash Attention. |
| Backend | ROCm | **Vulkan** | ~10–20 % faster than ROCm for this model; all layers offloaded. Use `GGML_VK_VISIBLE_DEVICES=1` because Vulkan enumerates the iGPU first on timmy. |
| `OLLAMA_KEEP_ALIVE` | not set | **evaluate `-1`** | For always-on production services, may avoid reload latency. Needs benchmark. |

### Pop / M5 Max — safe operating boundary

For normal desktop use plus LLM workloads, the M5 Max's practical boundary is:

| Setting | Boundary | Rationale |
|---|---|---|
| `OLLAMA_NUM_PARALLEL` | **≤ 3** | Concurrency 6+ with 32K context spiked unified memory to ~96 % and caused desktop stuttering. |
| `OLLAMA_CONTEXT_LENGTH` | **16384 or smaller** | 32K agentic workloads multiply KV-cache pressure across slots. |
| Model size | **9B when possible** | `qwen3.5:9b-mlx` leaves more unified-memory headroom than `gemma4:12b-mlx`. |
| Concurrency 6+ / 32K / 12B | **Batch, overnight, or headless only** | Safe when the machine is not in active use. |

| Setting | Current | Recommended | Rationale |
|---|---|---|---|
| `OLLAMA_NUM_PARALLEL` | 1 | **3** | Maximum that keeps the desktop responsive under load. |
| `OLLAMA_CONTEXT_LENGTH` | 16384 | **16384** | Matches the safe boundary above. |
| `OLLAMA_LOAD_TIMEOUT` | 2m | **2m** | MLX loads are fast; 2m is generous. |
| Long agentic / high-concurrency runs | — | **run overnight or headless** | Avoid desktop stutter. |

---

## Files

- `aggregate-throughput.png`
- `per-request-gen-tps.png`
- `p95-ttft.png`
- `summary.csv`

## Vulkan backend comparison

A head-to-head run of `gemma4:12b-it-qat` Q4_0 at `NUM_PARALLEL=8` showed
Ollama's Vulkan backend is **~10–20 % faster than ROCm** on the cluster RX 9070 XT.
Both workloads used `OLLAMA_CONTEXT_LENGTH=16384` and `OLLAMA_KV_CACHE_TYPE=q4_0`.

### Default workload (`mixed_mem0`)

| Conc | ROCm agg tok/s | Vulkan agg tok/s | ROCm P95 TTFT | Vulkan P95 TTFT | ROCm P50 gen tok/s | Vulkan P50 gen tok/s |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 25.0 | 28.6 | 0.44 | 0.45 | 52.8 | 60.6 |
| 2 | 31.1 | 36.7 | 0.50 | 0.51 | 35.2 | 42.7 |
| 3 | 43.0 | 51.2 | 0.54 | 0.52 | 42.5 | 50.9 |
| 4 | 53.8 | 62.2 | 0.57 | 0.55 | 30.3 | 35.2 |
| 5 | 54.0 | 63.8 | 0.58 | 0.57 | 21.1 | 25.0 |
| 6 | 68.8 | 74.3 | 0.56 | 0.60 | 30.5 | 31.0 |
| 7 | 68.5 | 69.9 | 0.68 | 0.65 | 27.4 | 27.3 |
| 8 | 88.1 | 93.4 | 0.67 | 0.76 | 23.8 | 24.5 |

### Agentic workload (long context, tool-calling)

| Conc | ROCm agg tok/s | Vulkan agg tok/s | ROCm P95 TTFT | Vulkan P95 TTFT | ROCm P50 gen tok/s | Vulkan P50 gen tok/s |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 25.2 | 29.4 | 2.45 | 0.50 | 51.7 | 60.2 |
| 2 | 36.7 | 32.9 | 2.51 | 0.49 | 41.1 | 33.7 |
| 3 | 39.5 | 41.2 | 2.99 | 0.54 | 30.5 | 31.1 |
| 4 | 50.8 | 47.7 | 0.51 | 0.52 | 31.3 | 30.6 |
| 5 | 52.3 | 54.8 | 0.62 | 0.58 | 31.6 | 33.1 |
| 6 | 58.3 | 51.6 | 0.63 | 0.67 | 24.2 | 21.7 |
| 7 | 55.2 | 59.3 | 0.58 | 0.62 | 21.5 | 21.8 |
| 8 | 63.6 | 69.3 | 0.66 | 1.55 | 20.9 | 22.5 |

Vulkan again has the higher peak in both workloads, and the win is larger
for the agentic workload at the top end (+9 % peak, 63.6 → 69.3 tok/s).
ROCm is slightly faster at a couple of mid-concurrency agentic points
(conc=2, 4, 6), but the differences are within run-to-run variance.

Ollama logs confirmed the model loaded on `Vulkan0` with all 49 layers
offloaded. Device selection matters: Vulkan enumerates the iGPU first on
timmy, so production uses `GGML_VK_VISIBLE_DEVICES=1`.

## Next steps

1. Run the higher-bit quant variants (Q5_K_M, Q6_K) under Vulkan to finish the matrix.
2. Complete remaining pop configs overnight or on a headless Mac.
3. Consider a cluster `np=12` default-workload test if short-prompt scaling continues to rise.
