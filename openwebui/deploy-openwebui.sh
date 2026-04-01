#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="openwebui"
VALUES="$HOME/code/homelab/openwebui/helm/values.yaml"

echo "Deploying Open WebUI to namespace: $NAMESPACE"

helm repo add open-webui https://open-webui.github.io/helm-charts 2>/dev/null || true
helm repo update open-webui

kubectl create namespace "$NAMESPACE" 2>/dev/null || true

helm upgrade --install open-webui open-webui/open-webui \
    --namespace "$NAMESPACE" \
    -f "$VALUES"

echo ""
echo "Waiting for pods..."
kubectl get pods -n "$NAMESPACE"

echo ""
echo "Done! Visit: https://chat.nathanwhyte.dev"
