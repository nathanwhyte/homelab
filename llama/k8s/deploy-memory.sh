#!/usr/bin/env bash

set -euo pipefail

LLAMA_DIR="${LLAMA_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
MEMORY_DIR="$LLAMA_DIR/memory"
NAMESPACE="llama"

if [ ! -x "$(command -v kubectl)" ]; then
  echo "kubectl not installed."
  exit 1
fi

if ! kubectl cluster-info > /dev/null 2>&1; then
  echo "kubectl not connected to a cluster."
  exit 1
fi

for file in namespace.yaml memory/kustomization.yaml; do
  if [ ! -f "$LLAMA_DIR/$file" ]; then
    echo "$file not found!"
    exit 1
  fi
done

echo
echo "Deploying llama memory stack..."

kubectl apply -f "$LLAMA_DIR/namespace.yaml"
kubectl apply -k "$MEMORY_DIR"

echo
echo "Waiting for memory components to be ready..."
kubectl rollout status deployment/embedder-cpu -n "$NAMESPACE" --timeout=900s
kubectl rollout status deployment/memory-service -n "$NAMESPACE" --timeout=900s
kubectl rollout status statefulset/qdrant -n "$NAMESPACE" --timeout=900s

echo
echo "Done!"
echo "  Memory API: http://memory-service.$NAMESPACE.svc.cluster.local"
echo "  Qdrant:     http://qdrant.$NAMESPACE.svc.cluster.local:6333"
echo
echo "Check status:"
echo "  kubectl get pods -n $NAMESPACE"
echo "  kubectl logs -n $NAMESPACE deploy/memory-service"
