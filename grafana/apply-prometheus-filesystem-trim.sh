#!/usr/bin/env bash
set -euo pipefail

GRAFANA_DIR="${GRAFANA_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
NAMESPACE="grafana"
PVC="prometheus-prom-prometheus-db-prometheus-prom-prometheus-0"
JOB="prometheus-filesystem-trim-weekly"
MANIFEST="$GRAFANA_DIR/manifests/prometheus-filesystem-trim.yaml"

case "${1:-}" in
--dry-run)
  [[ $# -eq 1 ]] || exit 2
  kubectl apply --dry-run=server -f "$MANIFEST"
  exit 0
  ;;
"") [[ $# -eq 0 ]] || exit 2 ;;
*)
  echo "Usage: $0 [--dry-run]" >&2
  exit 2
  ;;
esac

[[ -f "$MANIFEST" ]] || { echo "Missing $MANIFEST" >&2; exit 1; }

# A direct recurring-job label is scoped to this PVC. `source=enabled` tells
# Longhorn to synchronize labels from the PVC to its volume; without it, labels
# on the PVC have no effect. Do not use the default recurring-job group: every
# Longhorn volume currently inherits it.
kubectl apply -f "$MANIFEST"
kubectl -n "$NAMESPACE" label pvc "$PVC" \
  recurring-job.longhorn.io/source=enabled \
  "recurring-job.longhorn.io/$JOB=enabled" \
  --overwrite

echo "Applied $JOB and labelled $NAMESPACE/$PVC for its filesystem-trim schedule."
