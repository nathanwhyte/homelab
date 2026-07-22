#!/usr/bin/env bash
# Regenerate viking/manifests/openviking-exporter-configmap.yaml from
# the canonical viking/exporters/ sources (ov-exporter.py + s3lite.py,
# the stdlib S3 helper the exporter imports for bucket-level lock polling
# — IMPR-1095).
#
# Run this after any edit to either file and commit all of them together.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
kubectl create configmap openviking-exporter \
	-n viking \
	--from-file=ov-exporter.py="$DIR/exporters/ov-exporter.py" \
	--from-file=s3lite.py="$DIR/exporters/s3lite.py" \
	--dry-run=client -o yaml \
	>"$DIR/manifests/openviking-exporter-configmap.yaml"
echo "Regenerated $DIR/manifests/openviking-exporter-configmap.yaml"
