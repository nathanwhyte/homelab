#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== Deploying Homepage dashboard to homepage namespace ==="

if ! kubectl cluster-info &>/dev/null; then
  echo "ERROR: Cannot connect to Kubernetes cluster"
  exit 1
fi

kubectl apply -f "$SCRIPT_DIR/namespace.yaml"
kubectl apply -f "$SCRIPT_DIR/rbac.yaml"
kubectl apply -f "$SCRIPT_DIR/configmap.yaml"
kubectl apply -f "$SCRIPT_DIR/deployment.yaml"
kubectl apply -f "$SCRIPT_DIR/service.yaml"

# Cloudflare Tunnel
if kubectl get secret homepage-cloudflared-token -n homepage &>/dev/null; then
  kubectl apply -f "$SCRIPT_DIR/cloudflared.yaml"
  echo "Cloudflared tunnel configured"
else
  echo "NOTE: No cloudflared token secret found. To enable public access:"
  echo "  kubectl create secret generic homepage-cloudflared-token \\"
  echo "    --namespace homepage \\"
  echo "    --from-literal=TOKEN=<your-tunnel-token>"
  echo "  kubectl apply -f $SCRIPT_DIR/cloudflared.yaml"
fi

echo ""
echo "=== Waiting for rollouts ==="
kubectl rollout status deployment/homepage -n homepage --timeout=120s

echo ""
echo "=== Homepage deployed ==="
echo "Internal: http://homepage.homepage.svc.cluster.local:3000"
echo "Public:   https://home.nathanwhyte.dev (requires cloudflared)"
echo "Your homelab dashboard with all services at a glance!"
echo "Check:    kubectl get pods -n homepage"
