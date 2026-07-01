#!/usr/bin/env bash
# Pop-side NUM_PARALLEL sweep for the Ollama parameter-tuning campaign.
# Edits the LaunchAgent plist, restarts Ollama, and runs the matching default +
# agentic benchmark configs locally.
#
# Run on pop (this MacBook) from the repo root.
set -euo pipefail

PLIST="${PLIST:-${HOME}/Library/LaunchAgents/com.user.ollama-serve.plist}"
OUTPUT_DIR="${OUTPUT_DIR:-benchmarks/results}"
NP_VALUES=(1 3 6 8)

log() {
  echo "[run-pop-np-sweep] $*"
}

set_num_parallel() {
  local np="$1"
  log "updating LaunchAgent OLLAMA_NUM_PARALLEL=${np}"

  # Replace the existing NUM_PARALLEL value in the plist env section.
  /usr/libexec/PlistBuddy -c "Set :EnvironmentVariables:OLLAMA_NUM_PARALLEL ${np}" "$PLIST" || \
    /usr/libexec/PlistBuddy -c "Add :EnvironmentVariables:OLLAMA_NUM_PARALLEL string ${np}" "$PLIST"

  # Ensure the load timeout is short on pop; if the key is missing, add it.
  /usr/libexec/PlistBuddy -c "Set :EnvironmentVariables:OLLAMA_LOAD_TIMEOUT 2m" "$PLIST" 2>/dev/null || \
    /usr/libexec/PlistBuddy -c "Add :EnvironmentVariables:OLLAMA_LOAD_TIMEOUT string 2m" "$PLIST"

  launchctl unload "$PLIST" 2>/dev/null || true
  launchctl load -w "$PLIST"

  log "waiting for ollama to settle"
  sleep 5
  until curl -s http://localhost:11434/api/tags >/dev/null 2>&1; do
    sleep 2
  done
}

run_config() {
  local config="$1"
  local log_file="${OUTPUT_DIR}/pop-${config}.log"
  log "running ${config}"
  uv run --with aiohttp python benchmarks/ollama/tools/concurrency-bench.py \
    --config "benchmarks/ollama/configs/${config}.toml" \
    --output-dir "${OUTPUT_DIR}" \
    --no-gpu-sampling \
    > "${log_file}" 2>&1
  log "finished ${config}; log at ${log_file}"
}

capture_env() {
  local np="$1"
  local out="${OUTPUT_DIR}/pop-np${np}-env.json"
  cat > "$out" <<ENV_EOF
{
  "num_parallel": "${np}",
  "context_length": "$(launchctl getenv OLLAMA_CONTEXT_LENGTH || echo unknown)",
  "keep_alive": "$(launchctl getenv OLLAMA_KEEP_ALIVE || echo unknown)",
  "load_timeout": "$(launchctl getenv OLLAMA_LOAD_TIMEOUT || echo unknown)",
  "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
ENV_EOF
  log "captured env to ${out}"
}

main() {
  mkdir -p "${OUTPUT_DIR}"

  if [[ ! -f "$PLIST" ]]; then
    log "ERROR: LaunchAgent plist not found at ${PLIST}"
    exit 1
  fi

  for np in "${NP_VALUES[@]}"; do
    set_num_parallel "$np"
    capture_env "$np"
    run_config "pop-np${np}-default"
    run_config "pop-np${np}-agentic"
  done

  log "sweep complete; results in ${OUTPUT_DIR}"
}

main "$@"
