#!/usr/bin/env bash
# Stand up the TASK-1136 embedder throughput benchmark (tokenless).
#   ./cluster/throughput-apply.sh [--vault ~/code/compendium] [--node manu|wemby]
#
# --node pins the corpus-seed pod, and with it the RWO bench-data PVC, to the
# node whose GPU is being benchmarked (default manu). Every phase Job for that
# run must target the same node. To switch nodes, delete the bench namespace
# first: a running pod's nodeSelector cannot be changed.
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
node="manu"
while [ $# -gt 0 ]; do
  case "$1" in
    --vault) vault="$2"; shift 2 ;;
    --node) node="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

corpus="$here/corpus.jsonl"
echo ">> exporting corpus from $vault"
python3 "$here/export_corpus.py" --vault "$vault" --out "$corpus"

echo ">> applying base (ns + PVC + corpus-seed on $node)"
sed "s/kubernetes.io\/hostname: manu/kubernetes.io\/hostname: $node/" \
  "$here/cluster/throughput-base.yaml" | kubectl apply -f -

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
echo "  $here/cluster/throughput-cuda-run.sh --node $node Q8_0 --max-docs 200   # quick pass"
echo "  $here/cluster/throughput-cuda-run.sh --node $node Q8_0"
echo "  $here/cluster/throughput-cuda-run.sh --node $node Q4_K_M"
echo "fetch results:  kubectl -n bench cp corpus-seed:/data/results ./results-throughput"
