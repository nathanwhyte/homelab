#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Deploying Qwen3 14B summarizer stack..."

echo "1/4 Applying Qwen llama.cpp backend (manu NVIDIA GPU)..."
kubectl apply -f "$SCRIPT_DIR/qwen-summarizer-deployment.yaml"
kubectl apply -f "$SCRIPT_DIR/qwen-summarizer-service.yaml"

echo "2/4 Applying summarizer API code..."
kubectl apply -f "$SCRIPT_DIR/summarizer-api-configmap.yaml"

echo "3/4 Applying summarizer API deployment + service..."
kubectl apply -f "$SCRIPT_DIR/summarizer-api-deployment.yaml"

echo "4/4 Waiting for rollout..."
kubectl rollout status deployment/summarizer-api -n llama --timeout=60s || true

echo ""
echo "Done! Services:"
echo "  LLM backend: qwen-summarizer-llm.llama.svc.cluster.local (port 80)"
echo "  Summarizer API: summarizer-api.llama.svc.cluster.local (port 80)"
echo ""
echo "Usage:"
echo '  curl -s http://summarizer-api.llama.svc.cluster.local/summarize \'
echo '    -H "Content-Type: application/json" \'
echo '    -d '"'"'{"context": "your text here", "mode": "context"}'"'"''
echo ""
echo "Modes: context, conversation, code"
echo ""
echo "Note: The Qwen model download (~9GB) may take a while on first deploy."
echo "Watch progress: kubectl logs -f deploy/qwen-summarizer -n llama -c model-fetch"
