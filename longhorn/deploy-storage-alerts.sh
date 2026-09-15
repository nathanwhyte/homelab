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

# IMPR-1173: storage-alert-routing.yaml resolves its apiURL and bearer credentials
# from this Secret. Without it the CR passes server dry-run and then fails at
# operator reconcile, leaving the receiver silently unbuilt — so fail fast here.
# deploy-grafana.sh carries the same guard; this script is an explicitly supported
# standalone path and must not be able to bypass it (Codex P2 on #100).
if ! kubectl get secret alertmanager-slack-bot-token -n grafana >/dev/null 2>&1; then
	echo "alertmanager-slack-bot-token secret not found in namespace grafana."
	echo "Create it with the @newtbot bot token and the chat.postMessage endpoint:"
	echo "  kubectl create secret generic alertmanager-slack-bot-token -n grafana \\"
	echo "    --from-literal=bot-token='<xoxb-token>' \\"
	echo "    --from-literal=api-url='https://slack.com/api/chat.postMessage'"
	exit 1
fi

# Additive monitoring only: no Helm invocation or stateful workload restart.
kubectl apply "${dry_run[@]}" -f "$LONGHORN_DIR/servicemonitor.yaml"
kubectl apply "${dry_run[@]}" -f "$LONGHORN_DIR/alerts.yaml"
kubectl apply "${dry_run[@]}" -f "$GRAFANA_DIR/manifests/storage-alert-routing.yaml"
# Re-assert the strategy already set in the Helm values (idempotent merge
# patch), preserving namespace isolation outside grafana.
kubectl -n grafana patch alertmanager prom-alertmanager --type=merge \
	"${dry_run[@]}" \
	-p '{"spec":{"alertmanagerConfigMatcherStrategy":{"type":"OnNamespaceExceptForAlertmanagerNamespace"}}}'
