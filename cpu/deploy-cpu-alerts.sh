#!/usr/bin/env bash
set -euo pipefail
# Apply the CPU temperature PrometheusRule and its Alertmanager route.
#
#   cpu/deploy-cpu-alerts.sh [--dry-run]
#
# Additive monitoring only: no Helm invocation, no workload restart.
#
# Both objects are required. The rule alone routes to the "null" receiver —
# see BUG-1129 — so applying only one leaves the alert silently undelivered.

CPU_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]:-$0}")" && pwd)"
GRAFANA_DIR="$CPU_DIR/../grafana"

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

kubectl apply "${dry_run[@]}" -f "$CPU_DIR/alerts.yaml"
kubectl apply "${dry_run[@]}" -f "$GRAFANA_DIR/manifests/hardware-alert-routing.yaml"
# Re-assert the strategy already set in the Helm values (idempotent merge
# patch), preserving namespace isolation outside grafana.
kubectl -n grafana patch alertmanager prom-alertmanager --type=merge \
	"${dry_run[@]}" \
	-p '{"spec":{"alertmanagerConfigMatcherStrategy":{"type":"OnNamespaceExceptForAlertmanagerNamespace"}}}'
