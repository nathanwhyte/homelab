#!/usr/bin/env bash

HARBOR_DIR="$HOME/code/homelab/harbor"
NAMESPACE="harbor"

if [ ! -x "$(command -v "kubectl")" ]; then
    echo "kubectl not installed."
    exit 1
fi

if [ ! kubectl cluster-info > /dev/null 2>&1 ]; then
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

if [ ! -f "$HARBOR_DIR/harbor-values.yaml" ]; then
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

kubectl apply -f "$HARBOR_DIR/namespace.yaml"
kubectl apply -f "$HARBOR_DIR/letsencrypt-issuer.yaml"
kubectl apply -f "$HARBOR_DIR/harbor-middleware.yaml"

helm upgrade --install harbor \
    harbor/harbor \
    --namespace "$NAMESPACE" \
    -f "$HARBOR_DIR/harbor-values.yaml"

echo -e "\nDone! Visit:"
echo "  Web UI: https://registry.nathanwhyte.dev"
echo "  Default credentials: admin / <CHANGE_ME>"
echo "  Docker login: docker login registry.nathanwhyte.dev"

echo -e "\nCheck status:"
echo "  kubectl get pods -n $NAMESPACE"
echo "  kubectl get ingress -n $NAMESPACE"
echo "  kubectl get certificate -n $NAMESPACE"
