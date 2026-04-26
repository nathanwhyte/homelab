# llama

LLM serving infrastructure in the `llama` namespace.

## Active services

### Ollama (timmy — AMD RX 9070 XT)

- **Models**: 14 models available (qwen3:8b, qwen2.5-coder:14b, gemma4:e4b, mistral-nemo, ministral-3/14b, etc.)
- **Endpoint**: `http://ollama.llama.svc:80` (ClusterIP) or `192.168.1.19:11434` (bare metal)
- **Deploy**: Managed via `llama/ollama-deployment.yaml`

### ROCm llamacpp (timmy — AMD RX 9070 XT, scaled to 0)

- **Deploy**: `./llama/deploy-llamacpp-rocm.sh`
- **Endpoint**: `http://llama-rocm-api.llama.svc.cluster.local/v1`

## Quick test

```bash
kubectl run -it --rm curl --image=curlimages/curl:8.12.1 --restart=Never -- \
  curl -sS http://ollama.llama.svc/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen3:8b","messages":[{"role":"user","content":"Write one sentence about homelabs."}],"max_tokens":64}'
```
