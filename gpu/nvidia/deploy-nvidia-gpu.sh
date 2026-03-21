#!/usr/bin/env bash
set -euo pipefail

NVIDIA_GPU_DIR="$HOME/code/homelab/gpu/nvidia"
NAMESPACE="gpu-operator"

if [ ! -x "$(command -v "kubectl")" ]; then
    echo "kubectl not installed."
    exit 1
fi

if ! kubectl cluster-info > /dev/null 2>&1; then
    echo "kubectl not connected to a cluster."
    exit 1
fi

if [ ! -x "$(command -v "helm")" ]; then
    echo "helm not installed."
    exit 1
fi

if [ ! -f "$NVIDIA_GPU_DIR/values.yaml" ]; then
    echo "values.yaml not found!"
    exit 1
fi

# Check that GPU nodes have expected labels
for node in manu wemby; do
    if ! kubectl get node "$node" --show-labels 2>/dev/null | grep -q "gpu.vendor=nvidia"; then
        echo "WARNING: Node '$node' does not have label gpu.vendor=nvidia"
        echo "  kubectl label node $node gpu=true gpu.vendor=nvidia"
    fi
done

echo "Updating helm repositories..."
helm repo add nvidia https://helm.ngc.nvidia.com/nvidia
helm repo update nvidia

echo -e "\nDeploying NVIDIA GPU Operator..."
helm upgrade --install gpu-operator nvidia/gpu-operator \
    --create-namespace \
    --namespace "$NAMESPACE" \
    -f "$NVIDIA_GPU_DIR/values.yaml"

echo -e "\nWaiting for GPU operator rollout..."
kubectl rollout status deployment/gpu-operator -n "$NAMESPACE" --timeout=180s || true

echo -e "\nDone!"
echo "Verify GPU resources are registered:"
echo "  kubectl describe node manu | grep nvidia.com/gpu"
echo "  kubectl describe node wemby | grep nvidia.com/gpu"
echo ""
echo "Run smoke test:"
echo "  kubectl apply -f $NVIDIA_GPU_DIR/cuda-vectoradd.yaml"
echo "  kubectl wait --for=condition=Complete job/cuda-vectoradd --timeout=120s"
echo "  kubectl logs job/cuda-vectoradd"
echo "  kubectl delete job cuda-vectoradd"
