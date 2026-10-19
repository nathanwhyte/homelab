#!/usr/bin/env bash
# Regenerate viking/manifests/embedder-prompt-shim-configmap.yaml from the
# canonical viking/tools/embedder-prompt-shim.py source (IMPR-1344).
#
# Mirrors regen-lock-watchdog-configmap.sh. Run this after any edit to the .py
# and commit both files together: the ConfigMap is what the pod actually
# executes, so an unregenerated edit ships nothing. Client-side only; nothing is
# applied.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
kubectl create configmap embedder-prompt-shim \
	-n viking \
	--from-file=embedder-prompt-shim.py="$DIR/tools/embedder-prompt-shim.py" \
	--dry-run=client -o yaml \
	>"$DIR/manifests/embedder-prompt-shim-configmap.yaml"
echo "Regenerated $DIR/manifests/embedder-prompt-shim-configmap.yaml"
