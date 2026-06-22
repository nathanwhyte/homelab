#!/usr/bin/env bash
# Regenerate viking/manifests/openviking-dashboard-configmap.yaml from
# the canonical viking/dashboards/openviking-stack-health.json source.
#
# Run this after any edit to the dashboard JSON and commit both files together.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
kubectl create configmap openviking-stack-health-dashboard \
	-n grafana \
	--from-file=openviking-stack-health.json="$DIR/dashboards/openviking-stack-health.json" \
	--dry-run=client -o yaml |
	kubectl label --local -f - grafana_dashboard=1 -o yaml --dry-run=client \
		>"$DIR/manifests/openviking-dashboard-configmap.yaml"
echo "Regenerated $DIR/manifests/openviking-dashboard-configmap.yaml"
