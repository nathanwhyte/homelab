#!/usr/bin/env bash
# Stand up the TASK-1136 embedder throughput benchmark (tokenless).
#   ./cluster/throughput-apply.sh [--vault ~/code/compendium]
#
# 1. exports the corpus locally (export_corpus.py) — same file feeds both cards
# 2. applies throughput-base.yaml (ns + PVC + corpus-seed pod)
# 3. kubectl cp's corpus.jsonl onto the PVC via corpus-seed (no GitHub token)
# 4. creates configmap throughput-scripts (benchmark_throughput.py)
#
# Then run a phase:
#   kubectl apply -f cluster/throughput-1080-cuda.yaml
#   kubectl -n bench logs job/throughput-1080 -f
# Teardown everything: kubectl delete namespace bench
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
vault="$HOME/code/compendium"
[ "${1:-}" = "--vault" ] && vault="$2"

corpus="$here/corpus.jsonl"
echo ">> exporting corpus from $vault"
python3 "$here/export_corpus.py" --vault "$vault" --out "$corpus"

echo ">> applying base (ns + PVC + corpus-seed)"
kubectl apply -f "$here/cluster/throughput-base.yaml"

echo ">> waiting for corpus-seed pod"
kubectl -n bench wait --for=condition=Ready pod/corpus-seed --timeout=120s

echo ">> seeding corpus onto the PVC (kubectl cp)"
kubectl -n bench cp "$corpus" corpus-seed:/data/corpus.jsonl

echo ">> creating configmap throughput-scripts"
kubectl -n bench create configmap throughput-scripts \
  --from-file="$here/benchmark_throughput.py" \
  --dry-run=client -o yaml | kubectl apply -f -

echo
echo "ready. run a phase:"
echo "  kubectl apply -f $here/cluster/throughput-1080-cuda.yaml"
echo "  kubectl -n bench logs job/throughput-1080 -f"
echo "fetch results:  kubectl -n bench cp corpus-seed:/data/results ./results-throughput"
