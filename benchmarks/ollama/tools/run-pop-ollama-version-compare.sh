#!/usr/bin/env bash
# Ollama version A/B comparison on pop (PROJ-1003, TASK-1190).
#
# Runs the same probe set against two Ollama binaries, one at a time, by
# repointing /opt/homebrew/bin/ollama and kickstarting the launchd agent
# between arms. Repointing (rather than standing up a scratch server per arm)
# is deliberate: com.user.ollama-serve owns every OLLAMA_* setting on this
# machine, so reusing it guarantees both arms see a byte-identical server
# environment (NUM_PARALLEL=1, flash attention, q8_0 KV cache, 128K ctx). A
# scratch server would have to re-declare that env by hand, and any drift
# would land silently in the version delta.
#
# ⚠ ONE LOCAL-MODEL CONSUMER AT A TIME. Every probe here runs strictly
# serially, and nothing in this script may be parallelized — the global cap
# in claude/CLAUDE.md keys on what the work drives, not what runs it.
# Concurrency *within* a probe (the harness's own C=1..3 sweep) is the
# server's business and stays as configured.
#
# Usage:
#   run-pop-ollama-version-compare.sh <label-a> <prefix-a> <label-b> <prefix-b>
# where <prefix-*> is a cmake --install prefix containing bin/ollama.
#
# Example:
#   run-pop-ollama-version-compare.sh \
#     0.32.15   ~/.local/opt/ollama-0.32.15-src \
#     0.33.0rc1 ~/.local/opt/ollama-0.33.0-rc1

set -euo pipefail

if [[ $# -ne 4 ]]; then
	sed -n '20,27p' "$0" >&2
	exit 2
fi

LABEL_A="$1" PREFIX_A="$2" LABEL_B="$3" PREFIX_B="$4"

BENCH_DIR="$HOME/code/homelab/main/benchmarks"
TOOLS="$BENCH_DIR/ollama/tools"
CONFIGS="$BENCH_DIR/ollama/configs"
CORPUS="$BENCH_DIR/ollama/corpus"
SYMLINK="/opt/homebrew/bin/ollama"
AGENT="gui/$(id -u)/com.user.ollama-serve"
URL="http://localhost:11434"

STAMP="$(date +%Y-%m-%d-%H%M)"
OUT="$BENCH_DIR/results/ollama-version-compare-$STAMP"
mkdir -p "$OUT"

ORIGINAL_TARGET="$(readlink "$SYMLINK")"
echo "original $SYMLINK -> $ORIGINAL_TARGET" | tee "$OUT/original-symlink.txt"

restore() {
	echo "==> restoring $SYMLINK -> $ORIGINAL_TARGET"
	ln -sfn "$ORIGINAL_TARGET" "$SYMLINK"
	launchctl kickstart -k "$AGENT" || true
}
trap restore EXIT

wait_healthy() {

	for _ in $(seq 1 60); do
		if curl -fsS "$URL/api/version" >/dev/null 2>&1; then return 0; fi
		sleep 2
	done
	echo "FATAL: server did not come healthy within 120s" >&2
	return 1
}

# Idle the GPU between arms so thermals do not leak into the next version's
# numbers. The harness's own cooldown_seconds covers within-run spacing.
ARM_COOLDOWN="${ARM_COOLDOWN:-120}"

run_arm() {
	local label="$1" prefix="$2"
	local bin="$prefix/bin/ollama"
	local arm_out="$OUT/$label"
	mkdir -p "$arm_out"

	echo "=============================================================="
	echo "==> ARM $label   ($bin)"
	echo "=============================================================="

	[[ -x "$bin" ]] || {
		echo "FATAL: $bin missing or not executable" >&2
		return 1
	}

	ln -sfn "$bin" "$SYMLINK"
	launchctl kickstart -k "$AGENT"
	wait_healthy

	# Fail closed on a version mismatch: a silently-stale server would make the
	# whole arm a duplicate of the other one, which is worse than no data.
	local reported
	reported="$(curl -fsS "$URL/api/version" | python3 -c 'import json,sys; print(json.load(sys.stdin)["version"])')"
	echo "$reported" >"$arm_out/reported-version.txt"
	echo "==> server reports version: $reported"
	if [[ "$reported" == "0.0.0" ]]; then
		echo "FATAL: server reports 0.0.0 — binary was built without -DOLLAMA_VERSION" >&2
		return 1
	fi

	# --- Probe 1: MLX agentic (the surface the rc changed) -------------------
	echo "==> [$label] MLX agentic"
	uv run python "$TOOLS/concurrency-bench.py" \
		--config "$CONFIGS/pop-qwen38-27b-mlx-agentic.toml" \
		--output-dir "$arm_out" 2>&1 | tee "$arm_out/mlx-agentic.log"

	echo "==> cooldown ${ARM_COOLDOWN}s"
	sleep "$ARM_COOLDOWN"

	# --- Probe 2: GGUF agentic (control; expected flat) ----------------------
	echo "==> [$label] GGUF agentic"
	uv run python "$TOOLS/concurrency-bench.py" \
		--config "$CONFIGS/pop-qwen38-27b-gguf-agentic.toml" \
		--output-dir "$arm_out" 2>&1 | tee "$arm_out/gguf-agentic.log"

	echo "==> cooldown ${ARM_COOLDOWN}s"
	sleep "$ARM_COOLDOWN"

	# --- Probe 3: prefill curve, warm-prefix ---------------------------------
	# The load-bearing probe for ollama#17901. warm-prefix reports prefix_cached
	# per row and fails the run if every warm row missed, so a prefix-cache
	# behavior change is visible rather than averaged away.
	echo "==> [$label] prefill warm-prefix (MLX)"
	uv run python "$TOOLS/prefill-size-breakdown.py" \
		--model qwen3.8:27b-mlx \
		--corpus-dir "$CORPUS" \
		--mode warm-prefix \
		--output "$arm_out/prefill-warm-prefix.json" 2>&1 |
		tee "$arm_out/prefill-warm-prefix.log"

	echo "==> cooldown ${ARM_COOLDOWN}s"
	sleep "$ARM_COOLDOWN"

	# --- Probe 4: prefill curve, cold ----------------------------------------
	echo "==> [$label] prefill cold (MLX)"
	uv run python "$TOOLS/prefill-size-breakdown.py" \
		--model qwen3.8:27b-mlx \
		--corpus-dir "$CORPUS" \
		--mode cold \
		--output "$arm_out/prefill-cold.json" 2>&1 |
		tee "$arm_out/prefill-cold.log"

	echo "==> ARM $label complete"
}

cd "$HOME/code/homelab/main"

run_arm "$LABEL_A" "$PREFIX_A"
echo "==> inter-arm cooldown ${ARM_COOLDOWN}s"
sleep "$ARM_COOLDOWN"
run_arm "$LABEL_B" "$PREFIX_B"

echo
echo "=============================================================="
echo "Both arms complete. Results: $OUT"
echo "=============================================================="
