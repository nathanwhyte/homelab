# llama

OpenAI-compatible local LLM serving in the `llama` namespace.

This folder uses the llama.cpp deployment path for current GPUs (Pascal `sm_61`).

- `deploy-llamacpp.sh`

## Current endpoint

- Base URL: `http://llama-api.llama.svc.cluster.local/v1`

## Deploy (llama.cpp)

```bash
./llama/deploy-llamacpp.sh
```

## Deploy memory stack

```bash
./llama/deploy-memory.sh
```

Memory components live in `llama/memory/`:

- `qdrant`: vector store for chat memory and profile facts
- `embedder-gpu`: primary embedder on `wemby` (`nvidia.com/gpu: 1`)
- `embedder-cpu`: fallback embedder when GPU is unavailable
- `memory-service`: retrieval/upsert API used by `chat_client.py`

## Quick in-cluster test

```bash
kubectl run -it --rm curl --image=curlimages/curl:8.12.1 --restart=Never -- \
  curl -sS http://llama-api.llama.svc.cluster.local/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"current.gguf","messages":[{"role":"user","content":"Write one sentence about homelabs."}],"max_tokens":64}'
```

## Files

- `namespace.yaml`: creates namespace `llama`
- `pvc.yaml`: `llama-model-cache` PVC (`100Gi`, `longhorn-ssd`)
- `llamacpp-deployment.yaml`: llama.cpp OpenAI server deployment
- `llamacpp-service.yaml`: ClusterIP service `llama-api`
- `chat_client.py`: one-shot OpenAI-compatible client with optional memory integration
- `deploy-memory.sh`: deploys Qdrant, embedders, and memory-service
- `memory/`: manifests and service code for durable memory

## Switching model

Update both in `llamacpp-deployment.yaml`:

- init container download URL and output filename
- server `--model` path to the same filename
