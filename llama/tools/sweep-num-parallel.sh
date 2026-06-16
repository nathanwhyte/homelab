#!/usr/bin/env bash
# Sweep OLLAMA_NUM_PARALLEL values and run the concurrency benchmark at each.
#
# This script mutates the live Ollama Deployment. Run only when the cluster can
# tolerate a ~60-90s Ollama restart between benchmark runs.
#
# Usage:
#   llama/tools/sweep-num-parallel.sh [NP_VALUES...]
#
# Examples:
#   llama/tools/sweep-num-parallel.sh 2 4 6
#   NP_VALUES="2 4 6" BENCHMARK_ARGS="--mixed --max-concurrency 6" llama/tools/sweep-num-parallel.sh
#
# Environment:
#   NP_VALUES        - space-separated NUM_PARALLEL values (default: 2 4 6)
#   BENCHMARK_ARGS   - extra args passed to ollama-concurrency-benchmark.py
#   OUTPUT_DIR       - directory for per-NP result files (default: ./benchmark-results)
#   NAMESPACE        - Ollama namespace (default: llama)
#   DEPLOYMENT       - Ollama Deployment name (default: ollama)
#   KUBECTL          - kubectl binary/alias (default: kubectl)

set -euo pipefail

NAMESPACE="${NAMESPACE:-llama}"
DEPLOYMENT="${DEPLOYMENT:-ollama}"
KUBECTL="${KUBECTL:-kubectl}"
OUTPUT_DIR="${OUTPUT_DIR:-./benchmark-results}"
NP_VALUES="${NP_VALUES:-2 4 6}"
BENCHMARK_ARGS="${BENCHMARK_ARGS:-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BENCHMARK_PY="${SCRIPT_DIR}/ollama-concurrency-benchmark.py"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
OUTDIR="${OUTPUT_DIR}/${TIMESTAMP}"
mkdir -p "${OUTDIR}"

log() {
  echo "[$(date +%H:%M:%S)] $*"
}

current_num_parallel() {
  ${KUBECTL} get deployment "${DEPLOYMENT}" -n "${NAMESPACE}" -o jsonpath='{.spec.template.spec.containers[?(@.name=="ollama")].env[?(@.name=="OLLAMA_NUM_PARALLEL")].value}'
}

set_num_parallel() {
  local target="$1"
  log "Patching ${DEPLOYMENT}/${NAMESPACE} OLLAMA_NUM_PARALLEL -> ${target}"
  ${KUBECTL} set env deployment "${DEPLOYMENT}" -n "${NAMESPACE}" "OLLAMA_NUM_PARALLEL=${target}"
}

wait_for_rollout() {
  log "Waiting for rollout to complete ..."
  ${KUBECTL} rollout status deployment "${DEPLOYMENT}" -n "${NAMESPACE}" --timeout=300s
}

restore_num_parallel() {
  local original="$1"
  log "Restoring OLLAMA_NUM_PARALLEL -> ${original}"
  set_num_parallel "${original}"
  wait_for_rollout
}

run_benchmark() {
  local np="$1"
  local json_out="${OUTDIR}/np-${np}.json"
  local csv_out="${OUTDIR}/np-${np}.csv"
  log "Running benchmark for NUM_PARALLEL=${np}"
  # shellcheck disable=SC2086
  uv run --with aiohttp python "${BENCHMARK_PY}" \
    --output-json "${json_out}" \
    --output-csv "${csv_out}" \
    ${BENCHMARK_ARGS}
}

ORIGINAL_NP="$(current_num_parallel)"
log "Original OLLAMA_NUM_PARALLEL=${ORIGINAL_NP}"
log "Output directory: ${OUTDIR}"

# Ensure we restore the original value on exit or error.
trap 'restore_num_parallel "${ORIGINAL_NP}"' EXIT

for np in ${NP_VALUES}; do
  set_num_parallel "${np}"
  wait_for_rollout
  run_benchmark "${np}"
done

log "All sweeps complete. Results in ${OUTDIR}"
