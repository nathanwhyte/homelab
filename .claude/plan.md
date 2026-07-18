# Plan: Vulkan + higher-bit quant benchmark campaign on cluster RX 9070 XT

## Goal

Compare ROCm vs Vulkan backend performance on timmy's RX 9070 XT, and evaluate whether Q5_K_M / Q6_K Gemma 4 12B QAT variants improve quality enough to justify the size/VRAM cost.

## Work packages

### 1. Finish the existing ROCm NUM_PARALLEL sweep first

- The resume Job has already run `cluster-np6-agentic`, `cluster-np8-default`, `cluster-np8-agentic`.
- Verify completion, pull results to `/Users/noot/code/homelab/benchmarks/results`, and regenerate the report.
- This gives the ROCm baseline before switching backends.

### 2. Create Vulkan benchmark configs (setup now, run later)

Add new TOML configs under `benchmarks/ollama/configs/` that mirror the ROCm configs but use a distinct `prefix` so results don't collide:

- `cluster-vulkan-default.toml` — `gemma4:12b-it-qat`, mixed_mem0, np=8 (matching production)
- `cluster-vulkan-agentic.toml` — `gemma4:12b-it-qat`, agentic, np=8
- `cluster-vulkan-np1-default.toml`, `np3`, `np6`, `np8` default + agentic variants if we want a full sweep.

Also add higher-bit quant configs:

- `cluster-vulkan-q5km-default.toml` — `gemma4:12b-it-qat-q5_K_M` via `hf.co/unsloth/gemma-4-12B-it-qat-GGUF:Q5_K_M`
- `cluster-vulkan-q6k-default.toml` — `gemma4:12b-it-qat-q6_K` via `hf.co/unsloth/gemma-4-12B-it-qat-GGUF:Q6_K`
- (Optionally) `cluster-vulkan-q5km-agentic.toml` and `cluster-vulkan-q6k-agentic.toml` if VRAM allows at 32K context.

### 3. Add a Vulkan-capable Ollama deployment variant

> **SUPERSEDED (2026-07-18):** the separate `llama/ollama-deployment-vulkan.yaml`
> variant was created for this benchmark but later **deleted** — once the OV
> embedder left the 9070 XT (IMPR-1077), the production `llama/ollama-deployment.yaml`
> IS the Vulkan deployment, so the variant was a stale duplicate. Do not recreate it;
> benchmarks now run against the prod deployment (see `run-vulkan-benchmark-jobs.sh`).
> The steps below are retained for historical context only.

Create `llama/ollama-deployment-vulkan.yaml` based on the current ROCm deployment but:

- Use `ollama/ollama:0.31.1` (non-ROCm, Vulkan-enabled) image.
- Remove `HIP_VISIBLE_DEVICES`, `rocblas-library` volume, and ROCm hostPath mounts.
- Add a device-selection guardrail for Vulkan: because Vulkan reverses device order on timmy (per `GPU_AND_AI_REVIEW.md`), the iGPU may be enumerated first. Select the discrete RX 9070 XT via `OLLAMA_GPU_OVERHEAD` or environment-based device filtering (investigate exact Ollama Vulkan device selection; may need `VK_DEVICE` / `MESA_VK_DEVICE_SELECT` or a node-level udev rule).
- Keep `OLLAMA_NUM_PARALLEL=8`, `OLLAMA_CONTEXT_LENGTH=32768`, `OLLAMA_KV_CACHE_TYPE=q4_0`, `OLLAMA_FLASH_ATTENTION=1`, `OLLAMA_KEEP_ALIVE=-1`, `OLLAMA_LOAD_TIMEOUT=15m`.

### 4. Create a benchmark Job manifest for Vulkan

Create `benchmarks/ollama/manifests/benchmark-vulkan-job.yaml`:

- Same python:3.12-slim benchmark sidecar as the ROCm Job.
- Drop ROCm volume mounts and `LD_LIBRARY_PATH`.
- Keep `privileged: true` and `nodeSelector: timmy`.
- No `--amdsmi` (Vulkan path won't have amdsmi); rely on Prometheus GPU metrics and `--no-gpu-sampling`.
- If possible, install `amdgpu_top` or use sysfs for manual GPU sampling; otherwise accept Prometheus-only metrics.

### 5. Benchmark execution sequence

1. Let the existing ROCm resume Job finish and collect results.
2. Apply the Vulkan deployment, ensure `gemma4:12b-it-qat` loads on the discrete GPU.
3. Run the Vulkan default + agentic baseline at np=8.
4. Run Q5_K_M and Q6_K subsets at np=8 default (and agentic if VRAM allows).
5. Optionally run a Vulkan NUM_PARALLEL sweep (np 1,3,6,8) if initial results look promising.
6. Pull results, generate comparison report.

### 6. Metrics to capture

- Aggregate throughput vs concurrency
- P95 TTFT and per-request gen t/s
- GPU utilization, memory usage, power, throttling
- Model load time (ROCm vs Vulkan)
- Quality sanity check: run a small fixed-prompt set through each quant and compare output (not a full eval, just a smoke test for coherence).

### 7. Risk mitigation

- **iGPU selection**: Vulkan may pick the integrated GPU. Verify with `ollama ps` / `nvtop` / `amdgpu_top` that the RX 9070 XT is active. If Ollama lacks a device selector, pin via `MESA_VK_DEVICE_SELECT` or a udev rule.
- **VRAM cliff**: Q5_K_M and Q6_K at 32K context may exceed 16 GB at np=8. Start with 16K context for the quant comparison, or reduce NUM_PARALLEL to 4–6 for the high-bit configs.
- **Model availability**: Confirm `hf.co/unsloth/gemma-4-12B-it-qat-GGUF:Q5_K_M` and `:Q6_K` are reachable from the cluster. Fallback to `google/gemma-4-12B-it-qat-q4_0-gguf` + Unsloth `UD-Q4_K_XL` if specific K_M tags are missing.
- **Production disruption**: The Vulkan deployment replaces the ROCm pod. Roll back to `llama/ollama-deployment.yaml` after benchmarks if Vulkan is slower or unstable.

### 8. Artifacts to commit

- New TOML configs in `benchmarks/ollama/configs/`.
- `llama/ollama-deployment-vulkan.yaml`.
- `benchmarks/ollama/manifests/benchmark-vulkan-job.yaml`.
- Updated `benchmarks/ollama/manifests/rebuild-configmap-np-sweep.sh` to include the new configs (or a separate rebuild script for Vulkan configs).
- Report under `benchmarks/results/report-2026-07-0X/`.

## Decision gates

- Gate 1: Existing ROCm sweep finished and results look sound.
- Gate 2: Vulkan deployment loads `gemma4:12b-it-qat` on the RX 9070 XT (not iGPU) and serves requests.
- Gate 3: Q5_K_M / Q6_K models fit in 16 GB at the chosen NUM_PARALLEL/context.

## Recommended first step now

Create the Vulkan TOML configs and the Vulkan deployment/Job manifests while the ROCm resume Job is finishing. Do not apply the Vulkan deployment until the ROCm baseline is complete.
