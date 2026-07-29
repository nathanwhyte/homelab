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
# Output budget: num_predict is 16384, up from the 2048 used on 2026-07-13.
# A first 0.32.5 pass at 2048 showed the cap was the dominant effect rather
# than a bound: qwen3.6:35b-mlx truncated 7-8 of every 9 requests and its
# time-to-first-answer (36.9s) matched 2048/55.3tps (37.0s) almost exactly —
# it reasoned through the entire budget and only began answering at the
# ceiling, yielding a 22-44% usable rate. north-mini, nemotron3 and gemma4:26b
# truncated 2-5 of 9. At 2048 those rows measure "how fast can it fill the
# budget", not "how fast can it answer". 16384 lets every model stop naturally,
# which makes a truncation a real finding about the model.
#
# This deliberately breaks output-budget comparability with the 07-13 table.
# That table was already incomparable on runtime (0.31.1 vs 0.32.5); this run
# is internally consistent, and that is the comparison that matters.
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
	"laguna-latest|laguna-xs-2.1:latest"
	"laguna-nvfp4|laguna-xs-2.1:nvfp4"
	# Dropped 2026-07-28 as infeasible, not as a null result:
	#   qwen36-35b-coding-mxfp8 (37 GB) — every request hit the 300s timeout
	#   laguna-mxfp8            (39 GB) — measured 1.14 tok/s, so one 16384-token
	#                                     request needs ~4 hours
	# Both are MLX mxfp8 at 37-39 GB. Smaller mxfp8 tags are healthy on the same
	# machine and stay in the matrix (gemma4:26b-mxfp8 27 GB -> 67 tok/s,
	# gemma4:12b-mxfp8 13 GB -> 42 tok/s), so this is a size effect on 64 GB
	# unified memory, not a defect in the mxfp8 format. Configs retained.
	# --- matched daily-driver config for the baseline ---
	# qwen3.6:35b-mlx is deployed with thinking disabled, so the native row above
	# does not represent it. Pairs with qwen36-35b-mlx to isolate thinking.
	"qwen36-35b-mlx-nothink|qwen3.6:35b-mlx"
	# --- gemma4:12b quant sweep, think on/off pairs ---
	# The plain GGUF gemma4:12b pair is omitted: the sweep's question is how the
	# MLX quants compare to each other, and the configs remain if a GGUF
	# reference row is wanted later.
	"gemma4-12b-mlx-think|gemma4:12b-mlx"
	"gemma4-12b-mlx-nothink|gemma4:12b-mlx"
	"gemma4-12b-mxfp8-think|gemma4:12b-mxfp8"
	"gemma4-12b-mxfp8-nothink|gemma4:12b-mxfp8"
	"gemma4-12b-mlx-bf16-think|gemma4:12b-mlx-bf16"
	"gemma4-12b-mlx-bf16-nothink|gemma4:12b-mlx-bf16"
	# --- variance check: row 1 repeated last, identical config ---
	# north-mini reported 86.6 then 72.1 tok/s across two runs of the same
	# config (~17%) while nemotron3 moved ~2%. Several models here sit within
	# 17% of each other, so that swing has to be quantified before the table
	# can support cross-model ranking.
	"north-mini-nvfp4-repeat|north-mini-code-1.0:mlx-nvfp4"
)

log() {
	echo "[run-pop-moe-matrix] $*"
}

# PlistBuddy rewrites the plist in canonical form, destroying its XML comments —
# on 2026-07-28 that stripped com.user.ollama-serve.plist from 178 lines to 54,
# deleting the BUG-1024 / IMPR-1057 rationale it documents as its own canonical
# source of truth. The file is tracked in dotfiles, so the damage lands in git.
# Back it up before touching it and restore on exit, including on failure.
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

# Read one OLLAMA_* setting from the *running server process*.
#
# `launchctl getenv` reads the global launchd environment, NOT a LaunchAgent's
# own EnvironmentVariables dict — so it returned empty for every per-agent
# setting and the 2026-07-28 run committed an env capture with blank
# context_length, keep_alive and load_timeout. Reading the live process is
# authoritative: it reflects what the server is actually using, including any
# drift from the plist on disk.
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
	local out="${OUTPUT_DIR}/pop-moe-np${np}-env.json"
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
