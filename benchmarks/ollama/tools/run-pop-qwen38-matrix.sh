#!/usr/bin/env bash
# Pop qwen3.8:27b benchmark matrix (PROJ-1003, TASK-1189).
#
# Two rows: the 27 B dense qwen3.8 served as MLX/nvfp4 and as Q4_K_M GGUF.
# Both artifacts are 18 GB, which makes this the only unconfounded MLX-vs-GGUF
# pair in the 2026-05-08 matrix — everywhere else the format axis moves
# together with weight size. Same workload, sampling, context and output budget
# as the gemma4-31b and pop-moe rows, so these slot in as comparable rows.
#
# SCOPE NARROWED 2026-08-15 — MLX ONLY. The original design was an ABBA
# MLX/GGUF/GGUF/MLX sequence to cancel thermal drift between the two formats.
# That was abandoned mid-run at the user's direction: the GGUF row decodes at
# roughly a third of the MLX rate (~25 vs ~68 tok/s on a short probe), so each
# GGUF row ran ~35 min against the MLX row's ~14, and the pair would have cost
# ~100 min of wall clock to answer a format question that is secondary to the
# dense-vs-MoE one. The partial GGUF pos2 row was killed and discarded — no
# results directory was written, so nothing half-measured is on disk.
#
# The question this now answers:
#   Does a 27 B DENSE model beat the ~3 B-active MoE incumbents on pop?
#   Every candidate added since codestral:22b was retired as bandwidth-bound
#   (INFO-1090) has been MoE. This is the first fair re-test of that guidance.
#
# The MLX-vs-GGUF format comparison is NOT answered here. Do not infer it from
# the capability-probe decode numbers either — those were single short prompts,
# not the agentic workload. If it is picked up later it needs its own run.
#
# WHY TWO IDENTICAL ROWS: with only one model there is no cross-model drift to
# cancel, but a single sample is still a single sample. Two runs of the same
# config separated by a model reload and cooldown give (a) a reproducibility
# check and (b) a direct read on thermal drift, which per homelab PR #58 runs
# ~8.3% monotonic decline over a long serial session. Report the pair, not just
# the mean — if they disagree by more than a few percent that is the finding.
#
# Structure copied from run-pop-gemma4-31b-matrix.sh: pin OLLAMA_NUM_PARALLEL=3
# once, run configs serially with an explicit `ollama stop` + cooldown so only
# one model is resident at a time, and restore the plist on exit including on
# failure.
#
# Run on pop (this MacBook) from the repo root.
set -euo pipefail

PLIST="${PLIST:-${HOME}/Library/LaunchAgents/com.user.ollama-serve.plist}"
OUTPUT_DIR="${OUTPUT_DIR:-benchmarks/results}"
NUM_PARALLEL="${NUM_PARALLEL:-3}"
MODEL_COOLDOWN="${MODEL_COOLDOWN:-30}"

# slug|model|config — same config twice, for repeat + drift (see header).
# rep1 here is a fresh run; the discarded ABBA attempt's own pos1 result
# (ollama-pop-qwen38-27b-mlx-agentic-20260815-095045) is retained separately as
# a third, earlier sample of the identical config.
MATRIX=(
	"qwen38-27b-mlx-rep1|qwen3.8:27b-mlx|pop-qwen38-27b-mlx-agentic"
	"qwen38-27b-mlx-rep2|qwen3.8:27b-mlx|pop-qwen38-27b-mlx-agentic"
)

log() {
	echo "[run-pop-qwen38-matrix] $*"
}

# PlistBuddy rewrites the plist in canonical form, destroying its XML comments —
# see run-pop-moe-matrix.sh for the 2026-07-28 incident this guards against.
# The file is tracked in dotfiles, so the damage would land in git.
PLIST_BACKUP=""
restore_plist() {
	if [[ -n "$PLIST_BACKUP" && -f "$PLIST_BACKUP" ]]; then
		cp "$PLIST_BACKUP" "$PLIST"
		rm -f "$PLIST_BACKUP"
		log "restored original plist (comments and production NUM_PARALLEL)"
		launchctl bootout "gui/$(id -u)/com.user.ollama-serve" 2>/dev/null || true
		sleep 2
		launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || true
	fi
}
trap restore_plist EXIT

set_num_parallel() {
	local np="$1"
	PLIST_BACKUP="$(mktemp -t ollama-serve-plist)"
	cp "$PLIST" "$PLIST_BACKUP"
	log "backed up plist to ${PLIST_BACKUP} (restored on exit)"
	log "updating LaunchAgent OLLAMA_NUM_PARALLEL=${np}"

	/usr/libexec/PlistBuddy -c "Set :EnvironmentVariables:OLLAMA_NUM_PARALLEL ${np}" "$PLIST" ||
		/usr/libexec/PlistBuddy -c "Add :EnvironmentVariables:OLLAMA_NUM_PARALLEL string ${np}" "$PLIST"

	/usr/libexec/PlistBuddy -c "Set :EnvironmentVariables:OLLAMA_LOAD_TIMEOUT 5m" "$PLIST" 2>/dev/null ||
		/usr/libexec/PlistBuddy -c "Add :EnvironmentVariables:OLLAMA_LOAD_TIMEOUT string 5m" "$PLIST"

	launchctl unload "$PLIST" 2>/dev/null || true
	launchctl load -w "$PLIST"

	log "waiting for ollama to settle"
	sleep 5
	until curl -s http://localhost:11434/api/tags >/dev/null 2>&1; do
		sleep 2
	done
}

# Read one OLLAMA_* setting from the *running server process*. `launchctl getenv`
# reads the global launchd environment, not a LaunchAgent's own
# EnvironmentVariables dict, and returns empty for every per-agent setting.
server_env() {
	local key="$1" pid
	pid="$(pgrep -f 'ollama serve' | head -1)"
	[[ -n "$pid" ]] || {
		echo "unknown"
		return
	}
	ps eww "$pid" 2>/dev/null | tr ' ' '\n' | sed -n "s/^${key}=//p" | head -1 | grep . || echo "unset"
}

capture_env() {
	local np="$1"
	local out="${OUTPUT_DIR}/pop-qwen38-np${np}-env.json"
	cat >"$out" <<ENV_EOF
{
  "num_parallel_requested": "${np}",
  "num_parallel_effective": "$(server_env OLLAMA_NUM_PARALLEL)",
  "context_length": "$(server_env OLLAMA_CONTEXT_LENGTH)",
  "keep_alive": "$(server_env OLLAMA_KEEP_ALIVE)",
  "load_timeout": "$(server_env OLLAMA_LOAD_TIMEOUT)",
  "flash_attention": "$(server_env OLLAMA_FLASH_ATTENTION)",
  "kv_cache_type": "$(server_env OLLAMA_KV_CACHE_TYPE)",
  "max_loaded_models": "$(server_env OLLAMA_MAX_LOADED_MODELS)",
  "ollama_version": "$(ollama --version 2>/dev/null | head -1)",
  "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
ENV_EOF
	log "captured env to ${out}"
}

run_model() {
	local slug="$1" model="$2" config_name="$3"
	local config="benchmarks/ollama/configs/${config_name}.toml"
	local log_file="${OUTPUT_DIR}/pop-${slug}.log"

	if [[ ! -f "$config" ]]; then
		log "ERROR: config not found: ${config}"
		exit 1
	fi

	log "running ${slug} (${model})"
	# No --tool-validate: the `agentic` workload is prose code-review prompts,
	# not tool calls. Usability comes from the per-level `quality` block.
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
		IFS='|' read -r slug model config_name <<<"$entry"
		run_model "$slug" "$model" "$config_name"
	done

	log "matrix complete; results in ${OUTPUT_DIR}"
	log "compare rep1 vs rep2: agreement = reproducible, gap = thermal drift"
}

main "$@"
