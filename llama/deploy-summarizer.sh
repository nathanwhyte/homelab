#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Deploying summarizer stack (backed by llamacpp-rocm)..."

echo "1/3 Applying LLM alias service (routes to llamacpp-rocm)..."
kubectl apply -f "$SCRIPT_DIR/llm-alias-service.yaml"

echo "2/3 Applying summarizer API code + deployment..."
kubectl apply -f "$SCRIPT_DIR/summarizer-api-configmap.yaml"
kubectl apply -f "$SCRIPT_DIR/summarizer-api-deployment.yaml"

echo "3/3 Waiting for rollout..."
kubectl rollout status deployment/summarizer-api -n llama --timeout=60s || true

echo ""
echo "Done! Services:"
echo "  LLM backend: qwen-summarizer-llm.llama.svc.cluster.local (port 80) -> llamacpp-rocm"
echo "  Summarizer API: summarizer-api.llama.svc.cluster.local (port 80)"
echo ""
echo "Usage:"
echo '  curl -s http://summarizer-api.llama.svc.cluster.local/summarize \'
echo '    -H "Content-Type: application/json" \'
echo '    -d '"'"'{"context": "your text here", "mode": "context"}'"'"''
echo ""
echo "Modes: context, conversation, code"
echo ""
echo "Note: Requires llamacpp-rocm to be running on timmy."
echo "  Deploy it with: bash llama/deploy-llamacpp-rocm.sh"
