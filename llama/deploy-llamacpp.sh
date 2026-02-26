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

echo -e "\nDeploying llama.cpp OpenAI-compatible server..."

for file in namespace.yaml pvc.yaml llamacpp-deployment.yaml llamacpp-service.yaml; do
    if [ ! -f "$LLAMA_DIR/$file" ]; then
        echo "$file not found!"
        exit 1
    fi
done

kubectl apply -f "$LLAMA_DIR/namespace.yaml"
kubectl apply -f "$LLAMA_DIR/pvc.yaml"

# Remove old vLLM deployment if present to free the single GPU.
kubectl delete deployment -n "$NAMESPACE" llama --ignore-not-found

kubectl apply -f "$LLAMA_DIR/llamacpp-deployment.yaml"
kubectl apply -f "$LLAMA_DIR/llamacpp-service.yaml"

echo -e "\nWaiting for llama.cpp pod to become ready..."
kubectl wait --for=condition=available deployment/llamacpp-openai -n "$NAMESPACE" --timeout=1200s || true

echo -e "\nDone!"
echo "  Service DNS: http://llama-api.llama.svc.cluster.local"
echo "  OpenAI API base URL: http://llama-api.llama.svc.cluster.local/v1"

echo -e "\nCheck status:"
echo "  kubectl get pods -n $NAMESPACE"
echo "  kubectl logs -n $NAMESPACE deploy/llamacpp-openai"

echo -e "\nQuick in-cluster test (example):"
echo "  kubectl run -it --rm curl --image=curlimages/curl:8.12.1 --restart=Never -- \\
    curl -sS http://llama-api.llama.svc.cluster.local/v1/chat/completions \\
    -H 'Content-Type: application/json' \\
    -d '{\"model\":\"tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf\",\"messages\":[{\"role\":\"user\",\"content\":\"Write one sentence about homelabs.\"}],\"max_tokens\":64}'"
