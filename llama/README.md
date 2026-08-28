# llama

LLM serving infrastructure in the `llama` namespace.

## Active services

### Ollama (timmy — AMD RX 9070 XT)

- **Models**: deepseek-coder-v2:fim (16B-lite MoE Q4, ~13 GB — the single edit-prediction model since the 2026-07-24 cutover; serves Zed `deepseek_coder` FIM, Minuet suffix-FIM, and remote VSCode FIM). zeta2.1 + qwen2.5-coder:fim retired from the warm list (BUG-1037: open-weights Zeta degenerates below ~3k prompt tokens vs Zed's hardcoded ~500-token self-hosted excerpts; tags kept on the PVC for rollback). gemma4:12b-it-qat orphaned (Hermes/mem0 retired, no longer warmed); OV's `gemma4:31b-cloud` VLM tag routes through this pod but occupies no local slot
- **Endpoint**: `http://ollama.llama.svc:11434` (ClusterIP) or `192.168.1.19:11434` (LoadBalancer externalIP)
- **Backend**: Vulkan on the discrete RX 9070 XT (`OLLAMA_VULKAN=1`, `OLLAMA_LLM_LIBRARY=vulkan`, `GGML_VK_VISIBLE_DEVICES=1`)
- **Config**: `OLLAMA_MAX_LOADED_MODELS=1` (dropped from 2 at the 2026-07-24 cutover — one pinned runner, no co-load), `OLLAMA_NUM_PARALLEL=2`, `OLLAMA_CONTEXT_LENGTH=131072` (TASK-1188/IMPR-1114; KV allocated on demand), `OLLAMA_KV_CACHE_TYPE=q8_0` (bumped from `q4_0` — 4.2 GB headroom at 2 slots covers it), `OLLAMA_KEEP_ALIVE=-1` (pinned); `CAP_PERFMON` for accurate VRAM packing under Vulkan
- **Deploy**: `llama/ollama-deployment.yaml`

### Ollama Auth Proxy

- nginx Bearer-token auth proxy for Ollama (`Authorization: Bearer <...>`)
- No nodeSelector — not pinned to any specific node
- **Purpose**: nginx Bearer-token auth (`Authorization: Bearer <ollama-api-key>`) for external access
- **Endpoint**: `http://ollama-auth-proxy.llama.svc:80`
- **Deploy**: `llama/ollama-auth-proxy.yaml`

### Chat Ollama Proxy

- **Purpose**: Reasoning-suppressing shim (`INJECT_REASONING_NONE=true`) routing to `ollama.llama.svc:11434`. **⚠️ Load-bearing — do NOT delete:** originally a Hermes shim, but since Hermes was retired (2026-07-18) this pod is **OpenViking's VLM route** — the `chat-ollama` Service selects `app=chat-ollama-proxy`, and `openviking-configmap.yaml`'s `vlm.api_base` is `http://chat-ollama.llama.svc:11434/v1` (`gemma4:31b-cloud`).
- **Endpoint**: `http://chat-ollama-proxy.llama.svc:11434` (also reached as `chat-ollama.llama.svc` — same pod)
- **Deploy**: `llama/chat-ollama-proxy.yaml`

### Cloud LLM Counter

- **Purpose**: Daily cloud API call usage tracker (`DAILY_CLOUD_CALL_BUDGET=1000000` — effectively uncapped since 2026-07-06; advisory metrics only, nothing enforces it in the request path). Fed by `chat-ollama-proxy`'s `METRICS_PUSH_URL` — its live metrics sink, not a Hermes leftover.
- **Endpoint**: `http://cloud-llm-counter.llama.svc:80`
- **Deploy**: `llama/cloud-llm-counter.yaml`

## Retired services

### ROCm llamacpp (timmy — AMD RX 9070 XT)

**Permanently retired** (IDEA-009 Phase 4, 2026-06-06). Manifest retained for rollback only. The VLM now runs exclusively on manu's GTX 1080 via `llamacpp-cuda-ov` in the `viking` namespace.

## Benchmarking

`llama/tools/ollama-concurrency-benchmark.py` measures how `gemma4:12b-it-qat` behaves under 1..N concurrent client requests. Use it to validate `OLLAMA_NUM_PARALLEL` sizing for Hermes sub-agents and mem0 extraction sharing the same GPU.

```bash
# Basic sweep 1..6 against the current Ollama config
uv run --with aiohttp python llama/tools/ollama-concurrency-benchmark.py \
  --max-concurrency 6 --output-json results.json --output-csv results.csv

# Mixed workload: one mem0-style long extraction prompt plus sub-agent prompts
uv run --with aiohttp python llama/tools/ollama-concurrency-benchmark.py \
  --max-concurrency 6 --mixed --output-json results-mixed.json

# Run inside the cluster from a throwaway pod
kubectl run -it --rm bench --image=python:3.12-slim --restart=Never -- \
  sh -c "pip install aiohttp && curl -sSLO https://raw.githubusercontent.com/nathanwhyte/homelab/main/llama/tools/ollama-concurrency-benchmark.py && python ollama-concurrency-benchmark.py --url http://ollama.llama.svc:11434"
```

To compare multiple `NUM_PARALLEL` values (e.g., 2, 4, 6), use the sweep wrapper. It patches the Ollama Deployment, waits for rollout, runs the benchmark, and restores the original value:

```bash
NP_VALUES="2 4 6" BENCHMARK_ARGS="--max-concurrency 6 --mixed" llama/tools/sweep-num-parallel.sh
```

Results match the methodology in INFO-047 and feed into INFO-055.

## Quick test

```bash
kubectl run -it --rm curl --image=curlimages/curl:8.12.1 --restart=Never -- \
  curl -sS http://ollama.llama.svc:11434/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"gemma4:12b-it-qat","messages":[{"role":"user","content":"Write one sentence about homelabs."}],"max_tokens":64}'
```
