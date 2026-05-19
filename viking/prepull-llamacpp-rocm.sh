#!/usr/bin/env bash
set -euo pipefail

# Pre-pull the llamacpp-rocm image on timmy to avoid slow image pulls during pod startup.
# In K3s, images are stored in containerd's snapshotter. Pre-pulling via `ctr` caches
# the image so that when the pod starts, the image is already available locally.

IMAGE="ghcr.io/ggml-org/llama.cpp:server-rocm"
NODE="timmy"

echo "Pre-pulling ${IMAGE} on node ${NODE}..."
echo "  This avoids slow image pulls during pod startup."
echo ""

# SSH into timmy and pull the image into K3s containerd
ssh -o ConnectTimeout=10 "${NODE}" \
  "sudo ctr -n k8s.io images pull ${IMAGE}"

echo ""
echo "Done. Image is now cached on ${NODE}."
echo "  Verify: ssh ${NODE} 'sudo ctr -n k8s.io images list | grep llama.cpp'"
