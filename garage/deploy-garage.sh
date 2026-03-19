#!/usr/bin/env bash

GARAGE_DIR="$HOME/code/homelab/garage"
NAMESPACE="garage"

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

CHART_DIR="$GARAGE_DIR/garage/script/helm/garage"

echo "Deploying Garage S3-compatible storage..."

if [ ! -d "$CHART_DIR" ]; then
    echo "Helm chart not found at $CHART_DIR"
    echo "Clone the Garage repo: git clone https://git.deuxfleurs.fr/Deuxfleurs/garage $GARAGE_DIR/garage"
    exit 1
fi

if [ ! -f "$GARAGE_DIR/garage-values.yaml" ]; then
    echo "garage-values.yaml file not found!"
    exit 1
fi

if [ ! -f "$GARAGE_DIR/cloudflared.yaml" ]; then
    echo "cloudflared.yaml file not found!"
    exit 1
fi

helm upgrade --install garage \
    "$CHART_DIR" \
    --create-namespace \
    --namespace "$NAMESPACE" \
    -f "$GARAGE_DIR/garage-values.yaml"

if [ ! -f "$GARAGE_DIR/cloudflared.secret.yaml" ]; then
     echo "cloudflared.secret.yaml file not found!"
     echo "Create this file and apply it, or manually create the cloudflare secret."
else
     kubectl apply -f "$GARAGE_DIR/cloudflared.secret.yaml"
fi

echo -e "\nApplying cloudflared manifest..."
kubectl apply -f "$GARAGE_DIR/cloudflared.yaml"

echo -e "\nDeploying Garage Manager..."

if [ ! -f "$GARAGE_DIR/manager/garage-manager-config.yaml" ]; then
    echo "garage-manager-config.yaml file not found!"
    exit 1
fi

if [ ! -f "$GARAGE_DIR/manager/garage-manager.yaml" ]; then
    echo "garage-manager.yaml file not found!"
    exit 1
fi

if [ ! -f "$GARAGE_DIR/manager/garage-manager.secret.yaml" ]; then
     echo "garage-manager.secret.yaml file not found!"
     echo "Create this file and apply it, or manually create the garage manager secret."
else
     kubectl apply -f "$GARAGE_DIR/manager/garage-manager.secret.yaml"
fi

kubectl apply -f "$GARAGE_DIR/manager/garage-manager-config.yaml"
kubectl apply -f "$GARAGE_DIR/manager/garage-manager.yaml"

echo -e "\nDone! Visit:"
echo "  S3 API: https://uploads.nathanwhyte.dev"
echo "  Garage uses S3-compatible API"

echo -e "\nCheck status:"
echo "  kubectl get pods -n $NAMESPACE"
echo "  kubectl get statefulset -n $NAMESPACE"
echo "  kubectl get pvc -n $NAMESPACE"
echo "  kubectl get ingress -n $NAMESPACE"
