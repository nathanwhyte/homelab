#!/bin/bash
# Migrate a single Longhorn volume's replica off timmy
# Usage: ./migrate-volume.sh <volume-name> <replica-name-on-timmy>
#
# Process:
#   1. Bump numberOfReplicas by 1
#   2. Wait for new replica to finish rebuilding (volume robustness = healthy)
#   3. Evict timmy's replica
#   4. Wait for eviction to complete
#   5. Restore original numberOfReplicas

set -euo pipefail

VOL="$1"
REPLICA="$2"
NS="longhorn-system"

echo "=== Migrating volume: ${VOL} ==="
echo "    Replica to evict: ${REPLICA}"

# Get current replica count
CURRENT=$(kubectl -n "$NS" get volumes.longhorn.io "$VOL" -o jsonpath='{.spec.numberOfReplicas}')
TARGET=$((CURRENT + 1))
echo "    Current replicas: ${CURRENT}, bumping to: ${TARGET}"

# Step 1: Bump replica count
kubectl -n "$NS" patch volumes.longhorn.io "$VOL" \
  --type=merge -p "{\"spec\":{\"numberOfReplicas\":${TARGET}}}"
echo "    [1/4] Replica count bumped to ${TARGET}"

# Step 2: Wait for volume to become healthy (new replica rebuilt)
echo "    [2/4] Waiting for rebuild..."
for i in $(seq 1 120); do
  ROBUSTNESS=$(kubectl -n "$NS" get volumes.longhorn.io "$VOL" -o jsonpath='{.status.robustness}')
  if [ "$ROBUSTNESS" = "healthy" ]; then
    echo "    [2/4] Volume is healthy after ${i}0 seconds"
    break
  fi
  if [ "$i" -eq 120 ]; then
    echo "    ERROR: Timed out waiting for healthy (20 min). Current: ${ROBUSTNESS}"
    exit 1
  fi
  sleep 10
done

# Step 3: Evict timmy's replica
kubectl -n "$NS" patch replicas.longhorn.io "$REPLICA" \
  --type=merge -p '{"spec":{"evictionRequested":true}}'
echo "    [3/4] Eviction requested for ${REPLICA}"

# Step 4: Wait for eviction (replica disappears or moves)
echo "    [4/4] Waiting for eviction..."
for i in $(seq 1 60); do
  if ! kubectl -n "$NS" get replicas.longhorn.io "$REPLICA" &>/dev/null; then
    echo "    [4/4] Replica evicted after ${i}0 seconds"
    break
  fi
  STATE=$(kubectl -n "$NS" get replicas.longhorn.io "$REPLICA" -o jsonpath='{.status.currentState}' 2>/dev/null || echo "gone")
  if [ "$STATE" = "gone" ]; then
    echo "    [4/4] Replica evicted"
    break
  fi
  if [ "$i" -eq 60 ]; then
    echo "    WARNING: Eviction taking long (10 min). State: ${STATE}"
  fi
  sleep 10
done

# Step 5: Restore original replica count
kubectl -n "$NS" patch volumes.longhorn.io "$VOL" \
  --type=merge -p "{\"spec\":{\"numberOfReplicas\":${CURRENT}}}"
echo "    Restored replica count to ${CURRENT}"
echo "=== Done: ${VOL} ==="
