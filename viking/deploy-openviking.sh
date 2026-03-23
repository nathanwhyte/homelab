#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== Deploying OpenViking to viking namespace ==="

# Create namespace
kubectl apply -f "$SCRIPT_DIR/namespace.yaml"

# PVCs first
kubectl apply -f "$SCRIPT_DIR/openviking-pvc.yaml"
# wemby-model-cache PVC no longer needed (embedder uses emptyDir)
# kubectl apply -f "$SCRIPT_DIR/wemby-model-cache-pvc.yaml"

# Config
kubectl apply -f "$SCRIPT_DIR/openviking-configmap.yaml"

# S3 credentials for AGFS backend
if [ -f "$SCRIPT_DIR/openviking-s3-credentials.secret.yaml" ]; then
    echo "Applying S3 credentials secret..."
    kubectl apply -f "$SCRIPT_DIR/openviking-s3-credentials.secret.yaml"
else
    echo "S3 credentials secret not found."
    echo "  AGFS will fail to start until the secret exists."
    echo "  Copy viking/openviking-s3-credentials.secret.yaml.example to"
    echo "  viking/openviking-s3-credentials.secret.yaml and fill in Garage credentials."
fi

# Auth secret for Traefik basicAuth ingress
if [ -f "$SCRIPT_DIR/openviking-auth.secret.yaml" ]; then
    echo "Applying auth secret..."
    kubectl apply -f "$SCRIPT_DIR/openviking-auth.secret.yaml"
else
    echo "Auth secret not found."
    echo "  Ingress will reject all requests until the secret exists."
    echo "  See viking/openviking-auth.secret.yaml.example for instructions."
fi

# Deployments and services
kubectl apply -f "$SCRIPT_DIR/embedder-llamacpp-deployment.yaml"
kubectl apply -f "$SCRIPT_DIR/embedder-llamacpp-service.yaml"
kubectl apply -f "$SCRIPT_DIR/openviking-deployment.yaml"
kubectl apply -f "$SCRIPT_DIR/openviking-service.yaml"

# Traefik ingress (context.nathanwhyte.dev with basicAuth)
kubectl apply -f "$SCRIPT_DIR/openviking-ingress.yaml"

echo ""
echo "=== Waiting for rollouts ==="
kubectl rollout status deployment/embedder-llamacpp -n viking --timeout=300s &
kubectl rollout status deployment/openviking -n viking --timeout=120s &
wait

echo ""
echo "=== OpenViking deployed ==="
echo "Internal: http://openviking.viking.svc.cluster.local:1933"
echo "LAN:      https://context.nathanwhyte.dev"
echo "Auth:     Basic api:<token from openviking-auth.secret.yaml>"
echo "Health:   curl -u api:\$TOKEN -H 'X-API-Key: \$OPENVIKING_KEY' https://context.nathanwhyte.dev/health"
