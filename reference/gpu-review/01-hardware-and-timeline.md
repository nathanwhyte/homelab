# GPU & AI Infrastructure Review — Hardware & Setup Timeline

Split from `GPU_AND_AI_REVIEW.md` (compiled 2026-05-01) into per-topic files
in `reference/gpu-review/`. This file covers the **what was** cluster + GPU
hardware and the chronological setup timeline (Jan–May 2026).

For current cluster state, see `CLAUDE.md` (service routing, LLM config, topology)
and `HARDWARE.md` (node specs). The original 619-line document is retained at
`GPU_AND_AI_REVIEW.md` for now.

## 1. Hardware Inventory

### Cluster Overview

3-node K3s cluster. 36 threads, ~64 GB RAM, mixed storage, 30 GB total VRAM across 3 GPUs.

| Node      | Role           | CPU                          | RAM   | Storage                         | GPU                              | VRAM        |
| --------- | -------------- | ---------------------------- | ----- | ------------------------------- | -------------------------------- | ----------- |
| **timmy** | Control+Worker | AMD Ryzen 7 7800X3D (8C/16T) | 32 GB | WD Green SN3000 2TB NVMe        | AMD RX 9070 XT (RDNA 4, gfx1201) | 16 GB GDDR6 |
| **manu**  | Worker         | AMD Ryzen 7 1700 (8C/16T)    | 16 GB | 2x Samsung 860 EVO 1TB SATA SSD | NVIDIA GTX 1080 (Pascal, SM 6.1) | 8 GB GDDR5X  |
| **wemby** | Worker         | Intel i7-8750H (6C/12T)      | 16 GB | WDC SN520 256GB NVMe + 1TB HDD  | NVIDIA GTX 1060                  | 6 GB        |

**Retired nodes:** patty (i5-7200U, 8 GB) and steph (i5-10210U, 12 GB) removed from cluster.

### GPU Details

| GPU        | Node  | Driver Stack                                    | Device Plugin                                     | Monitoring                                                | Known Quirks                                                                                                 |
| ---------- | ----- | ----------------------------------------------- | ------------------------------------------------- | --------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| RX 9070 XT | timmy | AMDGPU 7.2.70200, ROCm 7.2, Kernel 6.17.0-23    | `amd.com/gpu: "1"` (no runtimeClassName needed)   | Custom amdgpu-exporter DaemonSet (sysfs-based, port 9101) | iGPU (PCI 1002:164E) must be filtered: `HIP_VISIBLE_DEVICES=0`. Vulkan reverses device order.                |
| GTX 1080   | manu  | NVIDIA GPU Operator (DCGM Exporter 4.4.2-4.7.0) | `nvidia.com/gpu: "1"`, `runtimeClassName: nvidia` | DCGM Exporter → Prometheus                                | Memory clock BIOS-locked at 4513 MHz (vs 5005 max). `nvidia-smi -ac` not supported on GeForce.               |
| GTX 1060   | wemby | NVIDIA GPU Operator                             | `nvidia.com/gpu: "1"`, `runtimeClassName: nvidia` | DCGM Exporter                                             | Only 6 GB VRAM. Runs embedder-llamacpp (nomic-embed-text-v1.5, CUDA — moved from timmy in IDEA-009 Phase 2). |

---

## 2. GPU Setup Timeline

### Phase 1: Initial NVIDIA Setup (Jan-Feb 2026)

| Date       | Commit    | Event                                                               |
| ---------- | --------- | ------------------------------------------------------------------- |
| 2026-01-27 | `4add1fc` | Remove unused GPU metrics collector spec (early GPU exploration)    |
| 2026-02-25 | `60e782a` | Add gpu-operator values and test (NVIDIA GPU Operator installed)    |
| 2026-02-25 | `88972e4` | Update AGENTS.md with GPU info                                      |
| 2026-02-26 | `6c69372` | First LLM deployment: small model via llama.cpp on NVIDIA           |
| 2026-02-26 | `e4d86c7` | Delete benchmark script (not working -- early benchmarking attempt) |
| 2026-02-27 | `ae3af62` | Add memory service spec (vector store experiment)                   |

### Phase 2: AMD GPU Integration (Mar 2026)

| Date       | Commit     | Event                                                                         |
| ---------- | ---------- | ----------------------------------------------------------------------------- |
| 2026-03-13 | (research) | Research spike: AMD 9070 XT integration, mixed GPU cluster, nanochat training |
| 2026-03-15 | `3106a5f`  | Add AMD GPU specs to cluster                                                  |
| 2026-03-15 | `4cf00db`  | Add ROCm specs for running models on AMD card                                 |
| 2026-03-15 | `36371f1`  | Add nanochat training specs                                                   |
| 2026-03-18 | `b7fa309`  | Fix nanochat ROCm training for single GPU with v3 image                       |
| 2026-03-18 | `27d8692`  | Add Qwen3 14B summarizer stack                                                |
| 2026-03-18 | `6b259d8`  | Add viking namespace for OpenViking RAG service                               |

### Phase 3: GPU Monitoring & Tuning (Mar 2026)

| Date       | Commit    | Event                                                                    |
| ---------- | --------- | ------------------------------------------------------------------------ |
| 2026-03-20 | `00dfea4` | Add AMD GPU metrics exporter + fix NVIDIA DCGM scraping                  |
| 2026-03-20 | `3eb4a3e` | Add fixed NVIDIA DCGM dashboard (legend, units, series fixes)            |
| 2026-03-20 | `eba5d46` | Add combined GPU Overview dashboard (all 3 cluster GPUs)                 |
| 2026-03-23 | `a9dca57` | GPU fan control configs: LACT (active), amdgpu-fan, bash fallback        |
| 2026-03-23 | `b73639c` | Add 340W power cap via LACT (was 304W default -- card was power-starved) |
| 2026-03-23 | `9ff49fd` | Consolidate GPU tuning into LACT: COMPUTE profile, aggressive fan curve  |
| 2026-03-23 | `38f6aca` | Add GPU profile toggle script for timmy                                  |
| 2026-03-23 | `7aef331` | Add junction temp metric to AMD GPU exporter                             |

### Phase 4: Ollama Integration (Mar-Apr 2026)

| Date       | Commit      | Event                                                                                                  |
| ---------- | ----------- | ------------------------------------------------------------------------------------------------------ |
| 2026-03-26 | `a1ad9a2`   | Add Ollama setup script for timmy with optimized settings                                              |
| 2026-03-26 | `a77fc30`   | Add OLLAMA_KV_CACHE_TYPE=q8_0 to Ollama setup                                                          |
| 2026-03-26 | `de8475d`   | Add Ollama Prometheus exporter and monitoring                                                          |
| 2026-03-27 | `0181673`   | Ollama K8s deployment design spec                                                                      |
| 2026-03-27 | `5c20b56`   | Ollama ConfigMap with Modelfiles and startup scripts                                                   |
| 2026-03-27 | `1e4d5a6`   | Ollama Deployment with exporter sidecar and ClusterIP Service                                          |
| 2026-03-27 | `624f751`   | Traefik Ingress for Ollama (robots.nathanwhyte.dev, BasicAuth)                                         |
| 2026-03-29 | `6e46cc5`   | Replace Traefik ingress with Cloudflare Tunnel for Ollama                                              |
| 2026-04-09 | `3044-3045` | Added `imagePullPolicy: Always` to force Ollama image updates; gemma4:e4b added to modelfiles          |
| 2026-04-10 | `4913498`   | Dual-model loading enabled; Prometheus scrape target moved from bare-metal to K8s service              |
| 2026-04-11 | `3063-3065` | LACT daemon deployed on timmy; fan curve tuned for aggressive cooling; thermal throttling resolved     |
| 2026-04-11 | `3076`      | GPU fan curve adjusted to max at 85°C (GPU reached 94°C); replica count set to 1 for llama-model-cache |
| 2026-04-27 | `3100`      | Removed smaller devstral models from Ollama ConfigMap                                                  |
| 2026-04-27 | `3103-3104` | Pruned ~80 GB of stale Ollama models (devstral, legacy) from PVC                                       |
| 2026-04-30 | `3116-3118` | SearXNG search service added; ConfigMap patched to fix CrashLoopBackOff                                |

### Phase 5: Infrastructure Consolidation (Apr-May 2026)

| Date       | Commit      | Event                                                                         |
| ---------- | ----------- | ----------------------------------------------------------------------------- |
| 2026-04-30 | `3119`      | Investigated combining cloudflared/ and cloudflare-system/ directories        |
| 2026-05-01 | `3122-3124` | Consolidated cloudflare tunnel manifests into unified `cloudflare/` directory |

---

## Source

Extracted from `GPU_AND_AI_REVIEW.md` lines 22–110 (sections 1 + 2). For current
state, see the files noted in the header.
