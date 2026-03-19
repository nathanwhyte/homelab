#!/usr/bin/env bash
set -euo pipefail

# Upgrade all Longhorn volumes from v1.10.1 engine to v1.11.0
# Live engine upgrade is supported for attached volumes.
# Detached volumes are upgraded on next attach.

TARGET_IMAGE="docker.io/longhornio/longhorn-engine:v1.11.0"
NAMESPACE="longhorn-system"

echo "Longhorn Engine Upgrade: all volumes -> v1.11.0"
echo "================================================"

# Get all volumes still on v1.10.1
OLD_VOLS=$(kubectl get volumes.longhorn.io -n "$NAMESPACE" -o json | \
  python3 -c "
import json, sys
vols = json.load(sys.stdin)['items']
for v in vols:
    img = v['status'].get('currentImage', '')
    if 'v1.11.0' not in img:
        name = v['metadata']['name']
        state = v['status'].get('state', '?')
        pvc = v['status'].get('kubernetesStatus', {}).get('pvcName', '?')
        ns = v['status'].get('kubernetesStatus', {}).get('namespace', '?')
        print(f'{name}|{state}|{ns}/{pvc}')
")

if [ -z "$OLD_VOLS" ]; then
  echo "All volumes are already on v1.11.0!"
  exit 0
fi

TOTAL=$(echo "$OLD_VOLS" | wc -l)
echo "Found $TOTAL volumes to upgrade:"
echo "$OLD_VOLS" | while IFS='|' read -r vol state pvc; do
  printf "  %-8s %s\n" "$state" "$pvc"
done
echo ""

UPGRADED=0
SKIPPED=0
FAILED=0

echo "$OLD_VOLS" | while IFS='|' read -r vol state pvc; do
  if [ "$state" = "detached" ]; then
    echo "[skip] $pvc (detached — will upgrade on next attach)"
    SKIPPED=$((SKIPPED + 1))
    continue
  fi

  echo -n "[upgrading] $pvc ... "

  # Patch the volume spec to use the new engine image
  if kubectl patch volumes.longhorn.io "$vol" -n "$NAMESPACE" \
    --type=json \
    -p "[{\"op\": \"replace\", \"path\": \"/spec/image\", \"value\": \"$TARGET_IMAGE\"}]" \
    > /dev/null 2>&1; then
    echo "patched"
    UPGRADED=$((UPGRADED + 1))
  else
    echo "FAILED"
    FAILED=$((FAILED + 1))
  fi

  # Small delay between upgrades to avoid overwhelming the cluster
  sleep 2
done

echo ""
echo "Done. Upgrades are live — Longhorn will rolling-restart engines."
echo "Monitor progress:"
echo "  kubectl get volumes.longhorn.io -n $NAMESPACE -o custom-columns='PVC:.status.kubernetesStatus.pvcName,IMAGE:.status.currentImage,STATE:.status.state'"
echo ""
echo "Once all volumes show v1.11.0, the old engine image can be removed:"
echo "  kubectl delete engineimage ei-3154f3aa -n $NAMESPACE"
