#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== Deploying Excalidraw to excalidraw namespace ==="

if ! kubectl cluster-info &>/dev/null; then
  echo "ERROR: Cannot connect to Kubernetes cluster"
  exit 1
fi

kubectl apply -f "$SCRIPT_DIR/namespace.yaml"
kubectl apply -f "$SCRIPT_DIR/deployment.yaml"
kubectl apply -f "$SCRIPT_DIR/service.yaml"

# Cloudflare Tunnel
if kubectl get secret excalidraw-cloudflared-token -n excalidraw &>/dev/null; then
  kubectl apply -f "$SCRIPT_DIR/cloudflared.yaml"
  echo "Cloudflared tunnel configured"
else
  echo "NOTE: No cloudflared token secret found. To enable public access:"
  echo "  kubectl create secret generic excalidraw-cloudflared-token \\"
  echo "    --namespace excalidraw \\"
  echo "    --from-literal=TOKEN=<your-tunnel-token>"
  echo "  kubectl apply -f $SCRIPT_DIR/cloudflared.yaml"
fi

echo ""
echo "=== Waiting for rollouts ==="
kubectl rollout status deployment/excalidraw -n excalidraw --timeout=120s

echo ""
echo "=== Excalidraw deployed ==="
echo "Internal: http://excalidraw.excalidraw.svc.cluster.local"
echo "Public:   https://draw.nathanwhyte.dev (requires cloudflared)"
echo "A beautiful hand-drawn style whiteboard for diagrams & sketches!"
echo "Check:    kubectl get pods -n excalidraw"
