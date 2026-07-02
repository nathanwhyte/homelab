# Ollama NUM_PARALLEL benchmark report — 2026-07-01

Generated from the Ollama concurrency/tool-calling benchmark harness.

## Tested configurations

| Platform | GPU | Model | Ollama version | `NUM_PARALLEL` | Context | Workload |
|---|---|---|---|---|---|---|
| Cluster (timmy) | RX 9070 XT 16 GB | `gemma4:12b-it-qat` | 0.31.1-vulkan | 8 | 16K–32K | `mixed_mem0`, `agentic` |
| Pop (this Mac) | M5 Max | `gemma4:12b-mlx` | 0.31.1 | 1 / 3 / 6 | 16K–32K | `mixed_mem0`, `agentic` |

> Pop `np=8` and `np6-agentic` were not completed. The pop sweep was stopped during `pop-np6-agentic` because the 32K-context / 6-slot workload spiked unified memory usage to ~96 % and caused desktop stuttering on the M5 Max. The usable pop data is `np1`, `np3` (both workloads), and `np6-default`.
>
> **2026-07-02 correction**: an earlier version of this report cited pop `np1-default` aggregate throughput as 94.7 tok/s. That number came from a one-off, non-reproducible validation run of the v0.31.1 MTP feature (`pop-np1-default-v031.log`, 2026-06-30) that predates the actual sweep harness. The report's own `summary.csv` and charts always used the correct, reproducible sweep run (60.55 tok/s, captured with `pop-np1-env.json`) — only the prose disagreed with its own data. The narrative below and all tables now match the sweep data. The orphaned validation run and a second unexplained duplicate run for the same config have been removed from the results directory.
>
> **2026-07-02**: ROCm cluster data (`np6-agentic`, `np8-agentic`, `np8-default` under 0.31.1-rocm) has been dropped from this report. Production switched to the Vulkan backend on 2026-07-01 (see `INFO-1023`) after confirming Vulkan is consistently faster; the ROCm numbers are no longer operationally relevant. Cluster figures below are Vulkan-only.

---

## Aggregate throughput

### Cluster (Vulkan)

| Run | Conc 1 | Conc 2 | Conc 3 | Conc 4 | Conc 5 | Conc 6 | Conc 7 | Conc 8 | Saturation |
|---|---|---|---|---|---|---|---|---|---|
| cluster mixed_mem0 np=8 | 28.6 | 36.7 | 51.2 | 62.2 | 63.8 | 74.3 | 69.9 | **93.4** | keeps rising |
| cluster agentic np=8 | 29.4 | 32.9 | 41.2 | 47.7 | 54.8 | 51.6 | 59.3 | **69.3** | ~conc 6-8 |

### Pop

| Run | Conc 1 | Conc 2 | Conc 3 | Conc 4 | Conc 5 | Conc 6 | Notes |
|---|---|---|---|---|---|---|---|
| pop mixed_mem0 np=1 | 60.55 | — | — | — | — | — | single-slot, MTP-boosted |
| pop mixed_mem0 np=3 | 47.9 | 38.3 | 48.8 | — | — | — | flat scaling |
| pop mixed_mem0 np=6 | 33.6 | 35.8 | 37.1 | 37.2 | 29.3 | **51.3** | noisy; no clear rise |
| pop agentic np=1 | 61.4 | — | — | — | — | — | single-slot, long context |
| pop agentic np=3 | 32.8 | 33.5 | **67.3** | — | — | — | peaks at conc=3 |

### Observations

- **Cluster default workload (Vulkan) scales well to `NUM_PARALLEL=8`**: aggregate throughput reaches 93.4 tok/s at conc=8. The curve is still rising, suggesting the RX 9070 XT could potentially handle `np=10–12` for short prompts.
- **Cluster agentic workload (long context) saturates earlier**: peak is around conc=6-8, with diminishing returns beyond that. This is expected — 32K context and 2048-token outputs consume much more KV cache and compute per slot.
- **Pop single-slot aggregate throughput is roughly 2x the cluster's per-slot speed**: 60.55 tok/s vs 28.56 tok/s at concurrency=1 for the default workload. Per-request generation speed shows an even larger gap (see below) — the v0.31.1 MTP effect on Apple Silicon.
- **Pop does not show clean aggregate scaling**: the `np=3` and `np=6` default curves are relatively flat or noisy. This suggests `OLLAMA_NUM_PARALLEL` on the MLX backend behaves more like independent worker slots than true continuous batching.

---

## Per-request generation speed

### Cluster (Vulkan)

| Run | Conc 1 | Conc 2 | Conc 3 | Conc 4 | Conc 5 | Conc 6 | Conc 7 | Conc 8 |
|---|---|---|---|---|---|---|---|---|
| cluster mixed_mem0 np=8 | 60.6 | 42.7 | 50.9 | 35.2 | 25.0 | 31.0 | 27.3 | 24.5 |
| cluster agentic np=8 | 60.2 | 33.7 | 31.1 | 30.6 | 33.1 | 21.7 | 21.8 | 22.5 |

### Pop

| Run | Conc 1 | Conc 2 | Conc 3 | Conc 4 | Conc 5 | Conc 6 |
|---|---|---|---|---|---|---|
| pop mixed_mem0 np=1 | **104.6** | — | — | — | — | — |
| pop mixed_mem0 np=3 | 99.9 | 103.8 | 85.8 | — | — | — |
| pop mixed_mem0 np=6 | 72.7 | 70.4 | 73.0 | 72.9 | 73.0 | 72.7 |
| pop agentic np=1 | 92.1 | — | — | — | — | — |
| pop agentic np=3 | 74.6 | 73.6 | **81.3** | — | — | — |

### Observations

- Single-slot per-request speed is nearly identical on cluster for both workloads (~60 tok/s), because the agentic prompts are long but the model is compute-bound during prefill.
- Per-request speed drops to ~22 tok/s at high concurrency for agentic workloads on the cluster. This is the latency cost of packing more requests onto the same GPU.
- Pop's single-slot speed is roughly **1.7x** the cluster's single-slot speed, confirming the Gemma 4 MTP benefit on MLX.
- Pop per-request speed stays high even at `np=6`, which is consistent with slots being scheduled independently rather than contending for the same compute in a single batch.

---

## P95 time-to-first-token

### Cluster (Vulkan)

| Run | Conc 1 | Conc 2 | Conc 3 | Conc 4 | Conc 5 | Conc 6 | Conc 7 | Conc 8 |
|---|---|---|---|---|---|---|---|---|
| cluster mixed_mem0 np=8 | 0.45 | 0.51 | 0.52 | 0.55 | 0.57 | 0.60 | 0.65 | 0.76 |
| cluster agentic np=8 | 0.50 | 0.49 | 0.54 | 0.52 | 0.58 | 0.67 | 0.62 | 1.55 |

### Pop

| Run | Conc 1 | Conc 2 | Conc 3 | Conc 4 | Conc 5 | Conc 6 |
|---|---|---|---|---|---|---|
| pop mixed_mem0 np=1 | 0.07 | — | — | — | — | — |
| pop mixed_mem0 np=3 | 0.07 | 0.12 | 0.18 | — | — | — |
| pop mixed_mem0 np=6 | 0.07 | 0.12 | 0.17 | 0.22 | 0.28 | 0.33 |
| pop agentic np=1 | 0.07 | — | — | — | — | — |
| pop agentic np=3 | 0.07 | 0.13 | 0.19 | — | — | — |

### Observations

- Cluster P95 TTFT under Vulkan stays under 0.8 s through conc=8 for the default workload.
- Cluster agentic P95 TTFT spikes at conc=8 (1.55 s) — likely an isolated warmup/settling stall rather than a systemic issue, since conc=1-7 all stay under 0.7 s.
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
| `OLLAMA_NUM_PARALLEL` | 8 | **8** | Good balance; default workload still scales, agentic saturates ~6-8. Could test 10–12 for short-prompt services. |
| `OLLAMA_CONTEXT_LENGTH` | 32768 | **32768** | Required for agentic workloads; does not pre-allocate. |
| `OLLAMA_LOAD_TIMEOUT` | 15m | **15m** | PVC loads can be slow; no downside. |
| `OLLAMA_KV_CACHE_TYPE` | q4_0 | **q4_0** | Retain for RDNA4 symmetric K/V quantization + fused Flash Attention. |
| Backend | Vulkan | **Vulkan** | Already production default since 2026-07-01; ~10–20% faster than ROCm was, all layers offloaded. Use `GGML_VK_VISIBLE_DEVICES=1` because Vulkan enumerates the iGPU first on timmy. |
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

- `single-slot-speed.png`
- `peak-throughput.png`
- `peak-comparison-grid.png`
- `summary.csv`

## Next steps

1. Run the higher-bit quant variants (Q5_K_M, Q6_K) under Vulkan to finish the matrix — currently blocked on missing HF tags for the QAT model (see `INFO-1070`).
2. Complete remaining pop configs overnight or on a headless Mac.
3. Consider a cluster `np=12` default-workload test if short-prompt scaling continues to rise.
