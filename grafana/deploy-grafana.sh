#!/usr/bin/env bash

GRAFANA_DIR="${GRAFANA_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
NAMESPACE="grafana"

if [ ! -x "$(command -v "kubectl")" ]; then
	echo "kubectl not installed."
	exit 1
fi

if ! kubectl cluster-info >/dev/null 2>&1; then
	echo "kubectl not connected to a cluster."
	exit 1
fi

if [ ! -x "$(command -v "helm")" ]; then
	echo "helm not installed."
	exit 1
fi

echo "Updating helm repositories..."
helm repo add grafana https://grafana.github.io/helm-charts
helm repo update grafana

echo -e "\nDeploying Grafana monitoring stack components..."

if ! kubectl get secret alertmanager-slack-webhook -n "$NAMESPACE" >/dev/null 2>&1; then
	echo "alertmanager-slack-webhook secret not found in namespace $NAMESPACE."
	echo "Create it with an incoming-webhook URL for #cron-homelab before deploying:"
	echo "  kubectl create secret generic alertmanager-slack-webhook -n $NAMESPACE --from-literal=api-url='<slack-webhook-url>'"
	exit 1
fi

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

if [ ! -f "$GRAFANA_DIR/manifests/pvc-rwx.yaml" ]; then
	echo "pvc-rwx.yaml file not found!"
	exit 1
fi

if [ ! -f "$GRAFANA_DIR/manifests/node-power-alerts.yaml" ]; then
	echo "node-power-alerts.yaml file not found!"
	exit 1
fi

# The grafana namespace no longer runs its own cloudflared connector. Its tunnel
# was deleted 2026-03-28 in the consolidation into the cluster-wide `homelab`
# tunnel, which has served logs.nathanwhyte.dev ever since; the Deployment and
# its grafana-cloudflared-token Secret were removed 2026-07-20 after ~4 months
# of "Unauthorized: Tunnel not found" retries (IMPR-1028, INFO-1120).

echo -e "\nApplying other manifests..."
kubectl apply \
	-f "$GRAFANA_DIR/manifests/pvc-rwx.yaml" \
	-f "$GRAFANA_DIR/manifests/rbac.yaml" \
	-f "$GRAFANA_DIR/manifests/node-power-alerts.yaml"

echo -e "\nDone! Visit:"
echo "  https://logs.nathanwhyte.dev/            (public, via the homelab tunnel)"
echo "  k8s dashboard: LAN/tailnet only since IMPR-1029 — see reference/external-routes.md"
