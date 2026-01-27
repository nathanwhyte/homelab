#!/usr/bin/env bash

NAMESPACE="grafana"

if [ ! -x "$(command -v "kubectl")" ]; then
    echo "kubectl not installed installed."
    exit 1
fi

if [ ! kubectl cluster-info > /dev/null 2>&1 ]; then
     echo "kubectl not connected to a cluster."
     exit 1
fi

if [ ! -x "$(command -v "helm")" ]; then
     echo "helm not installed installed."
     exit 1
fi

echo "Updating helm repositories..."
helm repo add grafana https://grafana.github.io/helm-charts
helm repo update grafana

echo -e "\nDeploying Grafana monitoring stack components..."

if [ ! -f "helm/kube-prometheus-stack-values.yaml" ]; then
    echo "kube-prometheus-stack-values.yaml file not found!"
    exit 1
fi

if [ ! -f "helm/k8s-monitoring-values.yaml" ]; then
    echo "k8s-monitoring-values.yaml file not found!"
    exit 1
fi

if [ ! -f "helm/loki-values.yaml" ]; then
    echo "loki-values.yaml file not found!"
    exit 1
fi

echo -e "\nDeploying kube-prometheus-stack..."
helm upgrade --install kube-prometheus-stack \
    oci://ghcr.io/prometheus-community/charts/kube-prometheus-stack \
    --namespace "$NAMESPACE" \
    -f "helm/kube-prometheus-stack-values.yaml"

echo -e "\nDeploying k8s-monitoring..."
helm upgrade --install k8s-monitoring \
    grafana/k8s-monitoring \
    -n "$NAMESPACE" \
    -f "helm/k8s-monitoring-values.yaml"

echo -e "\nDeploying loki..."
helm upgrade --install loki \
    grafana/loki \
    -n "$NAMESPACE" \
    -f "helm/loki-values.yaml"

if [ ! -f "manifests/rbac.yaml" ]; then
    echo "rbac.yaml file not found!"
    exit 1
fi

if [ ! -f "manifests/cloudflared.yaml" ]; then
     echo "cloudflared.yaml file not found!"
     exit 1
fi

if [ ! -f "cloudflared.secret.yaml" ]; then
     echo "cloudflared.secret.yaml file not found!"
     echo "Create this file and apply it, or manually create the cloudflare secret."
else
     kubectl apply -f cloudflared.secret.yaml
fi

echo -e "\nApplying other manifests..."
kubectl apply -f "manifests/rbac.yaml" -f "manifests/cloudflared.yaml"

echo -e "\nDone! Visit:"
echo "  https://k8s.nathanwhyte.dev/#/overview?namespace=grafana"
echo "  https://logs.nathanwhyte.dev/"
