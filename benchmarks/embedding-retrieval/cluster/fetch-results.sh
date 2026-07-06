#!/usr/bin/env bash
# Retrieve overnight results from the bench-data PVC (TASK-1122).
#   ./cluster/fetch-results.sh [dest-dir]
# `kubectl cp` needs a RUNNING container and the runner pod is Completed once
# the matrix finishes, so this spins a short-lived helper pod that mounts the
# PVC, copies /data/results out, then removes the pod. Run this BEFORE
# `kubectl delete namespace bench` — the namespace delete takes the PVC (and
# the only copy of the results) with it.
set -euo pipefail
dest="${1:-./results-overnight}"

kubectl -n bench delete pod bench-fetch --ignore-not-found >/dev/null
kubectl -n bench run bench-fetch --restart=Never --image=busybox:1.37 \
	--overrides='{"spec":{"containers":[{"name":"bench-fetch","image":"busybox:1.37","command":["sleep","600"],"volumeMounts":[{"name":"data","mountPath":"/data"}]}],"volumes":[{"name":"data","persistentVolumeClaim":{"claimName":"bench-data"}}]}}' \
	>/dev/null
kubectl -n bench wait --for=condition=Ready pod/bench-fetch --timeout=120s
mkdir -p "$dest"
kubectl -n bench cp bench-fetch:/data/results "$dest"
kubectl -n bench delete pod bench-fetch >/dev/null
echo "results copied to $dest:"
ls -la "$dest"
