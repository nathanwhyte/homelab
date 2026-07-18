#!/usr/bin/env bash
# Run the full Vulkan backend + higher-bit quant benchmark matrix on timmy.
# Benchmarks run against the live `ollama` deployment (llama/ollama-deployment.yaml),
# which IS the Vulkan backend in production — the separate -vulkan variant was retired
# 2026-07-17 once prod itself became Vulkan.
#
# The bench requires single-model isolation at NUM_PARALLEL=8 (the CONFIGS below drive
# concurrency through 8 and downstream reports label these as "np=8"). Production now
# runs MAX_LOADED_MODELS=2 / NUM_PARALLEL=4, so this script PATCHES the deployment to
# 1 / 8 for the run and RESTORES the original values on exit (via trap), rather than
# leaving it to a manual step that, if skipped, silently measures 4-slot queueing under
# two-model VRAM pressure. This bounces the prod ollama pod twice (patch + restore) —
# expect brief local-inference downtime around the run. The bench-scripts-vulkan
# ConfigMap must be current. Override targets with BENCH_MAX_LOADED / BENCH_NUM_PARALLEL.
set -euo pipefail

NS="${NS:-llama}"
DEPLOY="${DEPLOY:-ollama}"
OUTPUT_DIR="${OUTPUT_DIR:-benchmarks/results}"

# The configuration the benchmark must run under (single model, 8 slots).
BENCH_MAX_LOADED="${BENCH_MAX_LOADED:-1}"
BENCH_NUM_PARALLEL="${BENCH_NUM_PARALLEL:-8}"

# Populated by snapshot_deployment_config; restored by trap on exit.
ORIG_MAX_LOADED=""
ORIG_NUM_PARALLEL=""
CONFIG_PATCHED=0

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

# Read an env value from the ollama container of the deployment.
deploy_env() {
  kubectl get deployment "$DEPLOY" -n "$NS" -o jsonpath="{.spec.template.spec.containers[?(@.name=='ollama')].env[?(@.name=='$1')].value}"
}

# Capture the live MAX_LOADED_MODELS / NUM_PARALLEL so the trap can restore them.
# Refuse to run if either is unreadable — without a snapshot we cannot restore.
snapshot_deployment_config() {
  ORIG_MAX_LOADED=$(deploy_env OLLAMA_MAX_LOADED_MODELS)
  ORIG_NUM_PARALLEL=$(deploy_env OLLAMA_NUM_PARALLEL)
  if [ -z "$ORIG_MAX_LOADED" ] || [ -z "$ORIG_NUM_PARALLEL" ]; then
    log "ERROR: could not read current OLLAMA_MAX_LOADED_MODELS/OLLAMA_NUM_PARALLEL from deployment/${DEPLOY}; refusing to patch (cannot guarantee restore)"
    exit 1
  fi
  log "snapshot: MAX_LOADED_MODELS=${ORIG_MAX_LOADED} NUM_PARALLEL=${ORIG_NUM_PARALLEL}"
}

# Patch the deployment to the benchmark config and wait for the rollout.
apply_bench_config() {
  log "patching deployment/${DEPLOY} to benchmark config MAX_LOADED_MODELS=${BENCH_MAX_LOADED} NUM_PARALLEL=${BENCH_NUM_PARALLEL} (bounces the prod pod)"
  kubectl set env "deployment/${DEPLOY}" -n "$NS" \
    "OLLAMA_MAX_LOADED_MODELS=${BENCH_MAX_LOADED}" \
    "OLLAMA_NUM_PARALLEL=${BENCH_NUM_PARALLEL}" >/dev/null
  CONFIG_PATCHED=1
  kubectl rollout status "deployment/${DEPLOY}" -n "$NS" --timeout=360s
}

# EXIT trap: put the deployment back the way we found it. Best-effort — a failed
# restore is logged loudly rather than masking the original exit status.
restore_deployment_config() {
  [ "$CONFIG_PATCHED" -eq 1 ] || return 0
  log "restoring deployment/${DEPLOY} to MAX_LOADED_MODELS=${ORIG_MAX_LOADED} NUM_PARALLEL=${ORIG_NUM_PARALLEL}"
  if kubectl set env "deployment/${DEPLOY}" -n "$NS" \
    "OLLAMA_MAX_LOADED_MODELS=${ORIG_MAX_LOADED}" \
    "OLLAMA_NUM_PARALLEL=${ORIG_NUM_PARALLEL}" >/dev/null; then
    kubectl rollout status "deployment/${DEPLOY}" -n "$NS" --timeout=360s ||
      log "WARN: restore rollout did not confirm — verify deployment/${DEPLOY} is back to ${ORIG_MAX_LOADED}/${ORIG_NUM_PARALLEL}"
  else
    log "WARN: restore of deployment/${DEPLOY} FAILED — manually set MAX_LOADED_MODELS=${ORIG_MAX_LOADED} NUM_PARALLEL=${ORIG_NUM_PARALLEL}"
  fi
}

# Fail-fast: abort unless the live server is exactly the benchmark config with no
# stray second model resident (which MAX_LOADED_MODELS=1 should preclude, but a
# lingering pinned runner would poison the measurement).
preflight_verify() {
  local pod live_ml live_np loaded
  live_ml=$(deploy_env OLLAMA_MAX_LOADED_MODELS)
  live_np=$(deploy_env OLLAMA_NUM_PARALLEL)
  if [ "$live_ml" != "$BENCH_MAX_LOADED" ] || [ "$live_np" != "$BENCH_NUM_PARALLEL" ]; then
    log "ERROR: preflight failed — deployment is ${live_ml}/${live_np}, expected ${BENCH_MAX_LOADED}/${BENCH_NUM_PARALLEL}"
    exit 1
  fi
  pod=$(kubectl get pod -n "$NS" -l "app=${DEPLOY}" -o jsonpath='{.items[0].metadata.name}')
  # ollama ps header is one line; count model rows after it.
  loaded=$(kubectl exec -n "$NS" "$pod" -c ollama -- ollama ps 2>/dev/null | tail -n +2 | grep -c . || true)
  if [ "${loaded:-0}" -gt 1 ]; then
    log "ERROR: preflight failed — ${loaded} models resident, expected <=1 under MAX_LOADED_MODELS=1:"
    kubectl exec -n "$NS" "$pod" -c ollama -- ollama ps 2>/dev/null | sed 's/^/[preflight] /'
    exit 1
  fi
  log "preflight OK: deployment ${live_ml}/${live_np}, ${loaded:-0} model(s) resident"
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
  trap restore_deployment_config EXIT
  snapshot_deployment_config
  apply_bench_config
  preflight_verify
  capture_env
  for config in "${CONFIGS[@]}"; do
    run_config_job "$config"
  done
  log "Vulkan matrix complete; results in ${OUTPUT_DIR}"
}

main "$@"
