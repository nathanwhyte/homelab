# llama

LLM serving infrastructure in the `llama` namespace.

## Active services

### Qwen summarizer (manu — NVIDIA GTX 1080)

- **Model**: Qwen3-8B Q4_K_M via llama.cpp
- **Endpoint**: `http://qwen-summarizer-llm.llama.svc.cluster.local/v1`
- **Deploy**: `./llama/deploy-summarizer.sh`
- **Used by**: OpenViking (VLM), summarizer-api

### Summarizer API

- **Endpoint**: `http://summarizer-api.llama.svc.cluster.local/summarize`
- **Modes**: `context`, `conversation`, `code`

### ROCm server (timmy — AMD RX 9070 XT, scaled to 0)

- **Deploy**: `./llama/deploy-llamacpp-rocm.sh`
- **Endpoint**: `http://llama-rocm-api.llama.svc.cluster.local/v1`

## Quick test

```bash
kubectl run -it --rm curl --image=curlimages/curl:8.12.1 --restart=Never -- \
  curl -sS http://qwen-summarizer-llm.llama.svc.cluster.local/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen3-8b","messages":[{"role":"user","content":"Write one sentence about homelabs."}],"max_tokens":64}'
```
