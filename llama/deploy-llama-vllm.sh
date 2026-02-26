#!/usr/bin/env bash

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

echo -e "\nDeploying llama vLLM (internal ClusterIP service)..."

if [ ! -f "$LLAMA_DIR/namespace.yaml" ]; then
    echo "namespace.yaml file not found!"
    exit 1
fi

if [ ! -f "$LLAMA_DIR/pvc.yaml" ]; then
    echo "pvc.yaml file not found!"
    exit 1
fi

if [ ! -f "$LLAMA_DIR/deployment.yaml" ]; then
    echo "deployment.yaml file not found!"
    exit 1
fi

if [ ! -f "$LLAMA_DIR/service.yaml" ]; then
    echo "service.yaml file not found!"
    exit 1
fi

kubectl apply -f "$LLAMA_DIR/namespace.yaml"
kubectl apply -f "$LLAMA_DIR/pvc.yaml"
kubectl apply -f "$LLAMA_DIR/deployment.yaml"
kubectl apply -f "$LLAMA_DIR/service.yaml"

echo -e "\nWaiting for llama pod to become ready..."
kubectl wait --for=condition=available deployment/llama -n "$NAMESPACE" --timeout=900s || true

echo -e "\nDone!"
echo "  Service DNS: http://llama-api.llama.svc.cluster.local"
echo "  OpenAI API base URL: http://llama-api.llama.svc.cluster.local/v1"

echo -e "\nCheck status:"
echo "  kubectl get pods -n $NAMESPACE"
echo "  kubectl get svc -n $NAMESPACE"
echo "  kubectl logs -n $NAMESPACE deploy/llama"

echo -e "\nQuick in-cluster test (example):"
echo "  kubectl run -it --rm curl --image=curlimages/curl:8.12.1 --restart=Never -- \\
    curl -sS http://llama-api.llama.svc.cluster.local/v1/completions \\
    -H 'Content-Type: application/json' \\
    -d '{\"model\":\"TinyLlama/TinyLlama-1.1B-Chat-v1.0\",\"prompt\":\"San Francisco is a\",\"max_tokens\":16}'"
