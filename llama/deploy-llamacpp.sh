#!/usr/bin/env bash
set -euo pipefail

LLAMA_DIR="$HOME/code/homelab/llama"
NAMESPACE="llama"

if [ ! -x "$(command -v "kubectl")" ]; then
    echo "kubectl not installed."
    exit 1
fi

if ! kubectl cluster-info > /dev/null 2>&1; then
    echo "kubectl not connected to a cluster."
    exit 1
fi

echo -e "\nDeploying llama.cpp CUDA server (NVIDIA GPU on manu)..."

for file in namespace.yaml pvc.yaml llamacpp-deployment.yaml llamacpp-service.yaml; do
    if [ ! -f "$LLAMA_DIR/$file" ]; then
        echo "$file not found!"
        exit 1
    fi
done

kubectl apply -f "$LLAMA_DIR/namespace.yaml"
kubectl apply -f "$LLAMA_DIR/pvc.yaml"
kubectl apply -f "$LLAMA_DIR/llamacpp-deployment.yaml"
kubectl apply -f "$LLAMA_DIR/llamacpp-service.yaml"

echo -e "\nWaiting for llama.cpp pod to become ready..."
kubectl wait --for=condition=available deployment/llamacpp-openai -n "$NAMESPACE" --timeout=1200s || true

echo -e "\nDone!"
echo "  Service DNS: http://llama-api.llama.svc.cluster.local"
echo "  OpenAI API base URL: http://llama-api.llama.svc.cluster.local/v1"

echo -e "\nCheck status:"
echo "  kubectl get pods -n $NAMESPACE -l app=llamacpp-openai"
echo "  kubectl logs -n $NAMESPACE deploy/llamacpp-openai"
