#!/usr/bin/env bash
# Regenerate viking/manifests/ov-lock-watchdog-configmap.yaml from the canonical
# viking/tools/ov-lock-watchdog.py + lock_observations.py sources (BUG-1220).
#
# Mirrors regen-janitor-configmap.sh. Run this after any edit to the .py and
# commit both files together: the ConfigMap is what the pod actually executes,
# so an unregenerated edit ships nothing. Client-side only; nothing is applied.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
kubectl create configmap ov-lock-watchdog \
	-n viking \
	--from-file=ov-lock-watchdog.py="$DIR/tools/ov-lock-watchdog.py" \
	--from-file=lock_observations.py="$DIR/tools/lock_observations.py" \
	--dry-run=client -o yaml \
	>"$DIR/manifests/ov-lock-watchdog-configmap.yaml"
echo "Regenerated $DIR/manifests/ov-lock-watchdog-configmap.yaml"
