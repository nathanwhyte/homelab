# llama memory

Durable chat memory for the local llama.cpp endpoint.

This stack adds:

- `qdrant`: vector store for episodic memory and profile facts
- `embedder-gpu`: primary embedding endpoint pinned to `wemby` GPU
- `embedder-cpu`: fallback embedding endpoint for resilience
- `memory-service`: API that stores turns, extracts profile facts, retrieves memory

Image choices:

- CPU embedder uses `ghcr.io/huggingface/text-embeddings-inference:cpu-1.9`
- GPU embedder uses `ghcr.io/huggingface/text-embeddings-inference:cuda-1.9`

Note: TEI CUDA images require a supported NVIDIA runtime and GPU architecture.
Pascal GPUs (GTX 10xx, compute capability 6.1) are not supported by TEI CUDA.
If CUDA startup fails, memory-service automatically falls back to the CPU embedder.

## Deploy

```bash
./llama/deploy-memory.sh
```

## Endpoints (in-cluster)

- memory-service: `http://memory-service.llama.svc.cluster.local`
- qdrant: `http://qdrant.llama.svc.cluster.local:6333`

## Fallback behavior

`memory-service` tries GPU embeddings first. If GPU embedding fails or times out,
it falls back to CPU embeddings and opens a short cooldown before retrying GPU.
