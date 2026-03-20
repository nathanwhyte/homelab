#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== Deploying Heaper to heaper namespace ==="

# Verify cluster access
if ! kubectl cluster-info &>/dev/null; then
  echo "ERROR: Cannot connect to Kubernetes cluster"
  exit 1
fi

# Create namespace
kubectl apply -f "$SCRIPT_DIR/namespace.yaml"

# PVCs
kubectl apply -f "$SCRIPT_DIR/pvcs.yaml"

# Secret (warn if using template)
if [ -f "$SCRIPT_DIR/secret.yaml" ]; then
  echo "WARNING: Applying secret.yaml — make sure you've set a real POSTGRES_PASSWORD!"
  kubectl apply -f "$SCRIPT_DIR/secret.yaml"
else
  echo "NOTE: No secret.yaml found. Create the secret manually:"
  echo "  kubectl create secret generic heaper-credentials \\"
  echo "    --namespace heaper \\"
  echo "    --from-literal=POSTGRES_PASSWORD=<your-password>"
fi

# Deployment and service
kubectl apply -f "$SCRIPT_DIR/deployment.yaml"
kubectl apply -f "$SCRIPT_DIR/service.yaml"

# Cloudflare Tunnel (public access)
if kubectl get secret heaper-cloudflared-token -n heaper &>/dev/null; then
  kubectl apply -f "$SCRIPT_DIR/cloudflared.yaml"
  echo "Cloudflared tunnel configured"
else
  echo "NOTE: No cloudflared token secret found. To enable public access:"
  echo "  kubectl create secret generic heaper-cloudflared-token \\"
  echo "    --namespace heaper \\"
  echo "    --from-literal=TOKEN=<your-tunnel-token>"
  echo "  kubectl apply -f $SCRIPT_DIR/cloudflared.yaml"
fi

echo ""
echo "=== Waiting for rollouts ==="
kubectl rollout status deployment/heaper -n heaper --timeout=180s

echo ""
echo "=== Heaper deployed ==="
echo "Internal: http://heaper.heaper.svc.cluster.local"
echo "Public:   https://heaper.nathanwhyte.dev (requires cloudflared)"
echo "Health:   curl http://heaper.heaper.svc.cluster.local/api"
echo "Check:    kubectl get pods -n heaper"
