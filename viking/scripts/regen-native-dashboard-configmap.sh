#!/usr/bin/env bash

# Regenerate viking/manifests/openviking-native-dashboard-configmap.yaml from
# the official OpenViking demo dashboard JSON (examples/grafana/ in the
# upstream repo, pinned copy in viking/dashboards/openviking-native-demo.json).
# Run this after refreshing the JSON and commit both files together.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
kubectl create configmap openviking-native-demo-dashboard \
	-n grafana \
	--from-file=openviking-native-demo.json="$DIR/dashboards/openviking-native-demo.json" \
	--dry-run=client -o yaml |
	kubectl label --local -f - grafana_dashboard=1 --dry-run=client -o yaml \
		>"$DIR/manifests/openviking-native-dashboard-configmap.yaml"
echo "Regenerated $DIR/manifests/openviking-native-dashboard-configmap.yaml"
