#!/usr/bin/env bash

GRAFANA_DIR="$HOME/code/homelab/grafana"
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

if [ ! -f "$GRAFANA_DIR/helm/kube-prometheus-stack-values.yaml" ]; then
    echo "kube-prometheus-stack-values.yaml file not found!"
    exit 1
fi

if [ ! -f "$GRAFANA_DIR/helm/k8s-monitoring-values.yaml" ]; then
    echo "k8s-monitoring-values.yaml file not found!"
    exit 1
fi

if [ ! -f "$GRAFANA_DIR/helm/loki-values.yaml" ]; then
    echo "loki-values.yaml file not found!"
    exit 1
fi

echo -e "\nDeploying kube-prometheus-stack..."
helm upgrade --install kube-prometheus-stack \
    oci://ghcr.io/prometheus-community/charts/kube-prometheus-stack \
    --create-namespace \
    --namespace "$NAMESPACE" \
    -f "$GRAFANA_DIR/helm/kube-prometheus-stack-values.yaml"

echo -e "\nDeploying k8s-monitoring..."
helm upgrade --install k8s-monitoring \
    grafana/k8s-monitoring \
    -n "$NAMESPACE" \
    -f "$GRAFANA_DIR/helm/k8s-monitoring-values.yaml"

echo -e "\nDeploying loki..."
helm upgrade --install loki \
    grafana/loki \
    -n "$NAMESPACE" \
    -f "$GRAFANA_DIR/helm/loki-values.yaml"

if [ ! -f "$GRAFANA_DIR/manifests/rbac.yaml" ]; then
    echo "rbac.yaml file not found!"
    exit 1
fi

if [ ! -f "$GRAFANA_DIR/manifests/cloudflared.yaml" ]; then
     echo "cloudflared.yaml file not found!"
     exit 1
fi

if [ ! -f "$GRAFANA_DIR/cloudflared.secret.yaml" ]; then
     echo "cloudflared.secret.yaml file not found!"
     echo "Create this file and apply it, or manually create the cloudflare secret."
else
     kubectl apply -f "$GRAFANA_DIR/cloudflared.secret.yaml"
fi

echo -e "\nApplying other manifests..."
kubectl apply -f "$GRAFANA_DIR/manifests/rbac.yaml" -f "$GRAFANA_DIR/manifests/cloudflared.yaml"

echo -e "\nDone! Visit:"
echo "  https://k8s.nathanwhyte.dev/#/overview?namespace=grafana"
echo "  https://logs.nathanwhyte.dev/"
