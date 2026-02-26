# llama

OpenAI-compatible local LLM serving in the `llama` namespace.

This folder contains two deployment paths:

- `deploy-llamacpp.sh` (recommended for current GPUs, Pascal `sm_61`)
- `deploy-llama-vllm.sh` (kept for reference; vLLM requires newer GPU arch)

## Current endpoint

- Base URL: `http://llama-api.llama.svc.cluster.local/v1`

## Deploy (llama.cpp)

```bash
./llama/deploy-llamacpp.sh
```

## Quick in-cluster test

```bash
kubectl run -it --rm curl --image=curlimages/curl:8.12.1 --restart=Never -- \
  curl -sS http://llama-api.llama.svc.cluster.local/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf","messages":[{"role":"user","content":"Write one sentence about homelabs."}],"max_tokens":64}'
```

## Files

- `namespace.yaml`: creates namespace `llama`
- `pvc.yaml`: `llama-model-cache` PVC (`100Gi`, `longhorn-ssd`)
- `llamacpp-deployment.yaml`: llama.cpp OpenAI server deployment
- `llamacpp-service.yaml`: ClusterIP service `llama-api`

## Switching model

Update both in `llamacpp-deployment.yaml`:

- init container download URL and output filename
- server `--model` path to the same filename
