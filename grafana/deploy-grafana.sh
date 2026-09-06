#!/usr/bin/env bash
set -euo pipefail

GRAFANA_DIR="${GRAFANA_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
NAMESPACE="grafana"
HELM_DEPLOY="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/scripts/helm-deploy.py"

case "${1:-}" in
--dry-run)
	[[ $# -eq 1 ]] || exit 2
	python3 "$HELM_DEPLOY" kube-prometheus-stack "$NAMESPACE" \
		oci://ghcr.io/prometheus-community/charts/kube-prometheus-stack \
		--dry-run -f "$GRAFANA_DIR/helm/kube-prometheus-stack-values.yaml"
	python3 "$HELM_DEPLOY" k8s-monitoring "$NAMESPACE" grafana/k8s-monitoring \
		--dry-run -f "$GRAFANA_DIR/helm/k8s-monitoring-values.yaml"
	python3 "$HELM_DEPLOY" loki "$NAMESPACE" grafana/loki \
		--dry-run -f "$GRAFANA_DIR/helm/loki-values.yaml"
	exit 0
	;;
"") [[ $# -eq 0 ]] || exit 2 ;;
*)
	echo "Usage: $0 [--dry-run]" >&2
	exit 2
	;;
esac

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

# The StorageClass must exist BEFORE the Helm releases: all three observability
# volumeClaimTemplates request `longhorn-observability`, and a PVC naming a class
# that does not exist stays Pending indefinitely rather than failing loudly.
# Applying it here keeps a fresh/rebuilt cluster working without manual steps.
if [ ! -f "$GRAFANA_DIR/manifests/storageclass-longhorn-observability.yaml" ]; then
	echo "storageclass-longhorn-observability.yaml file not found!"
	exit 1
fi

echo -e "\nApplying longhorn-observability StorageClass..."
kubectl apply -f "$GRAFANA_DIR/manifests/storageclass-longhorn-observability.yaml"

echo -e "\nDeploying kube-prometheus-stack..."
python3 "$HELM_DEPLOY" kube-prometheus-stack "$NAMESPACE" \
	oci://ghcr.io/prometheus-community/charts/kube-prometheus-stack \
	--create-namespace \
	-f "$GRAFANA_DIR/helm/kube-prometheus-stack-values.yaml"

echo -e "\nDeploying k8s-monitoring..."
python3 "$HELM_DEPLOY" k8s-monitoring "$NAMESPACE" \
	grafana/k8s-monitoring \
	-f "$GRAFANA_DIR/helm/k8s-monitoring-values.yaml"

echo -e "\nDeploying loki..."
python3 "$HELM_DEPLOY" loki "$NAMESPACE" \
	grafana/loki \
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

if [ ! -f "$GRAFANA_DIR/manifests/backup-alerts.yaml" ]; then
	echo "backup-alerts.yaml file not found!"
	exit 1
fi

# The grafana namespace no longer runs its own cloudflared connector. Its tunnel
# was deleted 2026-03-28 in the consolidation into the cluster-wide `homelab`
# tunnel, which has served logs.nathanwhyte.dev ever since; the Deployment and
# its grafana-cloudflared-token Secret were removed 2026-07-20 after ~4 months
# of "Unauthorized: Tunnel not found" retries (IMPR-1028, INFO-1120).
#
# Dropping manifests/cloudflared.yaml does not remove what a previous run
# created — `kubectl apply` never deletes resources absent from its input — so
# clean up explicitly. Idempotent; a no-op where they were never applied.
echo -e "\nRemoving the retired grafana cloudflared connector, if present..."
kubectl delete deployment cloudflared -n "$NAMESPACE" --ignore-not-found
kubectl delete serviceaccount cloudflared -n "$NAMESPACE" --ignore-not-found
kubectl delete secret grafana-cloudflared-token -n "$NAMESPACE" --ignore-not-found

echo -e "\nApplying other manifests..."
kubectl apply \
	-f "$GRAFANA_DIR/manifests/pvc-rwx.yaml" \
	-f "$GRAFANA_DIR/manifests/rbac.yaml" \
	-f "$GRAFANA_DIR/manifests/node-power-alerts.yaml" \
	-f "$GRAFANA_DIR/manifests/backup-alerts.yaml"

# May also be run independently to avoid touching the Helm releases.
bash "$GRAFANA_DIR/../longhorn/deploy-storage-alerts.sh"
bash "$GRAFANA_DIR/deploy-backup-alerts.sh"

echo -e "\nDone! Visit:"
echo "  https://logs.nathanwhyte.dev/            (public, via the homelab tunnel)"
echo "  k8s dashboard: LAN/tailnet only since IMPR-1029 — see reference/external-routes.md"
