#!/usr/bin/env bash
set -euo pipefail

HEADLAMP_DIR="$HOME/code/homelab/headlamp"
NAMESPACE="headlamp"

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

echo "Updating helm repositories..."
helm repo add headlamp https://kubernetes-sigs.github.io/headlamp/ 2>/dev/null || true
helm repo update headlamp

echo -e "\nDeploying Headlamp..."
helm upgrade --install headlamp headlamp/headlamp \
    --create-namespace \
    --namespace "$NAMESPACE" \
    -f "$HEADLAMP_DIR/values.yaml"

echo -e "\nApplying Headlamp manifests..."

if [ -f "$HEADLAMP_DIR/cloudflared.secret.yaml" ]; then
    kubectl apply -f "$HEADLAMP_DIR/cloudflared.secret.yaml"
else
    echo "cloudflared.secret.yaml not found."
    echo "Create this file and apply it, or manually create the cloudflare secret."
fi

if [ -f "$HEADLAMP_DIR/cloudflared.yaml" ]; then
    kubectl apply -f "$HEADLAMP_DIR/cloudflared.yaml"
fi

if [ -f "$HEADLAMP_DIR/user.yaml" ]; then
    kubectl apply -f "$HEADLAMP_DIR/user.yaml"
fi

echo -e "\nDone!"
echo "Create a service account token:"
echo "  kubectl create token headlamp -n $NAMESPACE"
