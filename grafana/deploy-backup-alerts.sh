#!/usr/bin/env bash
set -euo pipefail

GRAFANA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
dry_run=(--dry-run=none)
case "${1:-}" in
--dry-run) dry_run=(--dry-run=server) ;;
"") ;;
*) exit 2 ;;
esac
[[ $# -le 1 ]] || exit 2

# Provision this Secret separately; never print credential values.
kubectl -n grafana get secret backup-freshness-r2-credentials -o name
kubectl -n grafana create configmap backup-freshness-exporter \
	--from-file="$GRAFANA_DIR/backup-freshness-exporter.py" --dry-run=client -o yaml |
	kubectl apply "${dry_run[@]}" -f -
kubectl apply "${dry_run[@]}" -f "$GRAFANA_DIR/manifests/backup-freshness-exporter.yaml"
kubectl apply "${dry_run[@]}" -f "$GRAFANA_DIR/manifests/backup-alerts.yaml"
kubectl apply "${dry_run[@]}" -f "$GRAFANA_DIR/manifests/backup-alert-routing.yaml"
# Keep routing effective on a fresh cluster as well as the Phase 2 deployment.
kubectl -n grafana patch alertmanager prom-alertmanager --type=merge \
	"${dry_run[@]}" \
	-p '{"spec":{"alertmanagerConfigMatcherStrategy":{"type":"OnNamespaceExceptForAlertmanagerNamespace"}}}'
if [[ ${1:-} != --dry-run ]]; then
	# A changed ConfigMap does not restart the Python interpreter by itself.
	kubectl -n grafana rollout restart deployment backup-freshness-exporter
	kubectl -n grafana rollout status deployment backup-freshness-exporter --timeout=180s
fi
