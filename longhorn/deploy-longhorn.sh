#!/usr/bin/env bash

LONGHORN_DIR="$HOME/code/homelab/longhorn"
NAMESPACE="longhorn-system"

if [ ! -x "$(command -v "kubectl")" ]; then
    echo "kubectl not installed."
    exit 1
fi

if ! kubectl cluster-info > /dev/null 2>&1; then
     echo "kubectl not connected to a cluster."
     exit 1
fi

echo -e "\nDeploying Longhorn distributed storage system..."

if [ ! -f "$LONGHORN_DIR/longhorn-values.yaml" ]; then
    echo "longhorn-values.yaml file not found!"
    exit 1
fi

if [ ! -f "$LONGHORN_DIR/storage.yaml" ]; then
    echo "storage.yaml file not found!"
    exit 1
fi

if [ ! -f "$LONGHORN_DIR/ui.yaml" ]; then
    echo "ui.yaml file not found!"
    exit 1
fi

echo "Applying Longhorn manifests..."
kubectl apply -f "$LONGHORN_DIR/longhorn-values.yaml"

echo -e "\nWaiting for Longhorn to be ready..."
echo "This may take a few minutes..."
kubectl wait --for=condition=ready pod -l app=longhorn-manager -n "$NAMESPACE" --timeout=300s || true

echo -e "\nApplying Longhorn storage classes..."
kubectl apply -f "$LONGHORN_DIR/storage.yaml"

echo -e "\nApplying Longhorn UI and ingress configuration..."
kubectl apply -f "$LONGHORN_DIR/ui.yaml"

echo -e "\nDone! Visit:"
echo "  Web UI: https://longhorn.nathanwhyte.dev"
echo "  Default credentials: admin / <see longhorn-auth-secret>"

echo -e "\nCheck status:"
echo "  kubectl get pods -n $NAMESPACE"
echo "  kubectl get storageclass | grep longhorn"
echo "  kubectl get ingress -n $NAMESPACE"

echo -e "\nAvailable Longhorn storage classes:"
echo "  - longhorn-hdd (HDD storage, 1 replica)"
echo "  - longhorn-ssd (SSD storage, 1 replica)"
echo "  - longhorn-nvme (NVMe storage, 1 replica)"
echo "  - longhorn-ethernet (Ethernet nodes, 1 replica)"
echo "  - longhorn-db (Database optimized, SSD, 3 replicas)"
