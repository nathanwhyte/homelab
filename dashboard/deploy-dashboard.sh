#!/usr/bin/env bash

DASHBOARD_DIR="$HOME/code/homelab/dashboard"

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

helm repo add kubernetes-dashboard https://kubernetes.github.io/dashboard/

echo "Updating helm repositories..."
helm repo update kubernetes-dashboard

helm upgrade --install kubernetes-dashboard \
     kubernetes-dashboard/kubernetes-dashboard \
    --create-namespace --namespace kubernetes-dashboard

echo -e "\nApplying kubernetes-dashboard manifests..."

if [ ! -f "$DASHBOARD_DIR/ingress.yaml" ]; then
    echo "ingress.yaml file not found!"
    exit 1
fi

if [ ! -f "$DASHBOARD_DIR/user.yaml" ]; then
     echo "user.yaml file not found!"
     exit 1
fi

if [ ! -f "$DASHBOARD_DIR/cloudflared.yaml" ]; then
     echo "cloudflared.yaml file not found!"
     exit 1
fi

if [ ! -f "$DASHBOARD_DIR/cloudflared.secret.yaml" ]; then
     echo "cloudflared.secret.yaml file not found!"
     echo "Create this file and apply it, or manually create the cloudflare secret."
else
     kubectl apply -f "$DASHBOARD_DIR/cloudflared.secret.yaml"
fi

kubectl apply -f "$DASHBOARD_DIR/ingress.yaml" -f "$DASHBOARD_DIR/user.yaml" -f "$DASHBOARD_DIR/cloudflared.yaml"

echo -e "\nDone! Visit:"
echo "  https://k8s.nathanwhyte.dev/#/overview?namespace=kubernetes-dashboard"
