#!/usr/bin/env bash
# Run one quant arm of the GTX 1060 embedder throughput benchmark.
#   ./cluster/throughput-1060-run.sh Q8_0|Q4_K_M [--ubatch 512] [--max-docs 0]
#
# Prereqs:
#   1. ./cluster/throughput-apply.sh --node wemby  (ns, PVC, corpus, scripts)
#   2. reranker-bge scaled to 0, freeing wemby's only GPU:
#        kubectl -n viking scale deploy/reranker-bge --replicas=0
#      Restore afterwards with --replicas=1.
#
# Renders throughput-1060-cuda.yaml, waits for the embedder (first run of a
# quant downloads its GGUF to the PVC), samples GPU memory and temperature
# from inside the embedder pod every 5s while the Job runs, copies the result
# JSON and the GPU samples into results-throughput/, then deletes the arm's
# Deployment, Service, and Job. The namespace, PVC, and cached models stay.
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"

quant="${1:-}"
case "$quant" in
  Q8_0 | Q4_K_M) shift ;;
  *) echo "usage: $0 Q8_0|Q4_K_M [--ubatch N] [--max-docs N]" >&2; exit 2 ;;
esac
ubatch=512
max_docs=0
while [ $# -gt 0 ]; do
  case "$1" in
    --ubatch) ubatch="$2"; shift 2 ;;
    --max-docs) max_docs="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

label="gtx1060-cuda-$(echo "$quant" | tr '[:upper:]' '[:lower:]')-ub${ubatch}"
[ "$max_docs" = "0" ] || label="${label}-quick${max_docs}"
out_dir="$here/results-throughput"
mkdir -p "$out_dir"

# --- preflight --------------------------------------------------------------
reranker="$(kubectl -n viking get deploy reranker-bge -o jsonpath='{.spec.replicas}' 2>/dev/null || echo absent)"
if [ "$reranker" != "0" ] && [ "$reranker" != "absent" ]; then
  echo "reranker-bge is at replicas=$reranker and holds wemby's GPU." >&2
  echo "Scale it down first: kubectl -n viking scale deploy/reranker-bge --replicas=0" >&2
  exit 1
fi
seed_node="$(kubectl -n bench get pod corpus-seed -o jsonpath='{.spec.nodeSelector.kubernetes\.io/hostname}' 2>/dev/null || true)"
if [ "$seed_node" != "wemby" ]; then
  echo "corpus-seed is not pinned to wemby (got '${seed_node:-missing}')." >&2
  echo "Run: ./cluster/throughput-apply.sh --node wemby" >&2
  exit 1
fi

rendered="$(mktemp)"
sampler_pid=""
cleanup() {
  [ -n "$sampler_pid" ] && kill "$sampler_pid" 2>/dev/null || true
  echo ">> tearing down arm $label"
  kubectl delete -f "$rendered" --ignore-not-found --wait=false
  rm -f "$rendered"
}
trap cleanup EXIT

sed -e "s/__QUANT__/$quant/g" -e "s/__UBATCH__/$ubatch/g" \
  -e "s/__LABEL__/$label/g" -e "s/__MAX_DOCS__/$max_docs/g" \
  "$here/cluster/throughput-1060-cuda.yaml" >"$rendered"

# --- run --------------------------------------------------------------------
echo ">> [$label] applying"
kubectl apply -f "$rendered"

echo ">> waiting for embedder-1060-bench (GGUF download on first run)"
kubectl -n bench rollout status deploy/embedder-1060-bench --timeout=45m

samples="$out_dir/gpu-samples-$label.csv"
echo "ts,memory_used_mib,memory_total_mib,temp_c,util_pct" >"$samples"
(
  while true; do
    line="$(kubectl -n bench exec deploy/embedder-1060-bench -c llamacpp -- \
      nvidia-smi --query-gpu=memory.used,memory.total,temperature.gpu,utilization.gpu \
      --format=csv,noheader,nounits 2>/dev/null | tr -d ' ' || true)"
    if [ -n "$line" ]; then echo "$(date +%s),$line" >>"$samples"; fi
    sleep 5
  done
) &
sampler_pid=$!

echo ">> job running; following logs"
kubectl -n bench wait --for=condition=Ready pod -l job-name=throughput-1060 --timeout=10m || true
kubectl -n bench logs -f job/throughput-1060 || true

while true; do
  succeeded="$(kubectl -n bench get job throughput-1060 -o jsonpath='{.status.succeeded}')"
  failed="$(kubectl -n bench get job throughput-1060 -o jsonpath='{.status.failed}')"
  [ "${succeeded:-0}" -ge 1 ] && break
  if [ "${failed:-0}" -ge 1 ]; then
    echo "job throughput-1060 failed" >&2
    exit 1
  fi
  sleep 10
done
kill "$sampler_pid" 2>/dev/null || true
sampler_pid=""

kubectl -n bench cp "corpus-seed:/data/results/throughput-$label.json" "$out_dir/throughput-$label.json"

awk -F, 'NR > 1 {
  if ($2 > mem) mem = $2; total = $3
  if ($4 > temp) temp = $4
  if ($5 > util) util = $5; n++
} END {
  printf ">> GPU over %d samples: peak %d / %d MiB (%d MiB spare), max %d C, max util %d%%\n",
    n, mem, total, total - mem, temp, util
}' "$samples"
echo ">> wrote $out_dir/throughput-$label.json and $samples"
