# llama

LLM serving infrastructure in the `llama` namespace.

## Active services

### Ollama (timmy — AMD RX 9070 XT)

- **Models**: gemma4:12b-it-qat (local chat) + qwen2.5-coder:fim (local FIM edit-prediction, co-resident); glm-5.1:cloud (remote fallback); others on demand
- **Endpoint**: `http://ollama.llama.svc:11434` (ClusterIP) or `192.168.1.19:11434` (LoadBalancer externalIP)
- **Config**: `OLLAMA_MAX_LOADED_MODELS=2` (chat + FIM co-resident since IMPR-1077 freed the card), `OLLAMA_NUM_PARALLEL=4`, `OLLAMA_CONTEXT_LENGTH=32768`, `OLLAMA_KV_CACHE_TYPE=q4_0`, `OLLAMA_KEEP_ALIVE=-1` (pinned); `CAP_PERFMON` for accurate VRAM packing under Vulkan (the default backend — no explicit `OLLAMA_VULKAN`)
- **Deploy**: `llama/ollama-deployment.yaml`

### Ollama Auth Proxy

- nginx Bearer-token auth proxy for Ollama (`Authorization: Bearer <...>`)
- No nodeSelector — not pinned to any specific node
- **Purpose**: nginx Bearer-token auth (`Authorization: Bearer <ollama-api-key>`) for external access
- **Endpoint**: `http://ollama-auth-proxy.llama.svc:80`
- **Deploy**: `llama/ollama-auth-proxy.yaml`

### Chat Ollama Proxy

- **Purpose**: Reasoning-suppressing shim (`INJECT_REASONING_NONE=true`) for Hermes; routes to `ollama.llama.svc:11434`
- **Endpoint**: `http://chat-ollama-proxy.llama.svc:11434`
- **Deploy**: `llama/chat-ollama-proxy.yaml`

### Cloud LLM Counter

- **Purpose**: Daily cloud API call usage tracker (`DAILY_CLOUD_CALL_BUDGET=1000000` — effectively uncapped since 2026-07-06; advisory metrics only, nothing enforces it in the request path)
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
