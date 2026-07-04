#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFESTS="$SCRIPT_DIR/manifests"
KUBECTL=(kubectl)

if [ -n "${KUBECTL_CONTEXT:-}" ]; then
  KUBECTL+=(--context "$KUBECTL_CONTEXT")
fi

apply() {
  echo "apply: $1"
  "${KUBECTL[@]}" apply -f "$MANIFESTS/$1"
}

require_secret_file() {
  local file="$1"
  local example="$2"
  local description="$3"
  if [ ! -f "$MANIFESTS/$file" ]; then
    echo "ERROR: missing $description: $MANIFESTS/$file" >&2
    echo "       copy $MANIFESTS/$example to $MANIFESTS/$file and fill in real values" >&2
    return 1
  fi
  apply "$file"
}

echo "=== Deploying canonical OpenViking stack to viking namespace ==="
echo "kubectl context: ${KUBECTL_CONTEXT:-current}"

# Namespace first so secret/config applies have a target.
apply namespace.yaml

# Required secrets for the canonical S3 AGFS + exporter-backed deployment.
# Auth posture: API-key-only (IMPR-1007 Phase 4, 2026-07-04). OV enforces
# root_api_key on every tier; no Traefik BasicAuth layer.
require_secret_file openviking-api-key.secret.yaml openviking-api-key.secret.yaml.example "OpenViking API key secret"
require_secret_file openviking-s3-credentials.secret.yaml openviking-s3-credentials.secret.yaml.example "Garage S3 credentials secret"

# Shared config and generated configmaps used by the app/exporter/dashboard.
apply openviking-configmap.yaml
apply openviking-standalone-configmap.yaml
apply openviking-exporter-configmap.yaml
apply openviking-dashboard-configmap.yaml

# Storage and model-serving dependencies before the OpenViking API pod. OpenViking
# readiness depends on both the HTTP vectordb and the embedding/VLM services.
apply ov-vectordb-pvc.yaml
apply ov-vectordb-deployment.yaml
apply ov-vectordb-service.yaml
apply embedder-qwen-cuda-deployment.yaml
apply llamacpp-vlm-service.yaml
apply cuda-llamacpp-deployment.yaml
apply cuda-llamacpp-service.yaml

# Main API and access surfaces. Ingress auth is OV API-key-only; no BasicAuth.
apply openviking-pvc.yaml
apply openviking-deployment.yaml
apply openviking-service.yaml
apply openviking-nodeport-service.yaml
apply openviking-ingress.yaml
apply openviking-mcp-ingress.yaml

# Observability resources are safe to apply after the Service exists. They depend
# on kube-prometheus-stack CRDs being installed in the cluster.
apply openviking-servicemonitor.yaml
apply openviking-alerts.yaml

echo ""
echo "=== Waiting for rollouts ==="
"${KUBECTL[@]}" -n viking rollout status deployment/ov-vectordb --timeout=300s
"${KUBECTL[@]}" -n viking rollout status deployment/embedder-qwen-cuda --timeout=900s
"${KUBECTL[@]}" -n viking rollout status deployment/llamacpp-cuda-ov --timeout=900s
"${KUBECTL[@]}" -n viking rollout status deployment/openviking --timeout=300s

echo ""
echo "=== OpenViking deployed ==="
echo "Internal: http://openviking.viking.svc.cluster.local:1933"
echo "LAN:      http://192.168.1.19:31933"
echo "Public:   https://context.nathanwhyte.dev"
echo "MCP:      https://context.nathanwhyte.dev/mcp"
echo "Auth:     Authorization: Bearer OPENVIKING_API_KEY"
echo "Health:   kubectl -n viking exec deploy/openviking -c openviking -- python - <<'PY'"
echo "          import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:1933/health', timeout=5).read().decode())"
echo "          PY"
