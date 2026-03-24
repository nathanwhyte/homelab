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
echo "  1. Set up WandB secret:"
echo "     cp $NANOCHAT_DIR/wandb.secret.yaml.example $NANOCHAT_DIR/wandb.secret.yaml"
echo "     # Edit wandb.secret.yaml with your API key from https://wandb.ai/authorize"
echo "     kubectl apply -f $NANOCHAT_DIR/wandb.secret.yaml"
echo ""
echo "  2. Build and push images:"
echo "     bash $NANOCHAT_DIR/build.sh --rocm-only"
echo ""
echo "  3. Scale down llama.cpp inference on timmy before training:"
echo "     kubectl scale deployment/llamacpp-rocm -n viking --replicas=0"
echo ""
echo "  4. Submit training job:"
echo "     kubectl apply -f $NANOCHAT_DIR/train-rocm-job.yaml"
echo ""
echo "  5. Watch training logs:"
echo "     kubectl logs -f job/nanochat-gpt2-rocm -n nanochat"
echo ""
echo "  6. After training, restore inference:"
echo "     kubectl scale deployment/llamacpp-rocm -n viking --replicas=1"
