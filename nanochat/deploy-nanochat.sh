#!/usr/bin/env bash
set -euo pipefail

NANOCHAT_DIR="$HOME/code/homelab/nanochat"

if [ ! -x "$(command -v "kubectl")" ]; then
    echo "kubectl not installed."
    exit 1
fi

if ! kubectl cluster-info > /dev/null 2>&1; then
    echo "kubectl not connected to a cluster."
    exit 1
fi

echo -e "\nDeploying nanochat namespace and PVCs..."

kubectl apply -f "$NANOCHAT_DIR/namespace.yaml"
kubectl apply -f "$NANOCHAT_DIR/pvcs.yaml"

echo -e "\nDone! Namespace and PVCs created."

echo -e "\nNext steps:"
echo "  1. Build and push images:"
echo "     bash $NANOCHAT_DIR/build.sh"
echo ""
echo "  2. Scale down llama.cpp inference on timmy before training:"
echo "     kubectl scale deployment/llamacpp-rocm -n llama --replicas=0"
echo ""
echo "  3. Submit training job:"
echo "     kubectl apply -f $NANOCHAT_DIR/train-rocm-job.yaml"
echo ""
echo "  4. Watch training logs:"
echo "     kubectl logs -f job/nanochat-gpt2-rocm -n nanochat"
echo ""
echo "  5. After training, restore inference:"
echo "     kubectl scale deployment/llamacpp-rocm -n llama --replicas=1"
