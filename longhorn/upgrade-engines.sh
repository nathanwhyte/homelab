#!/usr/bin/env bash
set -euo pipefail

# Live-upgrade every attached Longhorn volume to the engine image of TARGET.
# Run after the chart upgrade has deployed that engine image (see upgrading.md).
# Detached volumes are skipped; Longhorn upgrades them on their next attach.
#
# Usage: upgrade-engines.sh v1.12.1 [--dry-run]

TARGET="${1:-}"
DRY_RUN="${2:-}"
NAMESPACE="longhorn-system"

if [[ ! "$TARGET" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || [[ -n "$DRY_RUN" && "$DRY_RUN" != "--dry-run" ]]; then
  echo "Usage: $0 vX.Y.Z [--dry-run]" >&2
  exit 2
fi
TARGET_IMAGE="docker.io/longhornio/longhorn-engine:$TARGET"

# The engine image must be deployed and ready on every node before any volume
# points at it; a volume patched onto a missing image cannot start its engine.
EI_STATE=$(kubectl get engineimages.longhorn.io -n "$NAMESPACE" -o json | python3 -c "
import json, sys
target = sys.argv[1]
states = [ei['status'].get('state', '?') for ei in json.load(sys.stdin)['items'] if ei['spec'].get('image') == target]
print(states[0] if len(states) == 1 else 'missing')
" "$TARGET_IMAGE")
if [ "$EI_STATE" != "deployed" ]; then
  echo "Engine image $TARGET_IMAGE is '$EI_STATE', not 'deployed'. Upgrade the chart first." >&2
  exit 1
fi

echo "Longhorn engine upgrade: attached volumes -> $TARGET"
echo "================================================"

# name|state|namespace/pvc for every volume not already on the target image.
# Captured first so a kubectl failure stops the script under pipefail; read
# from a process substitution, a failure would look like "nothing to upgrade".
# A read loop, not mapfile: macOS still ships bash 3.2.
# Refuses outright if any volume already runs a NEWER engine: old engine images
# stay deployed during a rolling upgrade, and Longhorn's webhook checks image
# compatibility, not version order, so a stale target would patch volumes back.
VOL_ROWS=$(kubectl get volumes.longhorn.io -n "$NAMESPACE" -o json | python3 -c "
import json, re, sys
target = sys.argv[1]
def ver(image):
    m = re.search(r':v(\d+)\.(\d+)\.(\d+)$', image)
    return tuple(map(int, m.groups())) if m else None
want = ver(target)
rows, newer = [], []
for v in json.load(sys.stdin)['items']:
    cur = v['status'].get('currentImage', '')
    if cur == target:
        continue
    have = ver(cur)
    if have is not None and have > want:
        newer.append(f\"{v['metadata']['name']} ({cur})\")
    k = v['status'].get('kubernetesStatus', {})
    rows.append(f\"{v['metadata']['name']}|{v['status'].get('state', '?')}|{k.get('namespace', '?')}/{k.get('pvcName', '?')}\")
if newer:
    sys.exit('Refusing a downgrade; these volumes run a newer engine: ' + ', '.join(newer))
print('\n'.join(rows))
" "$TARGET_IMAGE")
OLD_VOLS=()
while IFS= read -r line; do
  [ -n "$line" ] && OLD_VOLS+=("$line")
done <<<"$VOL_ROWS"

if [ "${#OLD_VOLS[@]}" -eq 0 ]; then
  # `set -u` on bash 3.2 treats an empty array expansion as unbound, so this
  # exit also keeps the loop below from ever expanding an empty array.
  echo "All volumes are already on $TARGET."
  exit 0
fi

UPGRADED=0
SKIPPED=0
FAILED=0

# Not a pipeline: a `| while` loop runs in a subshell and loses the counters.
for row in "${OLD_VOLS[@]}"; do
  IFS='|' read -r vol state pvc <<<"$row"
  if [ "$state" != "attached" ]; then
    echo "[skip] $pvc ($state; upgrades on next attach)"
    SKIPPED=$((SKIPPED + 1))
    continue
  fi
  if [ "$DRY_RUN" = "--dry-run" ]; then
    echo "[would upgrade] $pvc"
    continue
  fi

  echo -n "[upgrading] $pvc ... "
  if kubectl patch volumes.longhorn.io "$vol" -n "$NAMESPACE" --type=json \
    -p "[{\"op\": \"replace\", \"path\": \"/spec/image\", \"value\": \"$TARGET_IMAGE\"}]" \
    >/dev/null; then
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
echo "Patched $UPGRADED, skipped $SKIPPED, failed $FAILED."
echo "Monitor progress:"
echo "  kubectl get volumes.longhorn.io -n $NAMESPACE -o custom-columns='PVC:.status.kubernetesStatus.pvcName,IMAGE:.status.currentImage,STATE:.status.state'"
echo "Once no volume reports the old image, its engineimage refCount is 0 and Longhorn"
echo "garbage-collects it (or delete it by hand: kubectl get engineimages -n $NAMESPACE)."
[ "$FAILED" -eq 0 ]
