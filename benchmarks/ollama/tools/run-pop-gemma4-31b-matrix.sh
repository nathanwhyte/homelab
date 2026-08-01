#!/usr/bin/env bash
# Pop gemma4:31b dense benchmark matrix (PROJ-1003).
#
# Two rows: the large dense gemma4 in MLX mxfp8 and nvfp4. Same workload,
# sampling, context and output budget as pop-moe-gemma4-26b-mxfp8, so these
# slot into the 2026-07-28 matrix table as directly comparable rows — the
# question they answer is dense-31b vs MoE-26b at equal settings, and mxfp8 vs
# nvfp4 within the dense model.
#
# Structure copied from run-pop-moe-matrix.sh: pin OLLAMA_NUM_PARALLEL=3 once
# (the value the 07-28 matrix ran at; the daily-driver plist sits at 2), run the
# configs serially with an explicit `ollama stop` + cooldown so only one model is
# resident at a time, and restore the plist on exit including on failure.
#
# Run on pop (this MacBook) from the repo root.
set -euo pipefail

PLIST="${PLIST:-${HOME}/Library/LaunchAgents/com.user.ollama-serve.plist}"
OUTPUT_DIR="${OUTPUT_DIR:-benchmarks/results}"
NUM_PARALLEL="${NUM_PARALLEL:-3}"
MODEL_COOLDOWN="${MODEL_COOLDOWN:-30}"

# slug|model — slug maps to configs/pop-moe-<slug>-agentic.toml, model to `ollama stop`.
#
# gemma4:31b-mxfp8 was dropped and deleted on 2026-07-31 before this matrix ran.
# Its config is not retained (unlike the 07-28 drops), because the reason is a
# property of the tag rather than of this machine's memory ceiling:
#
#   measured @ num_ctx=32768, C=1 : 9.61 tok/s decode, 32 GB resident
#   measured @ 8k prompts         : 42 GB resident, above the 37-39 GB zone
#                                   where the 07-28 mxfp8 tags collapsed
#   MLX streaming probe, same box : 570 GB/s achievable peak
#
# At 32 GB of weights a purely bandwidth-bound dense decode should reach
# 570/32 = 17.8 tok/s. It managed 9.61 — an implied 307 GB/s, or 54% of peak —
# so this tag is NOT bandwidth-bound and was losing ~1.85x to a slow kernel
# path before residency ever became the binding constraint. That refines the
# 07-28 "large mxfp8 tags are infeasible" finding: the degradation is graded
# and starts well below the 37 GB cliff that report identified.
#
# For contrast the nvfp4 tag implies 960 GB/s, i.e. 1.68x ABOVE the measured
# peak, which is only possible by emitting more than one token per weight pass
# — MTP is active and accepting. Together those two factors (1.85 x 1.68 =
# 3.11) account for the observed 5.55x decode gap that the 1.78x weight-size
# ratio alone does not explain.
MATRIX=(
	"gemma4-31b-nvfp4|gemma4:31b-nvfp4"
)

log() {
	echo "[run-pop-gemma4-31b-matrix] $*"
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
	local out="${OUTPUT_DIR}/pop-gemma4-31b-np${np}-env.json"
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
	local slug="$1" model="$2"
	local config="benchmarks/ollama/configs/pop-moe-${slug}-agentic.toml"
	local log_file="${OUTPUT_DIR}/pop-moe-${slug}.log"

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
		run_model "${entry%%|*}" "${entry##*|}"
	done

	log "matrix complete; results in ${OUTPUT_DIR}"
}

main "$@"
