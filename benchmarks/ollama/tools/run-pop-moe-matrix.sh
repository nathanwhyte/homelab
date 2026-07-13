#!/usr/bin/env bash
# Pop small-MoE benchmark matrix (PROJ-1003).
# Pins OLLAMA_NUM_PARALLEL=3 once, then runs the six per-model agentic configs
# serially with an explicit `ollama stop` + cooldown between models so only one
# model is resident at a time. Modeled on run-pop-np-sweep.sh.
#
# Run on pop (this MacBook) from the repo root.
set -euo pipefail

PLIST="${PLIST:-${HOME}/Library/LaunchAgents/com.user.ollama-serve.plist}"
OUTPUT_DIR="${OUTPUT_DIR:-benchmarks/results}"
NUM_PARALLEL="${NUM_PARALLEL:-3}"
MODEL_COOLDOWN="${MODEL_COOLDOWN:-30}"

# slug|model — slug maps to configs/pop-moe-<slug>-agentic.toml, model to `ollama stop`.
# laguna-xs-2.1 dropped 2026-07-13: upstream-acknowledged macOS/Metal empty-output
# bug (poolside readme banner); raw:true workaround yields degenerate output. Revisit
# after the upstream fix. See pop-moe-recommended-params.txt / results report.
MATRIX=(
  "north-mini-nvfp4|north-mini-code-1.0:mlx-nvfp4"
  "nemotron3-33b|nemotron3:33b"
  "qwen3-coder-30b|qwen3-coder:30b"
  "hermes3-8b|hermes3:8b"
  "qwen36-35b-mlx|qwen3.6:35b-mlx"
)

log() {
  echo "[run-pop-moe-matrix] $*"
}

set_num_parallel() {
  local np="$1"
  log "updating LaunchAgent OLLAMA_NUM_PARALLEL=${np}"

  /usr/libexec/PlistBuddy -c "Set :EnvironmentVariables:OLLAMA_NUM_PARALLEL ${np}" "$PLIST" ||
    /usr/libexec/PlistBuddy -c "Add :EnvironmentVariables:OLLAMA_NUM_PARALLEL string ${np}" "$PLIST"

  /usr/libexec/PlistBuddy -c "Set :EnvironmentVariables:OLLAMA_LOAD_TIMEOUT 2m" "$PLIST" 2>/dev/null ||
    /usr/libexec/PlistBuddy -c "Add :EnvironmentVariables:OLLAMA_LOAD_TIMEOUT string 2m" "$PLIST"

  launchctl unload "$PLIST" 2>/dev/null || true
  launchctl load -w "$PLIST"

  log "waiting for ollama to settle"
  sleep 5
  until curl -s http://localhost:11434/api/tags >/dev/null 2>&1; do
    sleep 2
  done
}

capture_env() {
  local np="$1"
  local out="${OUTPUT_DIR}/pop-moe-np${np}-env.json"
  cat >"$out" <<ENV_EOF
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

run_model() {
  local slug="$1" model="$2"
  local config="benchmarks/ollama/configs/pop-moe-${slug}-agentic.toml"
  local log_file="${OUTPUT_DIR}/pop-moe-${slug}.log"

  if [[ ! -f "$config" ]]; then
    log "ERROR: config not found: ${config}"
    exit 1
  fi

  log "running ${slug} (${model})"
  uv run --with aiohttp python benchmarks/ollama/tools/concurrency-bench.py \
    --config "$config" \
    --output-dir "$OUTPUT_DIR" \
    --no-gpu-sampling \
    >"$log_file" 2>&1
  log "finished ${slug}; log at ${log_file}"

  ollama stop "$model" || log "warning: ollama stop ${model} returned nonzero"
  log "cooldown ${MODEL_COOLDOWN}s before next model"
  sleep "$MODEL_COOLDOWN"
}

main() {
  mkdir -p "$OUTPUT_DIR"

  if [[ ! -f "$PLIST" ]]; then
    log "ERROR: LaunchAgent plist not found at ${PLIST}"
    exit 1
  fi

  set_num_parallel "$NUM_PARALLEL"
  capture_env "$NUM_PARALLEL"

  for entry in "${MATRIX[@]}"; do
    run_model "${entry%%|*}" "${entry##*|}"
  done

  log "matrix complete; results in ${OUTPUT_DIR}"
}

main "$@"
