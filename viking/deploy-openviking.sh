#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== Deploying OpenViking to viking namespace ==="

# Create namespace
kubectl apply -f "$SCRIPT_DIR/namespace.yaml"

# PVCs first
kubectl apply -f "$SCRIPT_DIR/openviking-pvc.yaml"
kubectl apply -f "$SCRIPT_DIR/wemby-model-cache-pvc.yaml"

# Config
kubectl apply -f "$SCRIPT_DIR/openviking-configmap.yaml"

# Deployments and services
kubectl apply -f "$SCRIPT_DIR/embedder-llamacpp-deployment.yaml"
kubectl apply -f "$SCRIPT_DIR/embedder-llamacpp-service.yaml"
kubectl apply -f "$SCRIPT_DIR/openviking-deployment.yaml"
kubectl apply -f "$SCRIPT_DIR/openviking-service.yaml"

echo ""
echo "=== Waiting for rollouts ==="
kubectl rollout status deployment/embedder-llamacpp -n viking --timeout=300s &
kubectl rollout status deployment/openviking -n viking --timeout=120s &
wait

echo ""
echo "=== OpenViking deployed ==="
echo "Internal: http://openviking.viking.svc.cluster.local:1933"
echo "Health:   kubectl exec -n viking deploy/openviking -- curl -s http://localhost:1933/health"
