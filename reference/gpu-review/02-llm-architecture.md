# GPU & AI Infrastructure Review — LLM Architecture Evolution & Current State

Split from `GPU_AND_AI_REVIEW.md` (compiled 2026-05-01) into per-topic files
in `reference/gpu-review/`. This file covers the **how it evolved** LLM
architecture timeline (Feb–Apr 2026) and the **what was running** state as of
2026-05-01, plus the key design decisions behind the architecture.

For current cluster state, see `CLAUDE.md` (service routing, LLM config, topology)
and `HARDWARE.md` (node specs). The original 619-line document is retained at
`GPU_AND_AI_REVIEW.md` for now.

> The architecture diagram (lines 185–233 of the original) is a textual cluster
> layout using box-drawing characters; the ASCII art is preserved below for
> historical reference but is **not** the canonical architecture — the current
> cluster layout has changed substantially since 2026-05-01.

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

```text
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

## 7. Current State (2026-05-01)

### Running Workloads

| Namespace     | Deployment        | Replicas | Node   | GPU        | Purpose                                                                  |
| ------------- | ----------------- | -------- | ------ | ---------- | ------------------------------------------------------------------------ |
| **viking**    | llamacpp-cuda-ov  | 1/1      | manu   | GTX 1080   | OV VLM inference (Qwen3-8B)                                              |
| **viking**    | llamacpp-rocm     | —        | —      | —          | Retired 2026-06-06 (commit `ebeffcc`); VLM is `llamacpp-cuda-ov` on manu |
| **viking**    | embedder-llamacpp | 1/1      | wemby  | GTX 1060   | nomic-embed-text-v1.5 (CUDA)                                             |
| **viking**    | openviking        | 1/1      | wemby  | --         | OV server                                                                |
| **viking**    | ov-worker (SS)    | 3/3      | spread | --         | OV data workers                                                          |
| **viking**    | ov-coordinator    | 1/1      | timmy  | --         | Parallel indexing coordinator                                            |
| **viking**    | ov-console        | 1/1      | wemby  | --         | OV web UI                                                                |
| **viking**    | ov-merge          | 1/1      | timmy  | --         | Unified read service                                                     |
| **llama**     | ollama            | 1/1      | timmy  | RX 9070 XT | Ollama for Claude Code                                                   |
| **llama**     | ollama-auth-proxy | 1/1      | timmy  | --         | BasicAuth proxy for Ollama                                               |
| **llama**     | cloudflared       | 0/0      | --     | --         | Scaled to 0 (using namespace tunnels)                                    |
| **openwebui** | open-webui (SS)   | 1/1      | manu   | --         | Chat UI                                                                  |
| **searxng**   | searxng           | 1/1      | manu   | --         | Search aggregator                                                        |

### Service Routing

| Service                             | Endpoint       | Backends                                                |
| ----------------------------------- | -------------- | ------------------------------------------------------- |
| `llamacpp-cuda-llm.viking.svc:80`   | OV VLM         | Selector: `app: llamacpp-cuda-ov` (points to manu CUDA) |
| `embedder-llamacpp.viking.svc:8080` | Embeddings     | nomic-embed on wemby (GTX 1060, CUDA)                   |
| `openviking.viking.svc:1933`        | Knowledge base | OV server on wemby                                      |
| `ollama.llama.svc:80`               | Ollama API     | Ollama on timmy                                         |
| `qwen-summarizer-llm.llama.svc:80`  | Summarizer LLM | **Removed** (Apr 2026)                                  |
| `summarizer-api.llama.svc:80`       | Agent API      | **Removed** (Apr 2026) -- replaced by OV tool-calling   |

### GPU Allocation

| GPU                | Current Workload              | VRAM Used      | Notes                                                  |
| ------------------ | ----------------------------- | -------------- | ------------------------------------------------------ |
| RX 9070 XT (timmy) | Ollama gemma4:e4b             | ~10 GB         | Model loaded indefinitely; qwen3.5 available on demand |
| GTX 1080 (manu)    | llamacpp-cuda-ov Qwen3-8B     | ~6.2 GB / 8 GB | 4 parallel slots, power limit 220W                     |
| GTX 1060 (wemby)   | embedder-llamacpp nomic-embed | ~1 GB          | CUDA embeddings (moved from timmy in IDEA-009 Phase 2) |

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

## Source

Extracted from `GPU_AND_AI_REVIEW.md` lines 113–233 (section 3) + lines 501–606
(sections 7 + 8). The architecture diagram at lines 185–233 is preserved as a
`text`-fenced ASCII layout — it documents the **2026-05-01** state, not the
current cluster layout (consult `CLAUDE.md` and `HARDWARE.md` for current
service routing and node roles).
