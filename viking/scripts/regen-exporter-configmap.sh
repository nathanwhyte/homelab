#!/usr/bin/env bash
# Regenerate viking/manifests/openviking-exporter-configmap.yaml from
# the canonical viking/exporters/ov-exporter.py source.
#
# Run this after any edit to ov-exporter.py and commit both files together.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
kubectl create configmap openviking-exporter \
	-n viking \
	--from-file=ov-exporter.py="$DIR/exporters/ov-exporter.py" \
	--dry-run=client -o yaml \
	>"$DIR/manifests/openviking-exporter-configmap.yaml"
echo "Regenerated $DIR/manifests/openviking-exporter-configmap.yaml"
