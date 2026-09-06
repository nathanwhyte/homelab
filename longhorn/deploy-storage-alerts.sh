#!/usr/bin/env bash
set -euo pipefail

LONGHORN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GRAFANA_DIR="$LONGHORN_DIR/../grafana"
dry_run=(--dry-run=none)
case "${1:-}" in
--dry-run) dry_run=(--dry-run=server) ;;
"") ;;
*)
	echo "Usage: $0 [--dry-run]" >&2
	exit 2
	;;
esac
if (($# > 1)); then
	echo "Usage: $0 [--dry-run]" >&2
	exit 2
fi

# Additive monitoring only: no Helm invocation or stateful workload restart.
kubectl apply "${dry_run[@]}" -f "$LONGHORN_DIR/servicemonitor.yaml"
kubectl apply "${dry_run[@]}" -f "$LONGHORN_DIR/alerts.yaml"
kubectl apply "${dry_run[@]}" -f "$GRAFANA_DIR/manifests/storage-alert-routing.yaml"
# Match the Helm values, preserving namespace isolation outside grafana.
kubectl -n grafana patch alertmanager prom-alertmanager --type=merge \
	"${dry_run[@]}" \
	-p '{"spec":{"alertmanagerConfigMatcherStrategy":{"type":"OnNamespaceExceptForAlertmanagerNamespace"}}}'
