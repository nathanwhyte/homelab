#!/usr/bin/env bash
set -euo pipefail

REGISTRY="registry.nathanwhyte.dev/robots"
IMAGE="$REGISTRY/summarizer-api"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

TAG="${1:-latest}"

echo "Building summarizer-api image..."
docker build -t "$IMAGE:$TAG" "$SCRIPT_DIR"

echo "Pushing to Harbor..."
docker push "$IMAGE:$TAG"

echo -e "\nDone! $IMAGE:$TAG pushed"
