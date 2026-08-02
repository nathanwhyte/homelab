#!/usr/bin/env bash
# Authoritative gemma4 comparison matrix on pop (PROJ-1003), 2026-07-31.
#
# WHY THIS EXISTS. The 2026-07-28 matrix states its own limits: 8.3% endpoint
# drift on a byte-identical repeated config, row position as a confound, and
# "differences under ~8% are not resolvable". Separately, decode numbers taken
# from different harnesses do not compose — gemma4:26b-mxfp8 reads 77.07 tok/s
# under the agentic workload (num_predict 16384) and 88.7-105.0 under the
# prefill ladder (num_predict 512) on the same machine. Any table mixing those
# is quoting two different measurements under one column heading.
#
# This run fixes all three:
#
#   1. ONE HARNESS for the contested numbers. prefill-size-breakdown.py is the
#      only source of both prefill and decode here, so every cell in the table
#      comes from an identical request shape. concurrency-bench.py is NOT mixed
#      in; the concurrency/usable-rate columns stay in the 07-28 report where
#      they were measured, and are not restated alongside these.
#
#   2. REPEATS WITH ROTATED ORDER inside each tier, so no model is
#      systematically early (cool machine) or late (hot machine). Report the
#      median across repeats; the spread IS the error bar — the number the
#      07-28 matrix was missing.
#
#   3. AN EXPLICIT DRIFT CONTROL PER TIER. gemma4:12b-mlx runs first and last
#      in every tier, byte-identical. That within-tier delta is a direct
#      measurement of drift under that tier's thermal load, replacing the
#      07-28 report's untested thermal-throttling hypothesis. Wall clock and
#      `pmset -g therm` are captured per row so drift can be correlated rather
#      than assumed.
#
# TIER ORDER IS DELIBERATE — HIGHEST DECISION VALUE FIRST. Each tier is
# self-contained (own controls, own repeats), so stopping after any tier leaves
# a complete, error-barred result rather than a fraction of everything:
#
#   A  daily drivers @131K   the ONLY rows that inform a live config decision,
#                            and a regime no prior benchmark has measured.
#                            opencode (opencode.jsonc) and pi (pi/agent/
#                            models.json) both declare "context": 131072 and
#                            both default to gemma4:coding-26b, observed
#                            resident at 36 GB there. Run this even if nothing
#                            else runs.
#   B  26b quant pair @32K   same two families at the standard context, which
#                            separates "is nvfp4 better" from "is 131K the
#                            problem". Without B, tier A cannot attribute its
#                            own result.
#   C  12b dense family      validates the independently measured 570 GB/s
#                            bandwidth ceiling. bf16 does no dequantization, so
#                            it is the closest thing to a pure memory-bandwidth
#                            reference in the family. Cheap rows.
#   D  31b dense             completeness only. Already disqualified on prefill
#                            (~190 tok/s => ~6.8 min to first token on a real
#                            77.5k-token turn). Slowest tier, lowest value.
#
# gemma4:31b-mxfp8 is absent on purpose: deleted 2026-07-31 after measuring
# 9.61 tok/s (n=1) and 42 GB resident at 8k prompts. Being re-pulled elsewhere.
#
# Usage:
#   ./run-pop-gemma4-authoritative.sh            # all tiers, A->D
#   ./run-pop-gemma4-authoritative.sh A B        # just the decision-relevant ones
#   REPEATS=1 ./run-pop-gemma4-authoritative.sh A   # quick look, no error bar
#
# Run on pop from the repo root. Safe to kill between rows; every row writes
# its own JSON, so nothing already finished is lost.
set -euo pipefail

OUTPUT_DIR="${OUTPUT_DIR:-benchmarks/results/authoritative-20260731}"
REPEATS="${REPEATS:-3}"
COOLDOWN="${COOLDOWN:-30}"
COUNT="${COUNT:-27}"
CONTROL="gemma4:12b-mlx"

# tier:ctx:model,model,...   (rough per-repeat cost in the comments)
#
# NOTE (2026-08-02, dotfiles): the bare `gemma4:coding-26b` tag was
# repurposed as a MOVING daily-driver alias — it now points at nvfp4, not
# mxfp8 as it did when this script and quant-divergence.py were written. Use
# the immutable suffixed tags (`-mxfp8`, `-nvfp4`) here, never the bare name,
# so a future rename on the dotfiles side can't silently flip which model a
# rerun compares.
TIER_A="131072:gemma4:coding-26b-mxfp8,gemma4:coding-26b-nvfp4"    # ~30 min/repeat
TIER_B="32768:gemma4:26b-mxfp8,gemma4:26b-mlx"                     # ~20 min/repeat
TIER_C="32768:gemma4:12b-mlx,gemma4:12b-mxfp8,gemma4:12b-mlx-bf16" # ~25 min/repeat
TIER_D="32768:gemma4:31b-nvfp4"                                    # ~35 min/repeat

log() { echo "[authoritative $(date -u +%H:%M:%S)] $*"; }

slug() { echo "$1" | tr ':' '-' | tr '.' '_'; }

# Per-row environment capture. `launchctl getenv` does not read a LaunchAgent's
# own EnvironmentVariables dict, so read the live server process instead.
server_env() {
	local key="$1" pid
	pid="$(pgrep -f 'ollama serve' | head -1)"
	[[ -n "$pid" ]] || {
		echo "unknown"
		return
	}
	ps eww "$pid" 2>/dev/null | tr ' ' '\n' | sed -n "s/^${key}=//p" | head -1 | grep . || echo "unset"
}

run_row() {
	local model="$1" ctx="$2" tier="$3" rep="$4" label="$5"
	local s
	s="$(slug "$model")"
	local stem="${OUTPUT_DIR}/${tier}-r${rep}-${label}-${s}-ctx${ctx}"

	log "tier ${tier} | repeat ${rep} | ${label} | ${model} @ ctx=${ctx}"

	# Thermal / pressure proxy alongside the row, since 07-28 captured none.
	{
		echo "model=${model}"
		echo "ctx=${ctx}"
		echo "tier=${tier}"
		echo "repeat=${rep}"
		echo "label=${label}"
		echo "started=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
		echo "uptime=$(uptime | tr -s ' ')"
		pmset -g therm 2>/dev/null | tr -s ' ' || echo "therm=unavailable"
		sysctl vm.swapusage 2>/dev/null
		echo "num_parallel=$(server_env OLLAMA_NUM_PARALLEL)"
		echo "kv_cache_type=$(server_env OLLAMA_KV_CACHE_TYPE)"
		echo "ollama_version=$(ollama --version 2>/dev/null | head -1)"
	} >"${stem}.env.txt"

	if ! uv run --with aiohttp python benchmarks/ollama/tools/prefill-size-breakdown.py \
		--model "$model" \
		--num-ctx "$ctx" \
		--count "$COUNT" \
		--output "${stem}.json" \
		>"${stem}.log" 2>&1; then
		log "  ROW FAILED: ${model} (see ${stem}.log) — continuing"
	fi

	# Residency at the end of the row, before the unload. This is how the 36 GB
	# figure for gemma4:coding-26b @131K was first seen.
	ollama ps >"${stem}.ps.txt" 2>&1 || true

	ollama stop "$model" >/dev/null 2>&1 || log "  warning: stop ${model} nonzero"
	sleep "$COOLDOWN"
}

run_tier() {
	local tier="$1" spec="$2"
	local ctx="${spec%%:*}"
	local csv="${spec#*:}"
	local models=()
	IFS=',' read -r -a models <<<"$csv"
	local n=${#models[@]}

	log "===== TIER ${tier} — ${n} model(s) @ ctx=${ctx}, ${REPEATS} repeat(s) ====="

	# Drift control, cool end of the tier.
	run_row "$CONTROL" 32768 "$tier" 0 "ctrl-open"

	for rep in $(seq 1 "$REPEATS"); do
		# Rotate order per repeat so row position averages out.
		local offset=$((rep - 1))
		for i in $(seq 0 $((n - 1))); do
			local idx=$(((i + offset) % n))
			run_row "${models[$idx]}" "$ctx" "$tier" "$rep" "m$(printf '%02d' "$i")"
		done
	done

	# Drift control, hot end. Delta vs ctrl-open is this tier's error bar.
	run_row "$CONTROL" 32768 "$tier" 0 "ctrl-close"

	log "===== TIER ${tier} COMPLETE — safe stopping point ====="
}

main() {
	local tiers=("$@")
	[[ ${#tiers[@]} -gt 0 ]] || tiers=(A B C D)

	mkdir -p "$OUTPUT_DIR"
	log "output dir: ${OUTPUT_DIR}"
	log "tiers: ${tiers[*]} | repeats=${REPEATS} count=${COUNT} cooldown=${COOLDOWN}s"

	for t in "${tiers[@]}"; do
		case "$t" in
		A) run_tier A "$TIER_A" ;;
		B) run_tier B "$TIER_B" ;;
		C) run_tier C "$TIER_C" ;;
		D) run_tier D "$TIER_D" ;;
		*)
			log "unknown tier '${t}' — expected A, B, C or D"
			exit 1
			;;
		esac
	done

	log "requested tiers complete; results in ${OUTPUT_DIR}"
	log "summarize: median across repeats per model; ctrl-open vs ctrl-close = drift"
}

main "$@"
