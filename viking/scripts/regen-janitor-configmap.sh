#!/usr/bin/env bash
# Regenerate viking/manifests/ovlock-janitor-configmap.yaml from the canonical
# viking/tools/ovlock-janitor.py + lock_observations.py and
# viking/exporters/s3lite.py sources (IMPR-1095). All land in the same
# ConfigMap directory so the janitor's imports resolve in-pod.
#
# Run this after any edit to these files and commit all of them together.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
kubectl create configmap ovlock-janitor \
	-n viking \
	--from-file=ovlock-janitor.py="$DIR/tools/ovlock-janitor.py" \
	--from-file=lock_observations.py="$DIR/tools/lock_observations.py" \
	--from-file=s3lite.py="$DIR/exporters/s3lite.py" \
	--dry-run=client -o yaml \
	>"$DIR/manifests/ovlock-janitor-configmap.yaml"
echo "Regenerated $DIR/manifests/ovlock-janitor-configmap.yaml"
