#!/usr/bin/env bash
set -euo pipefail

LLAMA_DIR="${LLAMA_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
NAMESPACE="llama"

if [ ! -x "$(command -v "kubectl")" ]; then
    echo "kubectl not installed."
    exit 1
fi

if ! kubectl cluster-info > /dev/null 2>&1; then
    echo "kubectl not connected to a cluster."
    exit 1
fi

echo -e "\nDeploying llama.cpp ROCm server (AMD RX 9070 XT on timmy)..."
echo "  Primary model: Mistral-Small-24B-Instruct-2501 Q4_K_M (14.3 GB)"
echo "  Fallback model: Qwen3.5-27B Q3_K_M (13.5 GB)"

for file in namespace.yaml rocm-pvc.yaml rocm-llamacpp-ingress.yaml rocm-llamacpp-deployment.yaml rocm-llamacpp-service.yaml; do
    if [ ! -f "$LLAMA_DIR/$file" ]; then
        echo "$file not found!"
        exit 1
    fi
done

# Scale down other GPU workloads on timmy to free the AMD GPU
echo -e "\nScaling down competing GPU workloads on timmy..."
kubectl scale deployment/qwen-summarizer -n "$NAMESPACE" --replicas=0 2>/dev/null || true
kubectl scale deployment/llamacpp-rocm -n "$NAMESPACE" --replicas=0 2>/dev/null || true
sleep 5

kubectl apply -f "$LLAMA_DIR/namespace.yaml"
kubectl apply -f "$LLAMA_DIR/rocm-pvc.yaml"
kubectl apply -f "$LLAMA_DIR/rocm-llamacpp-ingress.yaml"
kubectl apply -f "$LLAMA_DIR/rocm-llamacpp-deployment.yaml"
kubectl apply -f "$LLAMA_DIR/rocm-llamacpp-service.yaml"

kubectl scale deployment/llamacpp-rocm -n "$NAMESPACE" --replicas=1

echo -e "\nWaiting for llama.cpp ROCm pod to become ready (model download may take a while)..."
kubectl wait --for=condition=available deployment/llamacpp-rocm -n "$NAMESPACE" --timeout=1200s || true

echo -e "\nDone!"
echo "  Backend: ROCm (flash attention enabled)"
echo "  GPU: AMD RX 9070 XT (HIP_VISIBLE_DEVICES=0, iGPU filtered)"
echo "  Cluster DNS:  http://llama-rocm-api.llama.svc.cluster.local"
echo "  Ingress:      http://llama.local (via Traefik)"
echo "  OpenAI API:   http://llama.local/v1"
echo "  Auth:         Bearer token required (see llama-rocm-api-key secret)"

echo -e "\nCheck status:"
echo "  kubectl get pods -n $NAMESPACE -l app=llamacpp-rocm"
echo "  kubectl logs -n $NAMESPACE deploy/llamacpp-rocm -f"

echo -e "\nTest the API:"
echo "  curl http://llama.local/v1/models -H 'Authorization: Bearer <token>'"

echo -e "\nNOTE: Scale down before running other GPU workloads on timmy:"
echo "  kubectl scale deployment/llamacpp-rocm -n llama --replicas=0"
