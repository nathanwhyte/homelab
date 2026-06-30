# GPU & AI Infrastructure Review — Tuning, Tools, & Lessons Learned

Split from `GPU_AND_AI_REVIEW.md` (compiled 2026-05-01) into per-topic files
in `reference/gpu-review/`. This file covers the **operational** content:
performance tuning history, AI tools deployed, and the lessons-learned list
that informed the 2026 cluster design.

For current cluster state, see `CLAUDE.md`, `HARDWARE.md`, and
`reference/llm-config.md`.

## 5. Performance Tuning

### 5.1 GPU Power & Thermal Tuning

#### timmy (RX 9070 XT)

| Setting      | Before              | After              | Impact                                                 |
| ------------ | ------------------- | ------------------ | ------------------------------------------------------ |
| Power cap    | 304W (default)      | 340W (LACT)        | Card was power-starved; 340W unlocked full performance |
| GPU profile  | Default             | COMPUTE (rocm-smi) | Dedicated compute mode                                 |
| Fan curve    | Default             | Aggressive (LACT)  | Junction temp stays ~88C under load                    |
| Tuning stack | 3 separate services | LACT consolidated  | Single service manages power, profile, fan curve       |

**Caveat:** COMPUTE profile and 340W must be re-verified after restarts.

#### manu (GTX 1080)

| Setting          | Before                 | After                                      | Impact                                                               |
| ---------------- | ---------------------- | ------------------------------------------ | -------------------------------------------------------------------- |
| Power limit      | 198W (default)         | 220W (nvidia-smi -pl 220 in initContainer) | GPU boosts to 1898-1923 MHz                                          |
| Power limit 238W | 220W                   | 238W                                       | **No improvement** -- memory clock stays at 4513 MHz                 |
| Memory clock     | 4513 MHz (BIOS-locked) | Cannot change                              | #1 bottleneck: effective bandwidth ~288 GB/s vs 320 GB/s theoretical |
| Flash attention  | auto                   | explicit on                                | ~0% difference (auto was already enabling it)                        |

### 5.2 LLM Server Tuning

#### Parallel Slots

| Config            | Slots | Ctx/Slot    | Impact                                                  |
| ----------------- | ----- | ----------- | ------------------------------------------------------- |
| Initial           | 8     | 65536 total | Underutilized -- too many slots with huge context       |
| Reduced           | 4     | 8192/slot   | Better utilization but slot contention                  |
| Split arch (manu) | 4     | 8192/slot   | Zero queuing for OV's 3 workers + 32% higher throughput |

#### KV Cache Quantization

| KV Precision  | Memory per 8192 ctx | Impact                                                                            |
| ------------- | ------------------- | --------------------------------------------------------------------------------- |
| f16 (default) | ~4 GB               | Too large for constrained VRAM                                                    |
| q8_0          | ~2 GB               | Good quality/memory tradeoff                                                      |
| q4_0          | ~1 GB               | Production choice. 75% memory savings. Acceptable quality for summarization tasks |

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

| Attempt                                    | Why It Failed                                                                      |
| ------------------------------------------ | ---------------------------------------------------------------------------------- |
| Qwen3.5-27B Q3_K_M for summarization       | 60% failure rate at 1024 max_tokens -- verbose output truncated                    |
| 238W power limit on GTX 1080               | Memory clock BIOS-locked at 4513 MHz regardless                                    |
| flash-attn "on" vs "auto" on GTX 1080      | 0% difference -- auto was already enabling it                                      |
| Shared LLM on timmy (8 consumers, 4 slots) | 2:1 oversubscription caused 50-70s agent test latency                              |
| 8 parallel slots with 4096 ctx             | Each slot gets only 512 tokens -- too small for most inputs                        |
| Nanochat training without iGPU filtering   | ROCm enumerates both devices, crashes on iGPU                                      |
| OV multi-worker in v0.2.9                  | Scoped search broken, no true multi-worker support                                 |
| Large model (Qwen3 14B) on GTX 1080        | Exceeded 8 GB VRAM, switched to Qwen3-8B                                           |
| devstral-small-2:24b on 16GB               | Filled VRAM exactly at 100% offload; required scaling all other GPU workloads to 0 |

---

## 6. AI Tools Deployed

### 6.1 OpenViking Knowledge Base

**Purpose:** Semantic RAG service for persistent knowledge across Claude Code sessions.

| Component                  | Namespace | Node   | Status              | Config                                                                       |
| -------------------------- | --------- | ------ | ------------------- | ---------------------------------------------------------------------------- |
| OpenViking server (v0.2.9) | viking    | wemby  | Running (1 replica) | AGFS on Garage S3, 4 CPU / 4Gi mem                                           |
| OV Workers (StatefulSet)   | viking    | spread | Running (3/3)       | 5Gi PVC each, Longhorn SSD, 3 CPU / 3Gi mem                                  |
| OV Coordinator             | viking    | timmy  | Running (1/1)       | Routes parallel indexing across workers                                      |
| OV Merge service           | viking    | timmy  | Running (1/1)       | Unified read service for multi-worker reads                                  |
| OV Console                 | viking    | wemby  | Running (1/1)       | Web UI for browsing knowledge base                                           |
| Embedder (llama.cpp)       | viking    | wemby  | Running (1/1)       | nomic-embed-text-v1.5 f16, CUDA (GTX 1060), 768-dim, --parallel 2, ctx 16384 |
| VLM (llama.cpp CUDA)       | viking    | manu   | Running (1/1)       | Qwen3-8B Q4_K_M, 4 slots, 32768 ctx, q4_0 KV                                 |
| MCP Server                 | local     | --     | Python process      | httpx with HTTP/2, connects to OV + VLM + Embedder                           |

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

| Setting           | Value                                                                                                                              |
| ----------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| Image             | `ollama/ollama:rocm`                                                                                                               |
| Node              | timmy (RX 9070 XT)                                                                                                                 |
| Warm model        | `gemma4:e4b` (~10 GB VRAM, 69%/31% CPU/GPU split)                                                                                  |
| Available models  | `qwen3.5`, `qwen3.5-128k` (131072 ctx), `gemma4:e4b`, `mistral-nemo`, `starcoder2`, `codestral`, `starcoder2:15b`, `codestral:22b` |
| Flash attention   | Enabled                                                                                                                            |
| KV cache type     | q4_0 (75% memory savings)                                                                                                          |
| Keep alive        | Infinite (-1) -- dedicated GPU, single-user                                                                                        |
| Parallel slots    | 1 (Claude Code is single-threaded)                                                                                                 |
| Max loaded models | 1 (16GB can't fit two large models simultaneously)                                                                                 |
| Context           | 24K default; 128K for qwen3.5-128k variant                                                                                         |
| Thinking mode     | Disabled (/no_think) -- inflates tokens 10-20x                                                                                     |
| Monitoring        | Ollama exporter sidecar (port 9111)                                                                                                |
| Auth              | nginx auth proxy in front of Ollama API                                                                                            |
| External access   | robots.nathanwhyte.dev via Cloudflare Tunnel                                                                                       |

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

| Component         | Detail                                                  |
| ----------------- | ------------------------------------------------------- |
| Dockerfile        | `Dockerfile.rocm` (ROCm-based PyTorch)                  |
| Training job      | `train-rocm-job.yaml` (K8s Job)                         |
| Monitoring        | WandB integration (API key secret)                      |
| GPU               | timmy's RX 9070 XT                                      |
| Target model name | "pop" (`--run=pop`)                                     |
| Fix applied       | Single GPU with v3 image (iGPU filtering)               |
| Status            | Moved to `backlog/` (May 2026) -- not actively training |

### 6.7 GPU Monitoring Stack

| Component              | Type                       | Node Selector     | Metrics                                                             |
| ---------------------- | -------------------------- | ----------------- | ------------------------------------------------------------------- |
| amdgpu-exporter        | DaemonSet (kube-system)    | `gpu.vendor: amd` | utilization %, temp C, power W, VRAM used/total, fan RPM, clock MHz |
| DCGM Exporter          | GPU Operator (kube-system) | NVIDIA nodes      | Standard DCGM metrics                                               |
| Ollama Exporter        | Sidecar (llama ns)         | timmy             | Model status, VRAM, tok/s, request counts                           |
| GPU Overview Dashboard | Grafana                    | --                | Combined view of all 3 GPUs                                         |
| NVIDIA DCGM Dashboard  | Grafana                    | --                | Fixed version of community 12239                                    |
| AMD GPU Dashboard      | Grafana                    | --                | Custom dashboard for 9070 XT                                        |

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

---

## Source

Extracted from `GPU_AND_AI_REVIEW.md` lines 323–498 (sections 5 + 6) and lines
608–619 (section 9). Tuning notes and lessons-learned remain valid as
historical reference; current settings are in `CLAUDE.md` and
`reference/llm-config.md`.
