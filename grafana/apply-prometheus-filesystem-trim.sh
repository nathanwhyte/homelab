#!/usr/bin/env bash
set -euo pipefail

GRAFANA_DIR="${GRAFANA_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
NAMESPACE="grafana"
PVC="prometheus-prom-prometheus-db-prometheus-prom-prometheus-0"
JOB="prometheus-filesystem-trim-weekly"
MANIFEST="$GRAFANA_DIR/manifests/prometheus-filesystem-trim.yaml"
WAIT_ATTEMPTS="${WAIT_ATTEMPTS:-60}"
WAIT_SECONDS="${WAIT_SECONDS:-5}"

wait_for_pvc() {
  local attempt

  for ((attempt = 1; attempt <= WAIT_ATTEMPTS; attempt++)); do
    if kubectl -n "$NAMESPACE" get pvc "$PVC" >/dev/null 2>&1; then
      return 0
    fi

    if ((attempt < WAIT_ATTEMPTS)); then
      sleep "$WAIT_SECONDS"
    fi
  done

  echo "Timed out waiting for PVC $NAMESPACE/$PVC after $((WAIT_ATTEMPTS * WAIT_SECONDS)) seconds" >&2
  return 1
}

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

# Longhorn replaces a volume's recurring-job selection with its PVC source
# labels once `source=enabled`. Preserve the inherited default group explicitly
# while adding this direct job. The trim job itself has `groups: []`, so it does
# not select the other volumes in that group.
kubectl apply -f "$MANIFEST"
wait_for_pvc
kubectl -n "$NAMESPACE" label pvc "$PVC" \
  recurring-job.longhorn.io/source=enabled \
  recurring-job-group.longhorn.io/default=enabled \
  "recurring-job.longhorn.io/$JOB=enabled" \
  --overwrite

echo "Applied $JOB and labelled $NAMESPACE/$PVC for its filesystem-trim schedule."
