#!/usr/bin/env bash
# Pop small-MoE benchmark matrix (PROJ-1003).
# Pins OLLAMA_NUM_PARALLEL=3 once, then runs the per-model agentic configs
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
#
# 2026-07-28 re-run: laguna-xs-2.1 is back. It was dropped on 2026-07-13 for an
# upstream-acknowledged macOS/Metal empty-output bug; upstream #17291 (Metal
# inference) shipped in Ollama 0.32.3 and #17237 (Laguna MLX) in 0.32.5, and the
# model was re-verified generating correctly on pop against 0.32.5. The whole
# matrix is re-run on 0.32.5 so every row shares one environment — the 07-13
# numbers were taken on 0.31.1 and are not comparable to these.
#
# Thinking policy: models are scored in their native mode (no `think` key), so
# the original configs send byte-identical payloads to 07-13. The four
# gemma4:12b variants additionally get an explicit think-on/think-off pair to
# isolate reasoning cost at fixed model size.
#
# hermes3:8b and qwen3-coder:30b were dropped on 2026-07-28: neither advertises
# a `thinking` capability, and both were removed from this machine. Their
# configs are kept so the rows can be restored by re-pulling the models and
# uncommenting them; their 07-13 numbers remain the historical record.
MATRIX=(
	# --- native mode, one row each ---
	"north-mini-nvfp4|north-mini-code-1.0:mlx-nvfp4"
	"nemotron3-33b|nemotron3:33b"
	"qwen36-35b-mlx|qwen3.6:35b-mlx"
	"gemma4-26b-mxfp8|gemma4:26b-mxfp8"
	"qwen36-35b-coding-mxfp8|qwen3.6:35b-a3b-coding-mxfp8"
	"laguna-latest|laguna-xs-2.1:latest"
	"laguna-mxfp8|laguna-xs-2.1:mxfp8"
	"laguna-nvfp4|laguna-xs-2.1:nvfp4"
	# --- gemma4:12b quant sweep, think on/off pairs ---
	"gemma4-12b-think|gemma4:12b"
	"gemma4-12b-nothink|gemma4:12b"
	"gemma4-12b-mlx-think|gemma4:12b-mlx"
	"gemma4-12b-mlx-nothink|gemma4:12b-mlx"
	"gemma4-12b-mxfp8-think|gemma4:12b-mxfp8"
	"gemma4-12b-mxfp8-nothink|gemma4:12b-mxfp8"
	"gemma4-12b-mlx-bf16-think|gemma4:12b-mlx-bf16"
	"gemma4-12b-mlx-bf16-nothink|gemma4:12b-mlx-bf16"
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
	# No --tool-validate here: these configs run the `agentic` workload, which is
	# prose code-review prompts, not tool calls. The validator parses answers as
	# {"tool", "arguments"} JSON and would score every correct answer invalid.
	# Usability comes from the per-level `quality` block (usable_rate,
	# empty_answers, truncated) instead.
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
