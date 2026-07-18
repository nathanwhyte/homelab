#!/usr/bin/env bash
# Run the full Vulkan backend + higher-bit quant benchmark matrix on timmy.
# Benchmarks run against the live `ollama` deployment (llama/ollama-deployment.yaml),
# which IS the Vulkan backend in production — the separate -vulkan variant was retired
# 2026-07-17 once prod itself became Vulkan. For a clean single-model, 8-wide bench,
# set OLLAMA_MAX_LOADED_MODELS=1 and OLLAMA_NUM_PARALLEL=8 on the deployment for the
# run (prod now defaults to 2 / 4). The bench-scripts-vulkan ConfigMap must be current.
set -euo pipefail

NS="${NS:-llama}"
OUTPUT_DIR="${OUTPUT_DIR:-benchmarks/results}"

# Order: baseline default/agentic first, then higher-bit quants.
# 2026-07-01: Q5_K_M and Q6_K model tags are not available in the Unsloth
# HF repo, so the higher-bit quant configs are skipped. Re-enable them once
# the GGUFs are published or converted locally.
CONFIGS=(
  cluster-vulkan-default
  cluster-vulkan-agentic
  # cluster-vulkan-q5km-default
  # cluster-vulkan-q6k-default
  # cluster-vulkan-q5km-agentic
  # cluster-vulkan-q6k-agentic
)

log() {
  echo "[run-vulkan-benchmark-jobs] $*"
}

run_config_job() {
  local config="$1"
  local log_file="${OUTPUT_DIR}/cluster-${config}.log"
  log "launching Job for ${config}"

  kubectl delete job ollama-benchmark-vulkan -n "$NS" --ignore-not-found=true
  sed "s/\${CONFIG}/${config}/g" benchmarks/ollama/manifests/benchmark-vulkan-job.yaml | \
    kubectl apply -f - -n "$NS"

  log "waiting for Job to complete"
  if kubectl wait --for=condition=complete job/ollama-benchmark-vulkan -n "$NS" --timeout=6h; then
    kubectl logs job/ollama-benchmark-vulkan -n "$NS" > "${log_file}" 2>&1
    local pod_name
    pod_name=$(kubectl get pods -n "$NS" -l job-name=ollama-benchmark-vulkan -o jsonpath='{.items[0].metadata.name}')
    if [ -n "$pod_name" ]; then
      mkdir -p "${OUTPUT_DIR}/pod"
      kubectl cp "${NS}/${pod_name}:/bench/benchmarks/results" "${OUTPUT_DIR}/pod" >/dev/null 2>&1 || true
      log "copied pod results to ${OUTPUT_DIR}/pod"
    fi
    log "finished ${config}; log at ${log_file}"
  else
    log "ERROR: Job did not complete successfully"
    kubectl logs job/ollama-benchmark-vulkan -n "$NS" > "${log_file}" 2>&1 || true
    kubectl describe job ollama-benchmark-vulkan -n "$NS" >> "${log_file}" 2>&1 || true
    return 1
  fi
}

capture_env() {
  local out="${OUTPUT_DIR}/cluster-vulkan-env.json"
  kubectl get deployment ollama -n "$NS" -o json | \
    jq '{
      num_parallel: .spec.template.spec.containers[] | select(.name=="ollama") | .env[] | select(.name=="OLLAMA_NUM_PARALLEL") | .value,
      context_length: .spec.template.spec.containers[] | select(.name=="ollama") | .env[] | select(.name=="OLLAMA_CONTEXT_LENGTH") | .value,
      kv_cache_type: .spec.template.spec.containers[] | select(.name=="ollama") | .env[] | select(.name=="OLLAMA_KV_CACHE_TYPE") | .value,
      keep_alive: .spec.template.spec.containers[] | select(.name=="ollama") | .env[] | select(.name=="OLLAMA_KEEP_ALIVE") | .value,
      load_timeout: .spec.template.spec.containers[] | select(.name=="ollama") | .env[] | select(.name=="OLLAMA_LOAD_TIMEOUT") | .value,
      max_loaded_models: .spec.template.spec.containers[] | select(.name=="ollama") | .env[] | select(.name=="OLLAMA_MAX_LOADED_MODELS") | .value,
      vk_visible_devices: .spec.template.spec.containers[] | select(.name=="ollama") | .env[] | select(.name=="GGML_VK_VISIBLE_DEVICES") | .value,
      image: .spec.template.spec.containers[] | select(.name=="ollama") | .image,
      timestamp: now | todate
    }' > "$out"
  log "captured Vulkan env to ${out}"
}

main() {
  mkdir -p "${OUTPUT_DIR}"
  capture_env
  for config in "${CONFIGS[@]}"; do
    run_config_job "$config"
  done
  log "Vulkan matrix complete; results in ${OUTPUT_DIR}"
}

main "$@"
