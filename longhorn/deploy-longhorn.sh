#!/usr/bin/env bash
set -euo pipefail

LONGHORN_DIR="$(cd "$(dirname "$0")" && pwd)"
NAMESPACE="longhorn-system"
CHART_VERSION="1.11.0"

if ! command -v kubectl &>/dev/null; then
    echo "kubectl not installed."
    exit 1
fi

if ! kubectl cluster-info &>/dev/null; then
    echo "kubectl not connected to a cluster."
    exit 1
fi

if ! command -v helm &>/dev/null; then
    echo "helm not installed."
    exit 1
fi

echo "Deploying Longhorn v${CHART_VERSION} via Helm..."

# Ensure repo is available
helm repo add longhorn https://charts.longhorn.io 2>/dev/null || true
helm repo update longhorn

# Install or upgrade Longhorn
helm upgrade --install longhorn longhorn/longhorn \
  --namespace "$NAMESPACE" --create-namespace \
  --version "$CHART_VERSION" \
  -f "$LONGHORN_DIR/longhorn-values.yaml"

echo -e "\nWaiting for Longhorn Manager to be ready..."
kubectl wait --for=condition=ready pod -l app=longhorn-manager \
  -n "$NAMESPACE" --timeout=300s || true

echo -e "\nApplying custom storage classes..."
kubectl apply -f "$LONGHORN_DIR/storage.yaml"

echo -e "\nApplying Longhorn UI extras (cloudflared, basic auth middleware)..."
kubectl apply -f "$LONGHORN_DIR/ui.yaml"

echo -e "\nDone!"
echo "  Web UI: https://longhorn.nathanwhyte.dev"
echo "  Credentials: admin / <see longhorn-auth-secret>"

echo -e "\nCheck status:"
echo "  kubectl get pods -n $NAMESPACE"
echo "  kubectl get storageclass | grep longhorn"
echo "  helm status longhorn -n $NAMESPACE"

echo -e "\nStorage classes:"
echo "  longhorn       (default, 1 replica)"
echo "  longhorn-hdd   (HDD, ethernet nodes, 1 replica)"
echo "  longhorn-ssd   (SSD, 1 replica)"
echo "  longhorn-nvme  (NVMe, 1 replica)"
echo "  longhorn-ethernet (ethernet nodes, 1 replica)"
echo "  longhorn-db    (SSD, 3 replicas)"
echo "  longhorn-harbor (HDD, ethernet, 2 replicas, NFS)"
