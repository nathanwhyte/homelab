#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFESTS="$SCRIPT_DIR/manifests"
KUBECTL=(kubectl)

if [ -n "${KUBECTL_CONTEXT:-}" ]; then
	KUBECTL+=(--context "$KUBECTL_CONTEXT")
fi

APPLIED=()

apply() {
	echo "apply: $1"
	"${KUBECTL[@]}" apply -f "$MANIFESTS/$1"
	APPLIED+=("$1")
}

# The script's apply list and kustomization.yaml both describe the active
# resource set; a manifest added to only one of them is exactly the drift
# class that rotted the previous version of this script. Secrets are
# script-only by design (kustomization excludes untracked *.secret.yaml).
verify_manifest_set() {
	local expected actual
	expected=$(awk '/^resources:/ { in_list = 1; next }
    in_list && sub(/^  - /, "") { print; next }
    in_list { exit }' "$MANIFESTS/kustomization.yaml" | sort)
	actual=$(printf '%s\n' "${APPLIED[@]}" | grep -v '\.secret\.yaml$' | sort)
	if [ "$expected" != "$actual" ]; then
		echo "ERROR: deploy script and kustomization.yaml disagree on the active manifest set" >&2
		echo "--- only in kustomization.yaml:" >&2
		comm -23 <(printf '%s\n' "$expected") <(printf '%s\n' "$actual") >&2
		echo "--- only in this script:" >&2
		comm -13 <(printf '%s\n' "$expected") <(printf '%s\n' "$actual") >&2
		return 1
	fi
	echo "manifest set check: script matches kustomization.yaml"
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
require_secret_file ollama-api-key.secret.yaml ollama-api-key.secret.yaml.example "ollama auth-proxy Bearer (cloud VLM, IDEA-1050)"

# Shared config and generated configmaps used by the app/exporter/dashboard.
apply openviking-configmap.yaml
apply openviking-standalone-configmap.yaml
apply openviking-exporter-configmap.yaml
apply openviking-dashboard-configmap.yaml
apply openviking-native-dashboard-configmap.yaml

# Storage and model-serving dependencies before the OpenViking API pod. OpenViking
# readiness depends on both the HTTP vectordb and the embedding/VLM services.
apply ov-vectordb-pvc.yaml
apply ov-vectordb-deployment.yaml
apply ov-vectordb-service.yaml
apply embedder-qwen-rocm-deployment.yaml # primary since 2026-07-06 (was embedder-qwen-cuda on wemby)
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
verify_manifest_set

echo ""
echo "=== Waiting for rollouts ==="
"${KUBECTL[@]}" -n viking rollout status deployment/ov-vectordb --timeout=300s
"${KUBECTL[@]}" -n viking rollout status deployment/embedder-qwen-rocm --timeout=900s
"${KUBECTL[@]}" -n viking rollout status deployment/llamacpp-cuda-ov --timeout=900s
"${KUBECTL[@]}" -n viking rollout status deployment/openviking --timeout=300s

# First-party config/provider validation (embedding + VLM auth reachability).
# Run inside the pod so it sees the rendered ov.conf and cluster DNS.
echo ""
echo "=== openviking-server doctor ==="
"${KUBECTL[@]}" -n viking exec deploy/openviking -c openviking -- openviking-server doctor --config /app/.openviking/ov.conf

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
