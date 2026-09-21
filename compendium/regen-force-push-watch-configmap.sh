#!/usr/bin/env bash
# Regenerate compendium/main-force-push-watch-configmap.yaml from the canonical
# compendium/main-force-push-watch.py source (compendium BUG-1160).
#
# Mirrors viking/scripts/regen-janitor-configmap.sh. Run this after any edit to
# the .py and commit both files together — the ConfigMap is what the pod
# actually executes, so an unregenerated edit ships nothing.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
kubectl create configmap main-force-push-watch \
	-n compendium \
	--from-file=main-force-push-watch.py="$DIR/main-force-push-watch.py" \
	--dry-run=client -o yaml \
	>"$DIR/main-force-push-watch-configmap.yaml"
echo "Regenerated $DIR/main-force-push-watch-configmap.yaml"
