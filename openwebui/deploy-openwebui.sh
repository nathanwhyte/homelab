#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="openwebui"
OPENWEBUI_DIR="${OPENWEBUI_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
VALUES="$OPENWEBUI_DIR/helm/values.yaml"
HELM_DEPLOY="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/scripts/helm-deploy.py"

case "${1:-}" in
--dry-run)
	[[ $# -eq 1 ]] || exit 2
	python3 "$HELM_DEPLOY" open-webui "$NAMESPACE" open-webui/open-webui --dry-run -f "$VALUES"
	exit 0
	;;
"") [[ $# -eq 0 ]] || exit 2 ;;
*)
	echo "Usage: $0 [--dry-run]" >&2
	exit 2
	;;
esac

echo "Deploying Open WebUI to namespace: $NAMESPACE"

helm repo add open-webui https://open-webui.github.io/helm-charts 2>/dev/null || true
helm repo update open-webui

kubectl create namespace "$NAMESPACE" 2>/dev/null || true

python3 "$HELM_DEPLOY" open-webui "$NAMESPACE" open-webui/open-webui \
	-f "$VALUES"

echo ""
echo "Waiting for pods..."
kubectl get pods -n "$NAMESPACE"

echo ""
echo "Done! Visit: https://chat.nathanwhyte.dev"
