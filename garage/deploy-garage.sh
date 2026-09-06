#!/usr/bin/env bash
set -euo pipefail

# Derive from the script's own location, matching deploy-grafana.sh. The previous
# hardcoded "$HOME/code/homelab/garage" broke when the repo became bare with
# sibling worktrees — that path no longer exists, so the script exited before
# reaching any of its work.
GARAGE_DIR="${GARAGE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
NAMESPACE="garage"
CHART_DIR="${GARAGE_CHART_DIR:-$GARAGE_DIR/garage/script/helm/garage}"
HELM_DEPLOY="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/scripts/helm-deploy.py"

case "${1:-}" in
--dry-run)
	[[ $# -eq 1 ]] || exit 2
	[[ -d "$CHART_DIR" ]] || {
		echo "Helm chart not found at $CHART_DIR" >&2
		exit 1
	}
	python3 "$HELM_DEPLOY" garage "$NAMESPACE" "$CHART_DIR" --dry-run -f "$GARAGE_DIR/garage-values.yaml"
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

# The StorageClass must exist BEFORE the Helm release: the data
# volumeClaimTemplate requests `longhorn-garage-data`, and a PVC naming a class
# that does not exist stays Pending indefinitely rather than failing loudly.
if [ ! -f "$GARAGE_DIR/storageclass-longhorn-garage-data.yaml" ]; then
	echo "storageclass-longhorn-garage-data.yaml file not found!"
	exit 1
fi

echo -e "\nApplying longhorn-garage-data StorageClass..."
kubectl apply -f "$GARAGE_DIR/storageclass-longhorn-garage-data.yaml"

python3 "$HELM_DEPLOY" garage "$NAMESPACE" \
	"$CHART_DIR" \
	--create-namespace \
	-f "$GARAGE_DIR/garage-values.yaml"

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
