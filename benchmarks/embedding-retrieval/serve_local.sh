#!/usr/bin/env bash
# Serve a candidate embedder locally via llama.cpp (Metal) for the TASK-1122 A/B.
# Params match model-parameters.md: 8192 ctx, ubatch>=ctx, correct pooling,
# nomic yarn-scaled to 8192. Foreground (Ctrl-C to stop); run one per terminal.
#
#   ./serve_local.sh nomic    # -> :8081  mean pooling, yarn
#   ./serve_local.sh qwen06   # -> :8082  last pooling
#   ./serve_local.sh eg2q8    # -> :8084  mean pooling (EmbeddingGemma 2 GGUF)
#
# LLAMA_SERVER overrides the binary, e.g. a newer release build: EmbeddingGemma 2
# needs llama.cpp with ggml-org/llama.cpp#30054 (merged 2026-10-06).
set -euo pipefail
cd "$(dirname "$0")"
llama_server=${LLAMA_SERVER:-llama-server}

case "${1:-}" in
  nomic)
    # --parallel 1 so n_ctx_slot == ctx-size (8192); default 4 slots => 2048/slot
    # would truncate ~29% of the corpus (the IMPR-1005 overflow trap).
    exec "$llama_server" -m models/nomic/nomic-embed-text-v1.5.f16.gguf \
      --host 127.0.0.1 --port 8081 --embedding --pooling mean --parallel 1 \
      --ctx-size 8192 --batch-size 8192 --ubatch-size 8192 --n-gpu-layers 99 \
      --rope-scaling yarn --rope-freq-scale 0.75 ;;
  qwen06)
    exec "$llama_server" -m models/qwen06/Qwen3-Embedding-0.6B-f16.gguf \
      --host 127.0.0.1 --port 8082 --embedding --pooling last --parallel 1 \
      --ctx-size 8192 --batch-size 8192 --ubatch-size 8192 --n-gpu-layers 99 ;;
  qwen4b)
    # Qwen3-Embedding-4B Q8_0 on Mac Metal, prod-parity params. No --mlock.
    exec "$llama_server" -m models/qwen4b/Qwen3-Embedding-4B-Q8_0.gguf \
      --host 127.0.0.1 --port 8083 --embedding --pooling last --parallel 1 \
      --ctx-size 8192 --batch-size 8192 --ubatch-size 8192 --n-gpu-layers 99 ;;
  eg2q8|eg2bf16)
    # EmbeddingGemma 2 text model, official ggml-org GGUFs; no mmproj (text only).
    quant=Q8_0; [ "$1" = eg2bf16 ] && quant=BF16
    exec "$llama_server" -m "models/eg2/embeddinggemma-2-$quant.gguf" \
      --host 127.0.0.1 --port 8084 --embedding --pooling mean --parallel 1 \
      --ctx-size 8192 --batch-size 8192 --ubatch-size 8192 --n-gpu-layers 99 ;;
  *)
    echo "usage: $0 {nomic|qwen06|qwen4b|eg2q8|eg2bf16}" >&2; exit 1 ;;
esac
