#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== Deploying IT-Tools to it-tools namespace ==="

if ! kubectl cluster-info &>/dev/null; then
  echo "ERROR: Cannot connect to Kubernetes cluster"
  exit 1
fi

kubectl apply -f "$SCRIPT_DIR/namespace.yaml"
kubectl apply -f "$SCRIPT_DIR/deployment.yaml"
kubectl apply -f "$SCRIPT_DIR/service.yaml"

# Cloudflare Tunnel
if kubectl get secret it-tools-cloudflared-token -n it-tools &>/dev/null; then
  kubectl apply -f "$SCRIPT_DIR/cloudflared.yaml"
  echo "Cloudflared tunnel configured"
else
  echo "NOTE: No cloudflared token secret found. To enable public access:"
  echo "  kubectl create secret generic it-tools-cloudflared-token \\"
  echo "    --namespace it-tools \\"
  echo "    --from-literal=TOKEN=<your-tunnel-token>"
  echo "  kubectl apply -f $SCRIPT_DIR/cloudflared.yaml"
fi

echo ""
echo "=== Waiting for rollouts ==="
kubectl rollout status deployment/it-tools -n it-tools --timeout=120s

echo ""
echo "=== IT-Tools deployed ==="
echo "Internal: http://it-tools.it-tools.svc.cluster.local"
echo "Public:   https://tools.nathanwhyte.dev (requires cloudflared)"
echo "80+ developer utilities: hash generators, JWT debugger, Base64,"
echo "  QR codes, cron parser, Docker run→compose, UUID gen, and more!"
echo "Check:    kubectl get pods -n it-tools"
