# GPU & AI Infrastructure Review

> **⚠️ Historical document** — compiled 2026-05-01. The sections below ("Current Architecture", "Running Workloads", "Service Routing", "GPU Allocation", and "AI Tools Deployed") reflect the cluster state as of that date and have diverged significantly. For current state, see `CLAUDE.md` (service routing, LLM config, topology) and `HARDWARE.md` (node specs).
>
> **Key changes since 2026-05-01 (IDEA-009 Phases 2-4):**
> - Embedder moved from timmy (CPU) to wemby (GTX 1060, CUDA) to free the 1080 for VLM-exclusive use
> - ROCm VLM (`llamacpp-rocm`) retired 2026-06-06; VLM is now `llamacpp-cuda-ov` on manu exclusively
> - OV coordinator, merge, workers, and console removed from cluster (not just scaled to 0)
> - Ollama model lineup changed: gemma4:12b-it-qat (local), glm-5.1:cloud (remote); keep-alive 30m; NUM_PARALLEL=1
> - Hermes Agent deployed (hermes namespace) with OpenViking memory provider
> - Tailscale mesh deployed on all 3 nodes
> - VLM steady-state changed from on-demand scaling to always-on (replicas=1)
> - VLM parallel slots reduced from 4 to 2; `vlm.max_concurrent` reduced from 4 to 2
>
> The benchmarks (§4), tuning history (§5), and design decisions (§8) remain valid as historical reference.

Comprehensive review of all GPU setup, model benchmarks, and AI tools work in the homelab K3s cluster. Compiled 2026-05-01.

---

## 1. Hardware Inventory

### Cluster Overview

3-node K3s cluster. 36 threads, ~64 GB RAM, mixed storage, 30 GB total VRAM across 3 GPUs.

| Node | Role | CPU | RAM | Storage | GPU | VRAM |
|------|------|-----|-----|---------|-----|------|
| **timmy** | Control+Worker | AMD Ryzen 7 7800X3D (8C/16T) | 32 GB | WD Green SN3000 2TB NVMe | AMD RX 9070 XT (RDNA 4, gfx1201) | 16 GB GDDR6 |
| **manu** | Worker | AMD Ryzen 7 1700 (8C/16T) | 16 GB | 2x Samsung 860 EVO 1TB SATA SSD | NVIDIA GTX 1080 (Pascal, SM 6.1) | 8 GB GDDR5X |
| **wemby** | Worker | Intel i7-8750H (6C/12T) | 16 GB | WDC SN520 256GB NVMe + 1TB HDD | NVIDIA GTX 1060 | 6 GB |

**Retired nodes:** patty (i5-7200U, 8 GB) and steph (i5-10210U, 12 GB) removed from cluster.

### GPU Details

| GPU | Node | Driver Stack | Device Plugin | Monitoring | Known Quirks |
|-----|------|-------------|---------------|------------|-------------|
| RX 9070 XT | timmy | AMDGPU 7.2.70200, ROCm 7.2, Kernel 6.17.0-23 | `amd.com/gpu: "1"` (no runtimeClassName needed) | Custom amdgpu-exporter DaemonSet (sysfs-based, port 9101) | iGPU (PCI 1002:164E) must be filtered: `HIP_VISIBLE_DEVICES=0`. Vulkan reverses device order. |
| GTX 1080 | manu | NVIDIA GPU Operator (DCGM Exporter 4.4.2-4.7.0) | `nvidia.com/gpu: "1"`, `runtimeClassName: nvidia` | DCGM Exporter → Prometheus | Memory clock BIOS-locked at 4513 MHz (vs 5005 max). `nvidia-smi -ac` not supported on GeForce. |
| GTX 1060 | wemby | NVIDIA GPU Operator | `nvidia.com/gpu: "1"`, `runtimeClassName: nvidia` | DCGM Exporter | Only 6 GB VRAM. Not currently used for inference. |

---

## 2. GPU Setup Timeline

### Phase 1: Initial NVIDIA Setup (Jan-Feb 2026)

| Date | Commit | Event |
|------|--------|-------|
| 2026-01-27 | `4add1fc` | Remove unused GPU metrics collector spec (early GPU exploration) |
| 2026-02-25 | `60e782a` | Add gpu-operator values and test (NVIDIA GPU Operator installed) |
| 2026-02-25 | `88972e4` | Update AGENTS.md with GPU info |
| 2026-02-26 | `6c69372` | First LLM deployment: small model via llama.cpp on NVIDIA |
| 2026-02-26 | `e4d86c7` | Delete benchmark script (not working -- early benchmarking attempt) |
| 2026-02-27 | `ae3af62` | Add memory service spec (vector store experiment) |

### Phase 2: AMD GPU Integration (Mar 2026)

| Date | Commit | Event |
|------|--------|-------|
| 2026-03-13 | (research) | Research spike: AMD 9070 XT integration, mixed GPU cluster, nanochat training |
| 2026-03-15 | `3106a5f` | Add AMD GPU specs to cluster |
| 2026-03-15 | `4cf00db` | Add ROCm specs for running models on AMD card |
| 2026-03-15 | `36371f1` | Add nanochat training specs |
| 2026-03-18 | `b7fa309` | Fix nanochat ROCm training for single GPU with v3 image |
| 2026-03-18 | `27d8692` | Add Qwen3 14B summarizer stack |
| 2026-03-18 | `6b259d8` | Add viking namespace for OpenViking RAG service |

### Phase 3: GPU Monitoring & Tuning (Mar 2026)

| Date | Commit | Event |
|------|--------|-------|
| 2026-03-20 | `00dfea4` | Add AMD GPU metrics exporter + fix NVIDIA DCGM scraping |
| 2026-03-20 | `3eb4a3e` | Add fixed NVIDIA DCGM dashboard (legend, units, series fixes) |
| 2026-03-20 | `eba5d46` | Add combined GPU Overview dashboard (all 3 cluster GPUs) |
| 2026-03-23 | `a9dca57` | GPU fan control configs: LACT (active), amdgpu-fan, bash fallback |
| 2026-03-23 | `b73639c` | Add 340W power cap via LACT (was 304W default -- card was power-starved) |
| 2026-03-23 | `9ff49fd` | Consolidate GPU tuning into LACT: COMPUTE profile, aggressive fan curve |
| 2026-03-23 | `38f6aca` | Add GPU profile toggle script for timmy |
| 2026-03-23 | `7aef331` | Add junction temp metric to AMD GPU exporter |

### Phase 4: Ollama Integration (Mar-Apr 2026)

| Date | Commit | Event |
|------|--------|-------|
| 2026-03-26 | `a1ad9a2` | Add Ollama setup script for timmy with optimized settings |
| 2026-03-26 | `a77fc30` | Add OLLAMA_KV_CACHE_TYPE=q8_0 to Ollama setup |
| 2026-03-26 | `de8475d` | Add Ollama Prometheus exporter and monitoring |
| 2026-03-27 | `0181673` | Ollama K8s deployment design spec |
| 2026-03-27 | `5c20b56` | Ollama ConfigMap with Modelfiles and startup scripts |
| 2026-03-27 | `1e4d5a6` | Ollama Deployment with exporter sidecar and ClusterIP Service |
| 2026-03-27 | `624f751` | Traefik Ingress for Ollama (robots.nathanwhyte.dev, BasicAuth) |
| 2026-03-29 | `6e46cc5` | Replace Traefik ingress with Cloudflare Tunnel for Ollama |
| 2026-04-09 | `3044-3045` | Added `imagePullPolicy: Always` to force Ollama image updates; gemma4:e4b added to modelfiles |
| 2026-04-10 | `4913498` | Dual-model loading enabled; Prometheus scrape target moved from bare-metal to K8s service |
| 2026-04-11 | `3063-3065` | LACT daemon deployed on timmy; fan curve tuned for aggressive cooling; thermal throttling resolved |
| 2026-04-11 | `3076` | GPU fan curve adjusted to max at 85°C (GPU reached 94°C); replica count set to 1 for llama-model-cache |
| 2026-04-27 | `3100` | Removed smaller devstral models from Ollama ConfigMap |
| 2026-04-27 | `3103-3104` | Pruned ~80 GB of stale Ollama models (devstral, legacy) from PVC |
| 2026-04-30 | `3116-3118` | SearXNG search service added; ConfigMap patched to fix CrashLoopBackOff |

### Phase 5: Infrastructure Consolidation (Apr-May 2026)

| Date | Commit | Event |
|------|--------|-------|
| 2026-04-30 | `3119` | Investigated combining cloudflared/ and cloudflare-system/ directories |
| 2026-05-01 | `3122-3124` | Consolidated cloudflare tunnel manifests into unified `cloudflare/` directory |

---

## 3. LLM Infrastructure Evolution

### Stage 1: Single NVIDIA LLM (Feb 2026)
- First llama.cpp deployment on wemby/manu with small model
- Memory service (vector store) added alongside
- Basic chat runner scripts

### Stage 2: AMD ROCm + Dual GPU (Mar 15-18)
- timmy's RX 9070 XT added with ROCm support
- ROCm llama.cpp deployment for LLM inference
- Nanochat training pipeline on ROCm (Dockerfile.rocm, train-rocm-job.yaml)
- Qwen3 14B summarizer stack deployed

### Stage 3: OpenViking RAG Service (Mar 18-20)
- OpenViking v0.2.9 deployed in viking namespace
- VLM backend initially pointed at llamacpp-rocm on timmy
- Embedder (nomic-embed-text-v1.5) initially GPU-accelerated, later migrated to CPU-only
- OpenViking MCP server created for Claude Code integration

### Stage 4: Model Benchmarking & Selection (Mar 20-21)
- Summarization showdown: Mistral-Small-24B vs Qwen3.5-27B vs Claude Haiku vs Qwen3-8B
- Agentic benchmark suite: 10-test tool-calling evaluation
- Three-model comparison report generated
- **Decision: Qwen3-8B Q4_K_M selected** -- matched quality of 24B models at 3x less VRAM

### Stage 5: Split GPU Architecture (Mar 21-23)
- LLM split to dedicated GPUs: timmy for OV, manu for summarizer-api
- llamacpp-rocm scaled to 0 on timmy, service redirected to CUDA on manu (`llamacpp-cuda-ov`)
- Qwen-summarizer moved from ROCm (timmy) to CUDA (manu)
- Summarizer-api code baked into container image (removed configmap)
- Parallel slots tuned: 8→4 on timmy, then 4 slots on manu

### Stage 6: OpenViking Scaling (Mar 22-25)
- AGFS migrated from local storage to Garage S3 backend
- OpenViking scaled to 2 workers (StatefulSet with parallel pod management)
- Coordinator proxy added for parallel indexing
- Merge service added for unified reads across workers
- Consolidation job for correctness checking
- S3 credentials moved inline (v0.2.9 limitation)

### Stage 7: Ollama for Claude Code (Mar 26-29)
- Ollama confirmed working on RDNA 4 (timmy's 9070 XT)
- Deployed as K8s Deployment with `ollama/ollama:rocm` image
- Ollama exporter sidecar for Prometheus monitoring
- Model: qwen3.5:9b-q4_K_M (via Modelfile), later devstral-small-2:24b explored
- External access via Cloudflare Tunnel (robots.nathanwhyte.dev)
- Open WebUI deployed alongside in openwebui namespace

### Stage 8: LACT, Dual-Model, and Thermal Tuning (Apr 9-11)
- `imagePullPolicy: Always` added to force Ollama image updates
- gemma4:e4b added to modelfiles; dual-model loading enabled
- LACT daemon deployed on timmy to replace amdgpu-fan-control (sysfs inaccessible over SSH)
- GPU thermal throttling resolved: fan curve adjusted to max at 85°C (GPU was reaching 94°C)
- llama-model-cache replica count reduced to 1 (Longhorn optimization)

### Stage 9: Model Refresh & Cleanup (Apr 27-30)
- Removed devstral models (devstral-4k, devstral-small-2-4k, devstral-small-2-32k) from startup script
- Pruned ~80 GB of stale models from Ollama PVC
- Added qwen3.5, qwen3.5-128k (131072 ctx), starcoder2, codestral to model lineup
- SearXNG search service deployed for OpenWebUI web search integration

### Current Architecture (2026-05-01)

```
                    ┌──────────────────────────────────────┐
                    │          timmy (RX 9070 XT)          │
                    │                                      │
                    │  ┌─ Ollama (llama ns) ────────────┐  │
                    │  │ gemma4:e4b (warm), qwen3.5,    │  │
                    │  │ mistral-nemo, starcoder2,      │  │
                    │  │ codestral (available)          │  │
                    │  │ flash-attn, q4_0 KV, keep-alive │  │
                    │  │ + Ollama Exporter sidecar       │  │
                    │  │ + Auth Proxy (nginx)            │  │
                    │  └────────────────────────────────┘  │
                    │                                      │
                    │  ┌─ Embedder (viking ns) ──────────┐  │
                    │  │ nomic-embed-text-v1.5 f16      │  │
                    │  │ CPU-only, 768-dim, ctx 16384    │  │
                    │  └─────────────────────────────────┘  │
                    └──────────────────────────────────────┘

                    ┌──────────────────────────────────────┐
                    │           manu (GTX 1080)            │
                    │                                      │
                    │  ┌─ llamacpp-cuda-ov (viking ns) ──┐ │
                    │  │ Qwen3-8B Q4_K_M                 │ │
                    │  │ 4 slots, 32768 ctx, q4_0 KV     │ │
                    │  │ flash-attn, batch 2048           │ │
                    │  │ Power limit: 220W via nvidia-smi │ │
                    │  └─────────────────────────────────┘ │
                    │                                      │
                    │  ┌─ Open WebUI (openwebui ns) ──────┐ │
                    │  │ StatefulSet, single replica      │ │
                    │  └─────────────────────────────────┘ │
                    │                                      │
                    │  ┌─ SearXNG (searxng ns) ──────────┐ │
                    │  │ Search aggregator for WebUI      │ │
                    │  └─────────────────────────────────┘ │
                    └──────────────────────────────────────┘

                    ┌──────────────────────────────────────┐
                    │         wemby (GTX 1060)             │
                    │                                      │
                    │  ┌─ OpenViking (viking ns) ─────────┐│
                    │  │ v0.2.9, AGFS on Garage S3       ││
                    │  │ 3 workers (StatefulSet)          ││
                    │  │ Coordinator + Merge service      ││
                    │  │ Console web UI                   ││
                    │  └──────────────────────────────────┘│
                    └──────────────────────────────────────┘
```

---

## 4. Model Benchmarks

### 4.1 Summarization Showdown (Mar 20-21)

Evaluated models for OpenViking VLM tasks (summarize code, README, config, etc.).

**Quality Scores (1-10 avg of Accuracy/Conciseness/Structure/Completeness):**

| Config | Model | Hardware | Avg Quality | Reliability | Avg Time | tok/s |
|--------|-------|----------|-------------|-------------|----------|-------|
| **A** | Mistral-Small-24B Q4_K_M | timmy (9070 XT) | **8.1** | 5/5 | 36.0s | ~18 |
| **B** | Qwen3.5-27B Q3_K_M /no_think | timmy (9070 XT) | **3.8** | 2/5 | 107.4s | ~10 |
| **D** | Claude Haiku 4.5 | API | **6.1** | 3/5 | 16.2s | -- |
| **E** | Qwen3-8B Q4_K_M | timmy (9070 XT) | **8.1** | 5/5 | 16.2s | ~25 |
| **F** | Qwen3-8B opt (parallel=8) | timmy (9070 XT) | **8.1** | 5/5 | faster | ~33 |
| **G** | Qwen3-8B opt+ (manu GTX 1080) | manu (GTX 1080) | **8.1** | 5/5 | faster | ~33 |

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
|------------|-------------------|-------------|------------------|
| Easy | 7.0 | 8.0 | 8.0 |
| Moderate | 6.0 | 9.0 | 9.0 |
| Hard | 6.5 | 8.5 | 9.0 |
| Expert | 5.0 | 7.5 | 8.5 |

**Verdict:** Mistral degrades sharply on expert tasks (5.0). Qwen and Claude maintain quality. Claude is most consistent. For batch/async workloads, Qwen3.5-27B with thinking enabled approaches Claude quality. For interactive use, Mistral is fastest but lowest quality.

### 4.3 Agentic Benchmark (Mar 21-22)

10-test suite for tool-calling capability with Qwen3-8B on RX 9070 XT.

| Test | Category | Result | Time | Tokens | Speed |
|------|----------|--------|------|--------|-------|
| 1. Single Tool Call | Tool Calling | PASS | 694ms | 29 | 41.8 t/s |
| 2. Multi-Step Tool Plan | Tool Calling | PASS | 6983ms | 347 | 49.7 t/s |
| 3. Tool-Observation Loop | Tool Calling | PASS | 4185ms | 203 | 48.5 t/s |
| 4. Function Generation | Code Gen | PASS | 7469ms | 311 | 41.6 t/s |
| 5. Bug Fix | Code Gen | PASS | -- | -- | -- |
| 7. JSON Schema Output | Structured | PASS | -- | -- | -- |
| 8. Diff Generation | Structured | PASS | 1518ms | 128 | 84.3 t/s |
| 9. Architecture Reasoning | Reasoning | PASS | -- | -- | -- |
| 10. Error Root Cause | Reasoning | PASS | -- | -- | -- |

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

| Scenario | Gen tok/s (per req) | Aggregate tok/s |
|----------|---------------------|-----------------|
| 1 concurrent | 28.9 | 28.9 |
| 2 concurrent | 28.3 | 56.6 |
| 4 concurrent | 20.6 | 82.5 |

- OV real-world avg: 22.4 gen tok/s, 333 prompt tok/s
- Average busy slots: 2.55, requests deferred: 0
- **Increasing parallel slots from 2 to 4 was the biggest win** -- eliminated queuing for OV's 3 concurrent workers

---

## 5. Performance Tuning

### 5.1 GPU Power & Thermal Tuning

#### timmy (RX 9070 XT)

| Setting | Before | After | Impact |
|---------|--------|-------|--------|
| Power cap | 304W (default) | 340W (LACT) | Card was power-starved; 340W unlocked full performance |
| GPU profile | Default | COMPUTE (rocm-smi) | Dedicated compute mode |
| Fan curve | Default | Aggressive (LACT) | Junction temp stays ~88C under load |
| Tuning stack | 3 separate services | LACT consolidated | Single service manages power, profile, fan curve |

**Caveat:** COMPUTE profile and 340W must be re-verified after restarts.

#### manu (GTX 1080)

| Setting | Before | After | Impact |
|---------|--------|-------|--------|
| Power limit | 198W (default) | 220W (nvidia-smi -pl 220 in initContainer) | GPU boosts to 1898-1923 MHz |
| Power limit 238W | 220W | 238W | **No improvement** -- memory clock stays at 4513 MHz |
| Memory clock | 4513 MHz (BIOS-locked) | Cannot change | #1 bottleneck: effective bandwidth ~288 GB/s vs 320 GB/s theoretical |
| Flash attention | auto | explicit on | ~0% difference (auto was already enabling it) |

### 5.2 LLM Server Tuning

#### Parallel Slots

| Config | Slots | Ctx/Slot | Impact |
|--------|-------|----------|--------|
| Initial | 8 | 65536 total | Underutilized -- too many slots with huge context |
| Reduced | 4 | 8192/slot | Better utilization but slot contention |
| Split arch (manu) | 4 | 8192/slot | Zero queuing for OV's 3 workers + 32% higher throughput |

#### KV Cache Quantization

| KV Precision | Memory per 8192 ctx | Impact |
|-------------|---------------------|--------|
| f16 (default) | ~4 GB | Too large for constrained VRAM |
| q8_0 | ~2 GB | Good quality/memory tradeoff |
| q4_0 | ~1 GB | Production choice. 75% memory savings. Acceptable quality for summarization tasks |

#### Batch Sizing

- `batch=8192, ubatch=4096` on timmy (large batches for prompt processing)
- `batch=2048, ubatch=512` on manu (smaller GPU, 8 threads)
- Embedder batch_size increased from 8 to 64 for throughput

#### Context Window

- Embedder ctx-size bumped from 8192 to 16384 to fix embedding failures with 8 parallel slots
- Ollama context: 24K optimal for qwen3.5:9b -- higher values (48K, 65K) cause gen slowdown as KV spills to RAM
- qwen3.5-128k variant created with 131072 ctx for long-context tasks (separate from default 24K)
- `max_tokens=2048` is "free insurance" -- model naturally stops at 100-900 tokens for summarization

#### Networking

- Flannel backend switched from VXLAN to host-gw (2026-03-21) -- VXLAN halved timmy's throughput due to Realtek NIC lacking checksum offloading
- HTTP/2 enabled in OpenViking MCP server (httpx client) -- eliminated per-request connection overhead through Cloudflare Tunnel
- Post-fix network benchmarks: ~950 Mbits/sec between all nodes

### 5.3 What Didn't Work

| Attempt | Why It Failed |
|---------|--------------|
| Qwen3.5-27B Q3_K_M for summarization | 60% failure rate at 1024 max_tokens -- verbose output truncated |
| 238W power limit on GTX 1080 | Memory clock BIOS-locked at 4513 MHz regardless |
| flash-attn "on" vs "auto" on GTX 1080 | 0% difference -- auto was already enabling it |
| Shared LLM on timmy (8 consumers, 4 slots) | 2:1 oversubscription caused 50-70s agent test latency |
| 8 parallel slots with 4096 ctx | Each slot gets only 512 tokens -- too small for most inputs |
| Nanochat training without iGPU filtering | ROCm enumerates both devices, crashes on iGPU |
| OV multi-worker in v0.2.9 | Scoped search broken, no true multi-worker support |
| Large model (Qwen3 14B) on GTX 1080 | Exceeded 8 GB VRAM, switched to Qwen3-8B |
| devstral-small-2:24b on 16GB | Filled VRAM exactly at 100% offload; required scaling all other GPU workloads to 0 |

---

## 6. AI Tools Deployed

### 6.1 OpenViking Knowledge Base

**Purpose:** Semantic RAG service for persistent knowledge across Claude Code sessions.

| Component | Namespace | Node | Status | Config |
|-----------|-----------|------|--------|--------|
| OpenViking server (v0.2.9) | viking | wemby | Running (1 replica) | AGFS on Garage S3, 4 CPU / 4Gi mem |
| OV Workers (StatefulSet) | viking | spread | Running (3/3) | 5Gi PVC each, Longhorn SSD, 3 CPU / 3Gi mem |
| OV Coordinator | viking | timmy | Running (1/1) | Routes parallel indexing across workers |
| OV Merge service | viking | timmy | Running (1/1) | Unified read service for multi-worker reads |
| OV Console | viking | wemby | Running (1/1) | Web UI for browsing knowledge base |
| Embedder (llama.cpp) | viking | timmy | Running (1/1) | nomic-embed-text-v1.5 f16, CPU-only, 768-dim, 8 parallel, ctx 16384 |
| VLM (llama.cpp CUDA) | viking | manu | Running (1/1) | Qwen3-8B Q4_K_M, 4 slots, 32768 ctx, q4_0 KV |
| MCP Server | local | -- | Python process | httpx with HTTP/2, connects to OV + VLM + Embedder |

**Ingress:** context.nathanwhyte.dev via Traefik.

**Storage evolution:**
- Local PVC → Garage S3 (AGFS migration)
- Discovered v0.2.9 limitations: no multi-worker, scoped search broken, creds must be inline
- Workers scaled from 2 → 3 for better throughput

### 6.2 Summarizer API (Removed)

**Status:** Scaled to 0 and removed from active service routing (Apr 2026).

Previously provided agentic tool-calling loop with LLM + OpenViking integration. Functionality superseded by OpenViking's native tool-calling capabilities.

### 6.3 Ollama

**Purpose:** Local LLM for Claude Code integration (`ollama launch claude`).

| Setting | Value |
|---------|-------|
| Image | `ollama/ollama:rocm` |
| Node | timmy (RX 9070 XT) |
| Warm model | `gemma4:e4b` (~10 GB VRAM, 69%/31% CPU/GPU split) |
| Available models | `qwen3.5`, `qwen3.5-128k` (131072 ctx), `gemma4:e4b`, `mistral-nemo`, `starcoder2`, `codestral`, `starcoder2:15b`, `codestral:22b` |
| Flash attention | Enabled |
| KV cache type | q4_0 (75% memory savings) |
| Keep alive | Infinite (-1) -- dedicated GPU, single-user |
| Parallel slots | 1 (Claude Code is single-threaded) |
| Max loaded models | 1 (16GB can't fit two large models simultaneously) |
| Context | 24K default; 128K for qwen3.5-128k variant |
| Thinking mode | Disabled (/no_think) -- inflates tokens 10-20x |
| Monitoring | Ollama exporter sidecar (port 9111) |
| Auth | nginx auth proxy in front of Ollama API |
| External access | robots.nathanwhyte.dev via Cloudflare Tunnel |

**Removed models (Apr 2026):** devstral-4k, devstral-small-2-4k, devstral-small-2-32k -- pruned to free ~80 GB.

### 6.4 Open WebUI

**Purpose:** Web chat interface for Ollama models.

- Namespace: openwebui
- StatefulSet: 1 replica
- Service: ClusterIP on port 80
- Node: manu (moved from wemby to free wemby's resources for OpenViking)
- Web search: SearXNG integration enabled (searxng namespace)

### 6.5 SearXNG

**Purpose:** Privacy-respecting search aggregator for OpenWebUI web search.

- Namespace: searxng
- Deployment: 1 replica
- Node: manu
- Config: `server.secret_key` required in ConfigMap (missing key caused CrashLoopBackOff, patched Apr 30)
- Integration: OpenWebUI configured to use SearXNG for web search queries

### 6.6 Nanochat Training Pipeline

**Purpose:** LLM fine-tuning on AMD GPU.

| Component | Detail |
|-----------|--------|
| Dockerfile | `Dockerfile.rocm` (ROCm-based PyTorch) |
| Training job | `train-rocm-job.yaml` (K8s Job) |
| Monitoring | WandB integration (API key secret) |
| GPU | timmy's RX 9070 XT |
| Target model name | "pop" (`--run=pop`) |
| Fix applied | Single GPU with v3 image (iGPU filtering) |
| Status | Moved to `backlog/` (May 2026) -- not actively training |

### 6.7 GPU Monitoring Stack

| Component | Type | Node Selector | Metrics |
|-----------|------|---------------|---------|
| amdgpu-exporter | DaemonSet (kube-system) | `gpu.vendor: amd` | utilization %, temp C, power W, VRAM used/total, fan RPM, clock MHz |
| DCGM Exporter | GPU Operator (kube-system) | NVIDIA nodes | Standard DCGM metrics |
| Ollama Exporter | Sidecar (llama ns) | timmy | Model status, VRAM, tok/s, request counts |
| GPU Overview Dashboard | Grafana | -- | Combined view of all 3 GPUs |
| NVIDIA DCGM Dashboard | Grafana | -- | Fixed version of community 12239 |
| AMD GPU Dashboard | Grafana | -- | Custom dashboard for 9070 XT |

---

## 7. Current State (2026-05-01)

### Running Workloads

| Namespace | Deployment | Replicas | Node | GPU | Purpose |
|-----------|-----------|----------|------|-----|---------|
| **viking** | llamacpp-cuda-ov | 1/1 | manu | GTX 1080 | OV VLM inference (Qwen3-8B) |
| **viking** | llamacpp-rocm | 0/0 | -- | -- | Scaled to 0 (hot standby) |
| **viking** | embedder-llamacpp | 1/1 | timmy | CPU | nomic-embed-text-v1.5 |
| **viking** | openviking | 1/1 | wemby | -- | OV server |
| **viking** | ov-worker (SS) | 3/3 | spread | -- | OV data workers |
| **viking** | ov-coordinator | 1/1 | timmy | -- | Parallel indexing coordinator |
| **viking** | ov-console | 1/1 | wemby | -- | OV web UI |
| **viking** | ov-merge | 1/1 | timmy | -- | Unified read service |
| **llama** | ollama | 1/1 | timmy | RX 9070 XT | Ollama for Claude Code |
| **llama** | ollama-auth-proxy | 1/1 | timmy | -- | BasicAuth proxy for Ollama |
| **llama** | cloudflared | 0/0 | -- | -- | Scaled to 0 (using namespace tunnels) |
| **openwebui** | open-webui (SS) | 1/1 | manu | -- | Chat UI |
| **searxng** | searxng | 1/1 | manu | -- | Search aggregator |

### Service Routing

| Service | Endpoint | Backends |
|---------|----------|----------|
| `llamacpp-cuda-llm.viking.svc:80` | OV VLM | Selector: `app: llamacpp-cuda-ov` (points to manu CUDA) |
| `embedder-llamacpp.viking.svc:8080` | Embeddings | nomic-embed on timmy (CPU) |
| `openviking.viking.svc:1933` | Knowledge base | OV server on wemby |
| `ollama.llama.svc:80` | Ollama API | Ollama on timmy |
| `qwen-summarizer-llm.llama.svc:80` | Summarizer LLM | **Removed** (Apr 2026) |
| `summarizer-api.llama.svc:80` | Agent API | **Removed** (Apr 2026) -- replaced by OV tool-calling |

### GPU Allocation

| GPU | Current Workload | VRAM Used | Notes |
|-----|-----------------|-----------|-------|
| RX 9070 XT (timmy) | Ollama gemma4:e4b | ~10 GB | Model loaded indefinitely; qwen3.5 available on demand |
| GTX 1080 (manu) | llamacpp-cuda-ov Qwen3-8B | ~6.2 GB / 8 GB | 4 parallel slots, power limit 220W |
| GTX 1060 (wemby) | Unused | 0 | Available for lightweight tasks |

---

## 8. Key Decisions and Rationale

### Decision 1: Qwen3-8B over larger models

**Context:** Evaluated Mistral-Small-24B, Qwen3.5-27B, and Claude Haiku alongside Qwen3-8B.

**Rationale:** Qwen3-8B matched 24B model quality (8.1/10) for summarization at 3x less VRAM. The 27B model was unusable due to truncation issues. The 8B model fits comfortably on both the 9070 XT and GTX 1080 with room for KV cache.

### Decision 2: Split GPU architecture

**Context:** Initially ran shared LLM on timmy serving both OV and summarizer-api (8 consumers, 4 slots = 2:1 oversubscription).

**Rationale:** Agent tests took 50-70s due to slot queuing. After splitting -- timmy for OV, manu for summarizer-api -- agent tests dropped to 9-13s (5-6x faster). Even though GTX 1080 is 2x slower raw (29 t/s vs 59 t/s), zero contention wins on interactive latency.

**Update (Mar 28):** OV VLM was later moved entirely to manu (llamacpp-cuda-ov), and timmy was freed for Ollama. Service selector `llamacpp-rocm-llm` now points to `app: llamacpp-cuda-ov` on manu.

### Decision 3: CPU-only embedder

**Context:** Embedder initially ran on GPU alongside LLM, causing VRAM contention.

**Rationale:** nomic-embed-text-v1.5 is small enough for CPU inference. Moving it to CPU freed GPU VRAM entirely for LLM weights + KV cache. No perceptible latency impact for embedding requests.

### Decision 4: KV cache q4_0 quantization

**Context:** KV cache precision trades quality for memory.

**Rationale:** At q4_0, KV cache uses ~1 GB per 8192 context (vs 4 GB at f16). For summarization tasks, the quality difference is negligible. This allows more parallel slots within the same VRAM budget.

### Decision 5: Ollama for Claude Code instead of raw llama.cpp

**Context:** timmy's 9070 XT could run either llama.cpp or Ollama.

**Rationale:** Ollama provides model management, Modelfile customization, keep-alive, and native `ollama launch claude` integration. The overhead is minimal.

### Decision 6: LACT for GPU tuning (consolidated)

**Context:** Initially had 3 separate services (amdgpu-fan, bash script, rocm-smi calls).

**Rationale:** LACT consolidates power cap, COMPUTE profile, and fan curve into a single persistent service. Survives reboots (with verification). Junction temp stays ~88C under sustained load.

### Decision 7: Flannel host-gw over VXLAN

**Context:** VXLAN encapsulation was halving timmy's pod network throughput.

**Rationale:** Realtek USB-C NIC on timmy lacks VXLAN checksum offloading. host-gw eliminates encapsulation overhead entirely. Constraint: all nodes must stay on the same L2 subnet.

### Decision 8: Garage S3 for OpenViking storage

**Context:** OV AGFS initially on local PVC. Needed shared storage for multi-worker.

**Rationale:** Garage S3 provides shared object storage accessible from any node. However, discovered v0.2.9 limitations -- multi-worker and scoped search broken, credentials must be inline. Still better than node-pinned local PVCs for availability.

### Decision 9: Remove devstral models (Apr 2026)

**Context:** devstral-small-2:24b filled 16GB VRAM at 100% GPU offload, requiring all other GPU workloads to be scaled to 0.

**Rationale:** The model was impractical for daily use in a multi-workload cluster. Smaller variants (4k, 32k) were also unused. Removed from startup script and pruned ~80 GB from PVC to free space.

### Decision 10: Add qwen3.5-128k variant (Apr 2026)

**Context:** Need for long-context tasks beyond Ollama's default 24K context.

**Rationale:** Created a separate Modelfile variant with `num_ctx 131072` for tasks requiring very long context windows. Default qwen3.5 remains at 24K for speed. The 128K variant trades speed for context length.

---

## 9. Lessons Learned

1. **Smaller models can match larger ones** for specific tasks. Benchmark before assuming bigger = better.
2. **Contention matters more than raw speed** for interactive workloads. Split architecture over shared GPU.
3. **VRAM is the primary constraint.** Every decision (model quant, KV precision, parallel slots, embedder placement) revolves around VRAM budget.
4. **GeForce cards have hidden limitations** -- BIOS-locked memory clocks, no `nvidia-smi -ac`, which cap practical throughput below theoretical.
5. **RDNA 4 support arrived** -- Ollama and llama.cpp both work on the 9070 XT with ROCm 7.2, but the iGPU must be explicitly filtered.
6. **Network tuning has outsized impact** -- VXLAN→host-gw and HTTP/1.1→HTTP/2 both provided large throughput improvements with minimal effort.
7. **Monitoring is essential** -- GPU dashboards, Ollama exporter, and Prometheus scraping caught power-starvation and thermal issues that would have gone unnoticed.
8. **Model hoarding is expensive** -- keeping unused models on PVC consumes significant storage. Regular pruning saves space and reduces startup time.
9. **ConfigMap secrets are a footgun** -- SearXNG's `server.secret_key` missing from ConfigMap caused CrashLoopBackOff. Always validate required secrets in ConfigMaps.
10. **128K context is a different beast** -- while possible, models with 131072 context require careful memory management and are significantly slower than their 24K counterparts.
