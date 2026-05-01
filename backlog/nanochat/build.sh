#!/usr/bin/env bash
set -euo pipefail

REGISTRY="registry.nathanwhyte.dev/library"
TAG="rocm-v4"
NANOCHAT_DIR="$HOME/code/homelab/nanochat"

if [[ "${1:-}" == "--rocm-only" ]]; then
    echo "Building nanochat ROCm image ($TAG)..."
    docker build -f "$NANOCHAT_DIR/Dockerfile.rocm" -t "$REGISTRY/nanochat:$TAG" "$NANOCHAT_DIR"
    docker tag "$REGISTRY/nanochat:$TAG" "$REGISTRY/nanochat:rocm"
    docker push "$REGISTRY/nanochat:$TAG"
    docker push "$REGISTRY/nanochat:rocm"
    echo -e "\nDone! Pushed $REGISTRY/nanochat:$TAG and :rocm"
    exit 0
fi

echo "Building nanochat ROCm image ($TAG)..."
docker build -f "$NANOCHAT_DIR/Dockerfile.rocm" -t "$REGISTRY/nanochat:$TAG" "$NANOCHAT_DIR"
docker tag "$REGISTRY/nanochat:$TAG" "$REGISTRY/nanochat:rocm"

echo "Building nanochat CUDA image..."
docker build -f "$NANOCHAT_DIR/Dockerfile.cuda" -t "$REGISTRY/nanochat:cuda" "$NANOCHAT_DIR"

echo "Pushing images to Harbor..."
docker push "$REGISTRY/nanochat:$TAG"
docker push "$REGISTRY/nanochat:rocm"
docker push "$REGISTRY/nanochat:cuda"

echo -e "\nDone! Images pushed:"
echo "  $REGISTRY/nanochat:$TAG"
echo "  $REGISTRY/nanochat:rocm"
echo "  $REGISTRY/nanochat:cuda"
