#!/usr/bin/env bash
# Cluster-side NUM_PARALLEL sweep for the Ollama parameter-tuning campaign.
# Patches OLLAMA_NUM_PARALLEL on the timmy deployment, waits for rollout, and
# runs the matching default + agentic benchmark configs inside the
# ollama-benchmark pod.
#
# Run from the repo root (the worktree containing benchmarks/).
set -euo pipefail

NS="${NS:-llama}"
OUTPUT_DIR="${OUTPUT_DIR:-benchmarks/results}"
NP_VALUES=(1 3 6 8)

log() {
  echo "[run-cluster-np-sweep] $*"
}

ensure_pod() {
  if ! kubectl get pod ollama-benchmark -n "$NS" >/dev/null 2>&1; then
    log "creating ollama-benchmark pod"
    kubectl apply -f benchmarks/ollama/manifests/benchmark-pod.yaml -n "$NS"
    kubectl wait --for=condition=Ready pod/ollama-benchmark -n "$NS" --timeout=120s
  fi
}

set_num_parallel() {
  local np="$1"
  log "patching OLLAMA_NUM_PARALLEL=${np}"
  kubectl set env deployment ollama -n "$NS" "OLLAMA_NUM_PARALLEL=${np}"
  kubectl rollout status deployment ollama -n "$NS" --timeout=300s
  log "waiting for model to become ready"
  sleep 5
}

run_config() {
  local config="$1"
  local log_file="${OUTPUT_DIR}/cluster-${config}.log"
  log "running ${config}"
  # Use the local amdsmi Python library for GPU metrics instead of Prometheus.
  # amdsmi is installed in the benchmark pod from the mounted ROCm tree and
  # exposes RDNA4-specific fields like throttle_status, temperature_mem, and
  # average_umc_activity that the sysfs-based amdgpu-exporter does not.
  kubectl exec ollama-benchmark -n "$NS" -- \
    bash -c "cd /bench && python -u concurrency-bench.py --config ${config}.toml --amdsmi --no-gpu-sampling" \
    > "${log_file}" 2>&1
  log "finished ${config}; log at ${log_file}"
  # Copy any result directories the benchmark wrote to the pod's emptyDir out
  # to the host so they survive pod restarts/deletes.
  local pod_results="/bench/benchmarks/results"
  if kubectl exec ollama-benchmark -n "$NS" -- test -d "${pod_results}" >/dev/null 2>&1; then
    mkdir -p "${OUTPUT_DIR}/pod"
    kubectl cp "llama/ollama-benchmark:${pod_results}" "${OUTPUT_DIR}/pod" >/dev/null 2>&1 || true
    log "copied pod results to ${OUTPUT_DIR}/pod"
  fi
}

capture_env() {
  local np="$1"
  local out="${OUTPUT_DIR}/cluster-np${np}-env.json"
  kubectl get deployment ollama -n "$NS" -o json | \
    jq --arg np "${np}" '{
      num_parallel: $np,
      context_length: .spec.template.spec.containers[] | select(.name=="ollama") | .env[] | select(.name=="OLLAMA_CONTEXT_LENGTH") | .value,
      kv_cache_type: .spec.template.spec.containers[] | select(.name=="ollama") | .env[] | select(.name=="OLLAMA_KV_CACHE_TYPE") | .value,
      keep_alive: .spec.template.spec.containers[] | select(.name=="ollama") | .env[] | select(.name=="OLLAMA_KEEP_ALIVE") | .value,
      load_timeout: .spec.template.spec.containers[] | select(.name=="ollama") | .env[] | select(.name=="OLLAMA_LOAD_TIMEOUT") | .value,
      timestamp: now | todate
    }' > "$out"
  log "captured env to ${out}"
}

main() {
  mkdir -p "${OUTPUT_DIR}"
  ensure_pod

  for np in "${NP_VALUES[@]}"; do
    set_num_parallel "$np"
    capture_env "$np"
    run_config "cluster-np${np}-default"
    run_config "cluster-np${np}-agentic"
  done

  log "sweep complete; results in ${OUTPUT_DIR}"
}

main "$@"
