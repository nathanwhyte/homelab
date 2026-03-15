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

echo -e "\nDeploying llama.cpp ROCm server (AMD GPU on timmy)..."

for file in namespace.yaml rocm-pvc.yaml rocm-llamacpp-deployment.yaml rocm-llamacpp-service.yaml; do
    if [ ! -f "$LLAMA_DIR/$file" ]; then
        echo "$file not found!"
        exit 1
    fi
done

kubectl apply -f "$LLAMA_DIR/namespace.yaml"
kubectl apply -f "$LLAMA_DIR/rocm-pvc.yaml"
kubectl apply -f "$LLAMA_DIR/rocm-llamacpp-deployment.yaml"
kubectl apply -f "$LLAMA_DIR/rocm-llamacpp-service.yaml"

echo -e "\nWaiting for llama.cpp ROCm pod to become ready..."
kubectl wait --for=condition=available deployment/llamacpp-rocm -n "$NAMESPACE" --timeout=1200s || true

echo -e "\nDone!"
echo "  Service DNS: http://llama-rocm-api.llama.svc.cluster.local"
echo "  OpenAI API base URL: http://llama-rocm-api.llama.svc.cluster.local/v1"
echo "  Default requested model: current.gguf"

echo -e "\nCheck status:"
echo "  kubectl get pods -n $NAMESPACE"
echo "  kubectl logs -n $NAMESPACE deploy/llamacpp-rocm"

echo -e "\nQuick in-cluster test (example):"
echo "  kubectl run -it --rm curl --image=curlimages/curl:8.12.1 --restart=Never -- \\
    curl -sS http://llama-rocm-api.llama.svc.cluster.local/v1/chat/completions \\
    -H 'Content-Type: application/json' \\
    -d '{\"model\":\"current.gguf\",\"messages\":[{\"role\":\"user\",\"content\":\"Write one sentence about homelabs.\"}],\"max_tokens\":64}'"

echo -e "\nNOTE: The 9070XT has no MPS — scale down before training:"
echo "  kubectl scale deployment/llamacpp-rocm -n llama --replicas=0"
