#!/usr/bin/env bash
set -euo pipefail

# ⚠️  ARCHIVED / RESTORE-ONLY — do not run for a normal deploy.
# The parallel coordinator/worker/merge trio was removed from the live cluster
# after the 2026-06-03 single-instance cutover (see CLAUDE.md §Failover). These
# manifests are retained in the repo only so the parallel path can be restored
# if throughput ever demands it. The canonical deploy is deploy-openviking.sh
# (single-instance). To restore parallel mode: `kubectl apply` the
# ov-coordinator, ov-merge, and ov-worker manifests, then
# `kubectl scale statefulset ov-worker --replicas=3 -n viking`
# && `kubectl scale deployment ov-coordinator --replicas=1 -n viking`.
#
# Parallel OpenViking Deployment
# Deploys 3 OV worker pods + coordinator proxy
# Usage: bash viking/deploy-openviking-parallel.sh [--build]

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NAMESPACE="viking"
REGISTRY="registry.nathanwhyte.dev/homelab"
COORDINATOR_IMAGE="${REGISTRY}/ov-coordinator:latest"

echo "=== Parallel OpenViking Deployment ==="
echo "Namespace: ${NAMESPACE}"
echo ""

# Step 1: Ensure namespace exists
echo "--- Step 1: Namespace ---"
kubectl apply -f "${SCRIPT_DIR}/manifests/namespace.yaml"

# Step 2: Apply PVCs (unchanged, used by workers)
echo "--- Step 2: PVCs ---"
kubectl apply -f "${SCRIPT_DIR}/manifests/openviking-pvc.yaml"

# Step 3: Apply base ConfigMap (unchanged, used by workers)
echo "--- Step 3: ConfigMap ---"
kubectl apply -f "${SCRIPT_DIR}/manifests/openviking-configmap.yaml"

# Step 4: Apply secrets (if they exist)
echo "--- Step 4: Secrets ---"
if [ -f "${SCRIPT_DIR}/manifests/openviking-s3-credentials.secret.yaml" ]; then
	echo "Applying S3 credentials secret..."
	kubectl apply -f "${SCRIPT_DIR}/manifests/openviking-s3-credentials.secret.yaml"
else
	echo "S3 credentials secret not found."
	echo "  AGFS will fail to start until the secret exists."
	echo "  Copy viking/manifests/openviking-s3-credentials.secret.yaml.example and fill in Garage credentials."
fi

if [ -f "${SCRIPT_DIR}/manifests/openviking-auth.secret.yaml" ]; then
	echo "Applying auth secret..."
	kubectl apply -f "${SCRIPT_DIR}/manifests/openviking-auth.secret.yaml"
else
	echo "Auth secret not found."
	echo "  Ingress will reject all requests until the secret exists."
	echo "  See viking/manifests/openviking-auth.secret.yaml.example for instructions."
fi

# Step 5: Create coordinator code ConfigMap from source
echo "--- Step 5: Coordinator ConfigMap ---"
kubectl -n "${NAMESPACE}" create configmap ov-coordinator-code \
	--from-file=coordinator.py="${SCRIPT_DIR}/ov-coordinator/coordinator.py" \
	--dry-run=client -o yaml | kubectl apply -f -

# Step 6: Scale down existing single-instance deployment (if running)
echo "--- Step 6: Scale down existing deployment ---"
kubectl -n "${NAMESPACE}" scale deployment/openviking --replicas=0 2>/dev/null || true

# Step 7: Apply headless service for worker DNS
echo "--- Step 7: Headless service ---"
kubectl apply -f "${SCRIPT_DIR}/manifests/ov-worker-headless-service.yaml"

# Step 8: Apply worker StatefulSet
echo "--- Step 8: Worker StatefulSet ---"
kubectl apply -f "${SCRIPT_DIR}/manifests/ov-worker-statefulset.yaml"
# Apply worker config configmap if it exists
[[ -f "${SCRIPT_DIR}/manifests/ov-worker-config-configmap.yaml" ]] &&
	kubectl apply -f "${SCRIPT_DIR}/manifests/ov-worker-config-configmap.yaml"

echo "Waiting for workers..."
kubectl -n "${NAMESPACE}" rollout status statefulset/ov-worker --timeout=180s

# Step 9: Apply coordinator deployment
echo "--- Step 9: Coordinator ---"
kubectl apply -f "${SCRIPT_DIR}/manifests/ov-coordinator-deployment.yaml"
echo "Waiting for coordinator..."
kubectl -n "${NAMESPACE}" rollout status deployment/ov-coordinator --timeout=120s

# Step 10: Apply coordinator service (replaces existing 'openviking' service)
echo "--- Step 10: Service (coordinator) ---"
kubectl apply -f "${SCRIPT_DIR}/manifests/ov-coordinator-service.yaml"

# Step 10b: Apply merged single-instance service path for background merging
echo "--- Step 10b: Merged instance ---"
kubectl apply -f "${SCRIPT_DIR}/manifests/openviking-deployment.yaml"
kubectl apply -f "${SCRIPT_DIR}/manifests/ov-merged-service.yaml"

# Step 11: Apply ingress (unchanged)
echo "--- Step 11: Ingress ---"
kubectl apply -f "${SCRIPT_DIR}/manifests/openviking-ingress.yaml"

# Step 12: Apply embedder + CUDA LLM
echo "--- Step 12: Embedder + CUDA LLM ---"
kubectl apply -f "${SCRIPT_DIR}/manifests/embedder-llamacpp-deployment.yaml"
kubectl apply -f "${SCRIPT_DIR}/manifests/embedder-llamacpp-service.yaml"
kubectl apply -f "${SCRIPT_DIR}/manifests/cuda-llamacpp-deployment.yaml"
kubectl apply -f "${SCRIPT_DIR}/manifests/cuda-llamacpp-service.yaml"

# Step 13: Verify
echo ""
echo "=== Verification ==="
echo "Workers:"
kubectl -n "${NAMESPACE}" get pods -l app=ov-worker -o wide
echo ""
echo "Coordinator:"
kubectl -n "${NAMESPACE}" get pods -l app=ov-coordinator -o wide
echo ""
echo "Services:"
kubectl -n "${NAMESPACE}" get svc openviking ov-worker-headless
echo ""

# Health check
echo "--- Health Check ---"
echo "Run: kubectl run test --rm -i --restart=Never --image=curlimages/curl -- curl -s http://openviking.viking.svc:1933/health"
echo ""
echo "=== Deployment complete ==="
echo "Internal: http://openviking.viking.svc.cluster.local:1933"
echo "LAN:      http://192.168.1.19:31933"
echo ""
echo "To rollback to single instance:"
echo "  kubectl -n ${NAMESPACE} scale statefulset/ov-worker --replicas=0"
echo "  kubectl -n ${NAMESPACE} scale deployment/ov-coordinator --replicas=0"
echo "  kubectl apply -f ${SCRIPT_DIR}/manifests/openviking-service.yaml  # restore original selector"
echo "  kubectl -n ${NAMESPACE} scale deployment/openviking --replicas=1"
