#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
HARBOR_DIR="$SCRIPT_DIR"
LONGHORN_DIR="$REPO_ROOT/longhorn"
NAMESPACE="harbor"
RELEASE_NAME="harbor"
CHART_REF="harbor/harbor"
VALUES_FILE="$HARBOR_DIR/harbor-values.yaml"
TIMEOUT="10m"

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
helm repo add harbor https://helm.goharbor.io
helm repo update harbor

echo -e "\nDeploying Harbor container registry..."

if [ ! -f "$VALUES_FILE" ]; then
    echo "harbor-values.yaml file not found!"
    exit 1
fi

if [ ! -f "$HARBOR_DIR/namespace.yaml" ]; then
    echo "namespace.yaml file not found!"
    exit 1
fi

if [ ! -f "$HARBOR_DIR/letsencrypt-issuer.yaml" ]; then
    echo "letsencrypt-issuer.yaml file not found!"
    exit 1
fi

if [ ! -f "$HARBOR_DIR/harbor-middleware.yaml" ]; then
    echo "harbor-middleware.yaml file not found!"
    exit 1
fi

if [ ! -f "$HARBOR_DIR/rwo-pvcs.yaml" ]; then
    echo "rwo-pvcs.yaml file not found!"
    exit 1
fi

kubectl apply -f "$LONGHORN_DIR/storage.yaml"
kubectl apply -f "$HARBOR_DIR/namespace.yaml"
kubectl apply -f "$HARBOR_DIR/letsencrypt-issuer.yaml"
kubectl apply -f "$HARBOR_DIR/harbor-middleware.yaml"
kubectl apply -f "$HARBOR_DIR/rwo-pvcs.yaml"

helm upgrade --install harbor \
    "$CHART_REF" \
    --namespace "$NAMESPACE" \
    --create-namespace \
    --wait \
    --timeout "$TIMEOUT" \
    -f "$VALUES_FILE"

echo -e "\nDone! Visit:"
echo "  Web UI: https://registry.nathanwhyte.dev"
echo "  Default credentials: admin / <CHANGE_ME>"
echo "  Docker login: docker login registry.nathanwhyte.dev"

echo -e "\nCheck status:"
echo "  kubectl get pods -n $NAMESPACE"
echo "  kubectl get ingress -n $NAMESPACE"
echo "  kubectl get certificate -n $NAMESPACE"
