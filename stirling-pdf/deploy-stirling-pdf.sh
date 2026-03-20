#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== Deploying Stirling PDF to stirling-pdf namespace ==="

if ! kubectl cluster-info &>/dev/null; then
  echo "ERROR: Cannot connect to Kubernetes cluster"
  exit 1
fi

kubectl apply -f "$SCRIPT_DIR/namespace.yaml"
kubectl apply -f "$SCRIPT_DIR/deployment.yaml"
kubectl apply -f "$SCRIPT_DIR/service.yaml"

# Cloudflare Tunnel
if kubectl get secret stirling-pdf-cloudflared-token -n stirling-pdf &>/dev/null; then
  kubectl apply -f "$SCRIPT_DIR/cloudflared.yaml"
  echo "Cloudflared tunnel configured"
else
  echo "NOTE: No cloudflared token secret found. To enable public access:"
  echo "  kubectl create secret generic stirling-pdf-cloudflared-token \\"
  echo "    --namespace stirling-pdf \\"
  echo "    --from-literal=TOKEN=<your-tunnel-token>"
  echo "  kubectl apply -f $SCRIPT_DIR/cloudflared.yaml"
fi

echo ""
echo "=== Waiting for rollouts ==="
kubectl rollout status deployment/stirling-pdf -n stirling-pdf --timeout=180s

echo ""
echo "=== Stirling PDF deployed ==="
echo "Internal: http://stirling-pdf.stirling-pdf.svc.cluster.local:8080"
echo "Public:   https://pdf.nathanwhyte.dev (requires cloudflared)"
echo "Merge, split, rotate, convert, compress, watermark PDFs and more!"
echo "Check:    kubectl get pods -n stirling-pdf"
