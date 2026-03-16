#!/usr/bin/env bash
set -euo pipefail

REGISTRY="registry.nathanwhyte.dev/library"
NANOCHAT_DIR="$HOME/code/homelab/nanochat"

if [[ "${1:-}" == "--rocm-only" ]]; then
    echo "Building nanochat ROCm image..."
    docker build -f "$NANOCHAT_DIR/Dockerfile.rocm" -t "$REGISTRY/nanochat:rocm" "$NANOCHAT_DIR"
    docker push "$REGISTRY/nanochat:rocm"
    echo -e "\nDone! $REGISTRY/nanochat:rocm pushed"
    exit 0
fi

echo "Building nanochat ROCm image..."
docker build -f "$NANOCHAT_DIR/Dockerfile.rocm" -t "$REGISTRY/nanochat:rocm" "$NANOCHAT_DIR"

echo "Building nanochat CUDA image..."
docker build -f "$NANOCHAT_DIR/Dockerfile.cuda" -t "$REGISTRY/nanochat:cuda" "$NANOCHAT_DIR"

echo "Pushing images to Harbor..."
docker push "$REGISTRY/nanochat:rocm"
docker push "$REGISTRY/nanochat:cuda"

echo -e "\nDone! Images pushed:"
echo "  $REGISTRY/nanochat:rocm"
echo "  $REGISTRY/nanochat:cuda"
