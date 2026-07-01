#!/usr/bin/env bash
# Cluster-side NUM_PARALLEL sweep using Kubernetes Jobs.
#
# Jobs are not tied to a local `kubectl exec` session, so long agentic configs
# with large num_predict can't be killed by client-side timeouts. Results live
# in the Job's Pod logs and in its emptyDir; this script copies them out after
# each run.
#
# Run from the repo root.
set -euo pipefail

NS="${NS:-llama}"
OUTPUT_DIR="${OUTPUT_DIR:-benchmarks/results}"
NP_VALUES=(1 3 6 8)

log() {
  echo "[run-cluster-np-sweep-jobs] $*"
}

set_num_parallel() {
  local np="$1"
  log "patching OLLAMA_NUM_PARALLEL=${np}"
  kubectl set env deployment ollama -n "$NS" "OLLAMA_NUM_PARALLEL=${np}"
  kubectl rollout status deployment ollama -n "$NS" --timeout=300s
  log "waiting for model to become ready"
  sleep 5
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

run_config_job() {
  local config="$1"
  local log_file="${OUTPUT_DIR}/cluster-${config}.log"
  log "launching Job for ${config}"

  # Delete any previous Job with the same name so this run is idempotent.
  kubectl delete job ollama-benchmark -n "$NS" --ignore-not-found=true

  # Substitute the CONFIG env var inline and apply.
  sed "s/\${CONFIG}/${config}/g" benchmarks/ollama/manifests/benchmark-job.yaml | \
    kubectl apply -f - -n "$NS"

  log "waiting for Job to complete"
  if kubectl wait --for=condition=complete job/ollama-benchmark -n "$NS" --timeout=6h; then
    log "Job completed; copying logs and results"
    kubectl logs job/ollama-benchmark -n "$NS" > "${log_file}" 2>&1
    # Copy results out of the Job's pod before it is deleted by the next run.
    local pod_name
    pod_name=$(kubectl get pods -n "$NS" -l job-name=ollama-benchmark -o jsonpath='{.items[0].metadata.name}')
    if [ -n "$pod_name" ]; then
      mkdir -p "${OUTPUT_DIR}/pod"
      kubectl cp "${NS}/${pod_name}:/bench/benchmarks/results" "${OUTPUT_DIR}/pod" >/dev/null 2>&1 || true
      log "copied pod results to ${OUTPUT_DIR}/pod"
    fi
    log "finished ${config}; log at ${log_file}"
  else
    log "ERROR: Job did not complete successfully"
    kubectl logs job/ollama-benchmark -n "$NS" > "${log_file}" 2>&1 || true
    kubectl describe job ollama-benchmark -n "$NS" >> "${log_file}" 2>&1 || true
    return 1
  fi
}

main() {
  mkdir -p "${OUTPUT_DIR}"

  for np in "${NP_VALUES[@]}"; do
    set_num_parallel "$np"
    capture_env "$np"
    run_config_job "cluster-np${np}-default"
    run_config_job "cluster-np${np}-agentic"
  done

  log "sweep complete; results in ${OUTPUT_DIR}"
}

main "$@"
