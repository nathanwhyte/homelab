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

# Config. The standalone config is the canonical single-worker path: local AGFS
# on the OpenViking PVC and a dedicated CUDA llama.cpp VLM on manu.
kubectl apply -f "$SCRIPT_DIR/manifests/openviking-configmap.yaml"
kubectl apply -f "$SCRIPT_DIR/manifests/openviking-standalone-configmap.yaml"

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
	echo "Applying optional S3 credentials secret..."
	kubectl apply -f "$SCRIPT_DIR/manifests/openviking-s3-credentials.secret.yaml"
else
	echo "Optional S3 credentials secret not found."
	echo "  This is fine for the canonical local AGFS deployment."
	echo "  Copy viking/manifests/openviking-s3-credentials.secret.yaml.example to"
	echo "  viking/manifests/openviking-s3-credentials.secret.yaml only when using S3 AGFS."
fi

# Auth posture: API-key-only (IMPR-1007 Phase 4, 2026-07-04). OV enforces
# root_api_key on every tier; no Traefik BasicAuth layer.

# Local embedding service and CUDA llama.cpp backend first. OpenViking is
# pinned to timmy and points at these in-namespace services.
kubectl apply -f "$SCRIPT_DIR/manifests/embedder-llamacpp-deployment.yaml"
kubectl apply -f "$SCRIPT_DIR/manifests/embedder-llamacpp-service.yaml"

# Single-instance OpenViking is the canonical deployment path. The
# coordinator/worker manifests are kept for experiments, not default rollout.
kubectl apply -f "$SCRIPT_DIR/manifests/openviking-deployment.yaml"
kubectl apply -f "$SCRIPT_DIR/manifests/openviking-service.yaml"

# HTTP vectordb backend — required when ov.conf sets vectordb.backend=http.
kubectl apply -f "$SCRIPT_DIR/manifests/ov-vectordb-pvc.yaml"
kubectl apply -f "$SCRIPT_DIR/manifests/ov-vectordb-deployment.yaml"
kubectl apply -f "$SCRIPT_DIR/manifests/ov-vectordb-service.yaml"

# Traefik Ingress (repo manifest; the live public route uses the Cloudflare
# tunnel — see CLAUDE.md Endpoint tiers. Auth is OV API-key-only; no BasicAuth.)
kubectl apply -f "$SCRIPT_DIR/manifests/openviking-ingress.yaml"

echo ""
echo "=== Waiting for rollouts ==="
kubectl rollout status deployment/embedder-llamacpp -n viking --timeout=300s &
kubectl rollout status deployment/llamacpp-cuda-ov -n viking --timeout=900s &
kubectl rollout status deployment/openviking -n viking --timeout=120s &
wait

echo ""
echo "=== OpenViking deployed ==="
echo "Internal: http://openviking.viking.svc.cluster.local:1933"
echo "LAN:      http://192.168.1.19:31933"
echo "Public:   https://context.nathanwhyte.dev (Cloudflare tunnel)"
echo "Auth:     OV root_api_key (\$OPENVIKING_KEY)"
echo "Health:   curl -H 'X-API-Key: \$OPENVIKING_KEY' http://192.168.1.19:31933/health"
