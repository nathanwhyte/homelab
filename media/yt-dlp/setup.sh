#!/usr/bin/env bash
# Bootstrap the IDEA-019 yt-dlp revival on the homelab K3s cluster.
#
# Applies (in dependency order):
#   1. Namespace
#   2. Longhorn node tag (one-time bootstrap of the wemby tag)
#   3. Dedicated StorageClass (longhorn-hdd-wemby: 2 replicas, pinned
#      to wemby via the new tag)
#   4. 200Gi media PVC
#   5. ConfigMaps (yt-dlp.conf + yt-dlp-playlists)
#   6. Validation CronJob (playlist 2, enabled)
#   7. Suspended CronJobs (playlists 1, 3, 4, 5, 6)
#
# This script is idempotent (kubectl apply is). Re-running after
# manifest edits re-converges the cluster to the on-disk YAML.
#
# Pre-requisites:
#   - kubectl context is pointed at the homelab cluster
#   - Longhorn is installed (provides the node.longhorn.io CRD)
#   - The image `nathanwhyte/yt-dlp:2026.06.10-yt-dlp` is built and
#     pushed to Docker Hub BEFORE the first Job runs (this script
#     doesn't build the image — see media/yt-dlp/docker/Dockerfile
#     and the README).
#
# Usage:
#   bash media/yt-dlp/setup.sh

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> namespace"
kubectl apply -f "$DIR/namespace.yaml"

echo "==> Longhorn node tag (one-time bootstrap of wemby)"
kubectl apply -f "$DIR/longhorn-node-wemby-tag.yaml"

echo "==> StorageClass (longhorn-hdd-wemby)"
kubectl apply -f "$DIR/longhorn-hdd-wemby-storageclass.yaml"

echo "==> media PVC (200Gi, RWO, wemby-pinned)"
kubectl apply -f "$DIR/media-pvc.yaml"

echo "==> ConfigMaps"
kubectl apply -f "$DIR/yt-dlp-config.yaml"
kubectl apply -f "$DIR/yt-dlp-playlists.yaml"

echo "==> CronJobs (validation + 5 suspended)"
kubectl apply -f "$DIR/cronjob-validation.yaml"
kubectl apply -f "$DIR/cronjobs.yaml"

echo
echo "Setup complete. Verify with:"
echo "  kubectl get pvc -n yt-dlp media"
echo "  kubectl get cronjobs -n yt-dlp"
echo
echo "To trigger the validation run immediately (don't wait for 02:10 UTC):"
echo "  kubectl create job --from=cronjob/yt-dlp-playlist-2 -n yt-dlp test-validation"
echo
echo "Tail the run with:"
echo "  kubectl logs -n yt-dlp -l job-name=test-validation -f"
