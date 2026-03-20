#!/usr/bin/env bash
set -euo pipefail

BENCH_DIR="$(cd "$(dirname "$0")" && pwd)"
NAMESPACE="sysbench"

if [ ! -x "$(command -v kubectl)" ]; then
    echo "kubectl not installed."
    exit 1
fi

if ! kubectl cluster-info > /dev/null 2>&1; then
    echo "kubectl not connected to a cluster."
    exit 1
fi

echo "Deploying sysbench suite on timmy..."

# Create namespace
kubectl apply -f "$BENCH_DIR/namespace.yaml"

# Clean up any previous runs
kubectl delete job sysbench-timmy -n "$NAMESPACE" --ignore-not-found
kubectl delete job gpu-bench-timmy -n "$NAMESPACE" --ignore-not-found

# Launch jobs
kubectl apply -f "$BENCH_DIR/sysbench-job.yaml"
kubectl apply -f "$BENCH_DIR/gpu-bench-job.yaml"

echo ""
echo "Jobs launched! Monitor with:"
echo "  kubectl get jobs -n $NAMESPACE"
echo ""
echo "Watch sysbench (CPU/memory/fileio):"
echo "  kubectl logs -n $NAMESPACE job/sysbench-timmy -f"
echo ""
echo "Watch GPU benchmark:"
echo "  kubectl logs -n $NAMESPACE job/gpu-bench-timmy -f"
echo ""
echo "To re-run, execute this script again (it cleans up previous runs)."
