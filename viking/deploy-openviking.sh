#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== Deploying OpenViking to viking namespace ==="

# Create namespace
kubectl apply -f "$SCRIPT_DIR/manifests/namespace.yaml"

# PVCs first
kubectl apply -f "$SCRIPT_DIR/manifests/openviking-pvc.yaml"
# wemby-model-cache PVC no longer needed (embedder uses emptyDir)
# kubectl apply -f "$SCRIPT_DIR/manifests/wemby-model-cache-pvc.yaml"

# Config
kubectl apply -f "$SCRIPT_DIR/manifests/openviking-configmap.yaml"

# S3 credentials for AGFS backend
if [ -f "$SCRIPT_DIR/manifests/openviking-api-key.secret.yaml" ]; then
    echo "Applying OpenViking API key secret..."
    kubectl apply -f "$SCRIPT_DIR/manifests/openviking-api-key.secret.yaml"
else
    echo "OpenViking API key secret not found."
    echo "  OpenViking will fail to start until the secret exists."
    echo "  Copy viking/manifests/openviking-api-key.secret.yaml.example to"
    echo "  viking/manifests/openviking-api-key.secret.yaml and fill in the API key."
fi

if [ -f "$SCRIPT_DIR/manifests/openviking-s3-credentials.secret.yaml" ]; then
    echo "Applying S3 credentials secret..."
    kubectl apply -f "$SCRIPT_DIR/manifests/openviking-s3-credentials.secret.yaml"
else
    echo "S3 credentials secret not found."
    echo "  AGFS will fail to start until the secret exists."
    echo "  Copy viking/manifests/openviking-s3-credentials.secret.yaml.example to"
    echo "  viking/manifests/openviking-s3-credentials.secret.yaml and fill in Garage credentials."
fi

# Auth secret for Traefik basicAuth ingress
if [ -f "$SCRIPT_DIR/manifests/openviking-auth.secret.yaml" ]; then
    echo "Applying auth secret..."
    kubectl apply -f "$SCRIPT_DIR/manifests/openviking-auth.secret.yaml"
else
    echo "Auth secret not found."
    echo "  Ingress will reject all requests until the secret exists."
    echo "  See viking/manifests/openviking-auth.secret.yaml.example for instructions."
fi

# Local embedding service and ROCm Ollama backend first; OpenViking's config
# points at the embedder in this namespace and Ollama in the llama namespace.
kubectl apply -f "$SCRIPT_DIR/../llama/namespace.yaml"
kubectl apply -f "$SCRIPT_DIR/../llama/pvc.yaml"
kubectl apply -f "$SCRIPT_DIR/../llama/ollama-configmap.yaml"
kubectl apply -f "$SCRIPT_DIR/../llama/ollama-deployment.yaml"
kubectl -n llama scale deployment/ollama --replicas=1

kubectl apply -f "$SCRIPT_DIR/manifests/embedder-llamacpp-deployment.yaml"
kubectl apply -f "$SCRIPT_DIR/manifests/embedder-llamacpp-service.yaml"

# Single-instance OpenViking is the canonical deployment path. The
# coordinator/worker manifests are kept for experiments, not default rollout.
kubectl apply -f "$SCRIPT_DIR/manifests/openviking-deployment.yaml"
kubectl apply -f "$SCRIPT_DIR/manifests/openviking-service.yaml"

# Traefik ingress (context.nathanwhyte.dev with basicAuth)
kubectl apply -f "$SCRIPT_DIR/manifests/openviking-ingress.yaml"

echo ""
echo "=== Waiting for rollouts ==="
kubectl rollout status deployment/ollama -n llama --timeout=600s &
kubectl rollout status deployment/embedder-llamacpp -n viking --timeout=300s &
kubectl rollout status deployment/openviking -n viking --timeout=120s &
wait

echo ""
echo "=== OpenViking deployed ==="
echo "Internal: http://openviking.viking.svc.cluster.local:1933"
echo "LAN:      https://context.nathanwhyte.dev"
echo "Auth:     Basic api:<token from openviking-auth.secret.yaml>"
echo "Health:   curl -u api:\$TOKEN -H 'X-API-Key: \$OPENVIKING_KEY' https://context.nathanwhyte.dev/health"
