#!/usr/bin/env bash
# Run one quant arm of the CUDA embedder throughput benchmark on one node.
#   ./cluster/throughput-cuda-run.sh --node manu|wemby Q8_0|Q4_K_M \
#     [--ubatch 512] [--max-docs 0] [--abort-gpu-c 85] [--abort-cpu-c 80]
#
# Prereqs:
#   1. ./cluster/throughput-apply.sh --node <node>  (ns, PVC, corpus, scripts)
#   2. No other pod on <node> holds nvidia.com/gpu. The script refuses
#      otherwise. On manu that means the production embedder:
#        kubectl -n viking scale deploy/embedder-qwen-cuda --replicas=0
#      (OpenViking cannot embed until it is scaled back to 1.)
#
# Renders throughput-cuda-arm.yaml, waits for the embedder (the first run of a
# quant downloads its GGUF to the PVC), and samples GPU memory, GPU
# temperature, utilization, power draw, and host CPU temperature (hwmon
# k10temp/coretemp, visible from inside the pod) every 5s while the Job runs.
#
# Thermal guard: two consecutive samples at or above --abort-gpu-c or
# --abort-cpu-c stop the arm immediately (Deployment scaled to 0, Job deleted)
# and the script exits 3 with the reason. wemby hard-powered-off under this
# load, and manu's CPU cooler fault (BUG-1101) trips at 84-88 C die, so the
# guard is on by default.
#
# On success, copies the result JSON and GPU samples into results-throughput/.
# Always deletes the arm's Deployment, Service, and Job; the namespace, PVC,
# and cached models stay.
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"

usage() {
  echo "usage: $0 --node NODE Q8_0|Q4_K_M [--ubatch N] [--max-docs N] [--abort-gpu-c C] [--abort-cpu-c C]" >&2
  exit 2
}

node=""
quant=""
ubatch=512
max_docs=0
abort_gpu_c=85
abort_cpu_c=80
while [ $# -gt 0 ]; do
  case "$1" in
    --node) node="$2"; shift 2 ;;
    --ubatch) ubatch="$2"; shift 2 ;;
    --max-docs) max_docs="$2"; shift 2 ;;
    --abort-gpu-c) abort_gpu_c="$2"; shift 2 ;;
    --abort-cpu-c) abort_cpu_c="$2"; shift 2 ;;
    Q8_0 | Q4_K_M) quant="$1"; shift ;;
    *) usage ;;
  esac
done
[ -n "$node" ] && [ -n "$quant" ] || usage

card="$(kubectl get node "$node" -o jsonpath='{.metadata.labels.nvidia\.com/gpu\.product}' |
  sed -e 's/^NVIDIA-GeForce-//' -e 's/-//g' | tr '[:upper:]' '[:lower:]')"
[ -n "$card" ] || { echo "node $node has no nvidia.com/gpu.product label" >&2; exit 1; }
label="${card}-cuda-$(echo "$quant" | tr '[:upper:]' '[:lower:]')-ub${ubatch}"
[ "$max_docs" = "0" ] || label="${label}-quick${max_docs}"
out_dir="$here/results-throughput"
mkdir -p "$out_dir"

# --- preflight --------------------------------------------------------------
holders="$(kubectl get pods -A --field-selector "spec.nodeName=$node,status.phase=Running" \
  -o custom-columns='NS:.metadata.namespace,NAME:.metadata.name,GPU:.spec.containers[*].resources.limits.nvidia\.com/gpu' \
  --no-headers | awk '$3 != "<none>" && $1 != "bench" {print $1 "/" $2}')"
if [ -n "$holders" ]; then
  echo "GPU on $node is held by:" >&2
  echo "$holders" >&2
  echo "Scale those to 0 first (see header)." >&2
  exit 1
fi
seed_node="$(kubectl -n bench get pod corpus-seed -o jsonpath='{.spec.nodeSelector.kubernetes\.io/hostname}' 2>/dev/null || true)"
if [ "$seed_node" != "$node" ]; then
  echo "corpus-seed is not pinned to $node (got '${seed_node:-missing}')." >&2
  echo "Run: ./cluster/throughput-apply.sh --node $node" >&2
  exit 1
fi

rendered="$(mktemp)"
abort_file="$(mktemp)"
sampler_pid=""
cleanup() {
  if [ -n "$sampler_pid" ]; then kill "$sampler_pid" 2>/dev/null || true; fi
  echo ">> tearing down arm $label"
  kubectl delete -f "$rendered" --ignore-not-found --wait=false
  rm -f "$rendered" "$abort_file"
}
trap cleanup EXIT

sed -e "s/__NODE__/$node/g" -e "s/__QUANT__/$quant/g" -e "s/__UBATCH__/$ubatch/g" \
  -e "s/__LABEL__/$label/g" -e "s/__MAX_DOCS__/$max_docs/g" \
  "$here/cluster/throughput-cuda-arm.yaml" >"$rendered"

# --- run --------------------------------------------------------------------
echo ">> [$label] applying (abort at GPU >= ${abort_gpu_c} C or CPU >= ${abort_cpu_c} C)"
kubectl apply -f "$rendered"

echo ">> waiting for embedder-cuda-arm (GGUF download on first run)"
kubectl -n bench rollout status deploy/embedder-cuda-arm --timeout=60m

samples="$out_dir/gpu-samples-$label.csv"
echo "ts,memory_used_mib,memory_total_mib,gpu_temp_c,util_pct,power_w,cpu_temp_c" >"$samples"
(
  hot=0
  while true; do
    gpu="$(kubectl -n bench exec deploy/embedder-cuda-arm -c llamacpp -- \
      nvidia-smi --query-gpu=memory.used,memory.total,temperature.gpu,utilization.gpu,power.draw \
      --format=csv,noheader,nounits 2>/dev/null | tr -d ' ' || true)"
    # shellcheck disable=SC2016 # expanded by the pod's shell, not this one
    cpu="$(kubectl -n bench exec deploy/embedder-cuda-arm -c llamacpp -- sh -c \
      'm=0; for h in /sys/class/hwmon/hwmon*; do case "$(cat $h/name 2>/dev/null)" in k10temp|coretemp)
         for t in $h/temp*_input; do v=$(cat $t 2>/dev/null || echo 0); [ "$v" -gt "$m" ] && m=$v; done;; esac; done; echo $((m/1000))' \
      2>/dev/null || true)"
    if [ -n "$gpu" ]; then
      echo "$(date +%s),$gpu,${cpu:-}" >>"$samples"
      gpu_temp="$(echo "$gpu" | cut -d, -f3 | cut -d. -f1)"
      if [ "${gpu_temp:-0}" -ge "$abort_gpu_c" ] || [ "${cpu:-0}" -ge "$abort_cpu_c" ]; then
        hot=$((hot + 1))
      else
        hot=0
      fi
      if [ "$hot" -ge 2 ]; then
        echo "thermal abort: GPU ${gpu_temp} C (limit ${abort_gpu_c}), CPU ${cpu:-?} C (limit ${abort_cpu_c})" >"$abort_file"
        kubectl -n bench scale deploy/embedder-cuda-arm --replicas=0 >/dev/null 2>&1 || true
        kubectl -n bench delete job throughput-cuda-arm --wait=false >/dev/null 2>&1 || true
        exit 0
      fi
    fi
    sleep 5
  done
) &
sampler_pid=$!

summarize() {
  awk -F, 'NR > 1 {
    if ($2 > mem) mem = $2; total = $3
    if ($4 > gt) gt = $4
    if ($5 > util) util = $5
    if ($6 > pw) pw = $6
    if ($7 > ct) ct = $7; n++
  } END {
    printf ">> over %d samples: peak %d / %d MiB (%d MiB spare), GPU max %d C, CPU max %d C, max util %d%%, max power %.0f W\n",
      n, mem, total, total - mem, gt, ct, util, pw
  }' "$samples"
}

echo ">> job running; following logs"
kubectl -n bench wait --for=condition=Ready pod -l job-name=throughput-cuda-arm --timeout=10m || true
kubectl -n bench logs -f job/throughput-cuda-arm || true

while true; do
  if [ -s "$abort_file" ]; then
    echo ">> $(cat "$abort_file")" >&2
    summarize
    exit 3
  fi
  succeeded="$(kubectl -n bench get job throughput-cuda-arm -o jsonpath='{.status.succeeded}' 2>/dev/null || true)"
  failed="$(kubectl -n bench get job throughput-cuda-arm -o jsonpath='{.status.failed}' 2>/dev/null || true)"
  [ "${succeeded:-0}" -ge 1 ] && break
  if [ "${failed:-0}" -ge 1 ]; then
    echo "job throughput-cuda-arm failed" >&2
    summarize
    exit 1
  fi
  sleep 5
done
kill "$sampler_pid" 2>/dev/null || true
sampler_pid=""

kubectl -n bench cp "corpus-seed:/data/results/throughput-$label.json" "$out_dir/throughput-$label.json"
summarize
echo ">> wrote $out_dir/throughput-$label.json and $samples"
