# llama

LLM serving infrastructure in the `llama` namespace.

## Active services

### Ollama (timmy — AMD RX 9070 XT)

- **Models**: gemma4:12b-it-qat (local), glm-5.1:cloud (remote); others available on demand
- **Endpoint**: `http://ollama.llama.svc:11434` (ClusterIP) or `192.168.1.19:11434` (LoadBalancer externalIP)
- **Config**: `OLLAMA_NUM_PARALLEL=1`, `OLLAMA_CONTEXT_LENGTH=131072`, `OLLAMA_MAX_LOADED_MODELS=1`, `OLLAMA_KV_CACHE_TYPE=q4_0`, `OLLAMA_KEEP_ALIVE=30m`
- **Deploy**: `llama/ollama-deployment.yaml`

### Ollama Auth Proxy (wemby)

- **Purpose**: nginx Bearer-token auth (`Authorization: Bearer <ollama-api-key>`) for external access
- **Endpoint**: `http://ollama-auth-proxy.llama.svc:80`
- **Deploy**: `llama/ollama-auth-proxy.yaml`

### Chat Ollama Proxy

- **Purpose**: Reasoning-suppressing shim (`INJECT_REASONING_NONE=true`) for Hermes; routes to `ollama.llama.svc:11434`
- **Endpoint**: `http://chat-ollama-proxy.llama.svc:11434`
- **Deploy**: `llama/chat-ollama-proxy.yaml`

### Cloud LLM Counter

- **Purpose**: Daily cloud API call budget guard (`DAILY_CLOUD_CALL_BUDGET=200`)
- **Endpoint**: `http://cloud-llm-counter.llama.svc:80`
- **Deploy**: `llama/cloud-llm-counter.yaml`

## Retired services

### ROCm llamacpp (timmy — AMD RX 9070 XT)

**Permanently retired** (IDEA-009 Phase 4, 2026-06-06). Manifest retained for rollback only. The VLM now runs exclusively on manu's GTX 1080 via `llamacpp-cuda-ov` in the `viking` namespace.

## Quick test

```bash
kubectl run -it --rm curl --image=curlimages/curl:8.12.1 --restart=Never -- \
  curl -sS http://ollama.llama.svc:11434/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"gemma4:12b-it-qat","messages":[{"role":"user","content":"Write one sentence about homelabs."}],"max_tokens":64}'
```