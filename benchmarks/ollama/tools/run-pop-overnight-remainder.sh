#!/usr/bin/env bash
# Remainder of the 2026-07-31 gemma4 investigation (PROJ-1003), run 2026-08-01.
#
# Tiers A and B landed on 2026-07-31 and answered the live config question:
# gemma4:coding-26b-nvfp4 beats its mxfp8 twin at both 32K and 131K (+2.8%
# prefill at BOTH contexts, +15.7%/+22.6% decode), and the quant-divergence run
# showed the twins produce byte-identical output on 23/24 prompts. The config
# flip is done. What follows is everything that was deferred.
#
# STEP ORDER IS BY VALUE, NOT BY THE ORIGINAL TIER LETTERS. Each step is
# independent and writes its own artifacts, so stopping between steps loses
# nothing already finished:
#
#   1  TIER C   12b dense family. Validates the independently measured
#               570 GB/s bandwidth ceiling and the ~11.3 TFLOP/s prefill
#               constant against a THIRD active-parameter point (12.4B, vs the
#               MoE's 4B and the 31b's 31.7B). bf16 does no dequantization, so
#               it is the closest thing to a pure bandwidth reference here.
#
#   2  DETERM   Characterize the 2026-07-31 finding that gemma4:12b-mxfp8 is
#               NON-DETERMINISTIC at temperature 0 / top_k 1 / fixed seed
#               (1 of 4 probes differed). That is a real defect — it makes the
#               tag unusable for anything reproducibility-dependent, and it
#               blocked the 12b 4-bit-vs-8-bit fidelity comparison entirely.
#               12 probes per model puts a rate on it and checks whether any
#               other tag shares the problem. Cheap; run before the long steps.
#
#   3  QUALITY  agentic-coding-bench on the 26b MoE and the 31b dense. This is
#               the ONLY axis that could still favor the 31b, which loses on
#               every speed metric (~190 tok/s prefill => ~6.8 min to first
#               token on a real 77.5k-token turn). Verifier-scored, so the
#               `solved` column is mechanical, not judged.
#               ⚠ n=6 tasks. That resolves a large capability gap; it CANNOT
#               resolve a few-percent difference. Read it as a screen, not a
#               ranking.
#
#   4  TIER D   31b dense speed rows. Completeness only — the model is already
#               disqualified for interactive use and the feasibility probe
#               pinned its decode at ~53 tok/s. Deliberately last.
#
# Run from the repo root. Detached, so it survives the session ending.
set -uo pipefail # NOT -e: a failed step must not abandon the remaining steps

cd "$(dirname "$0")/../../.." || exit 1

# Every rerun gets its own dated results dir — never write into a prior run's
# directory, whose artifacts back an already-published report.
R="${OUTPUT_DIR:-benchmarks/results/authoritative-$(date +%Y%m%d-%H%M%S)}"
export OUTPUT_DIR="$R" # run-pop-gemma4-authoritative.sh honors this
mkdir -p "$R"

log() { echo "[remainder $(date '+%H:%M:%S')] $*"; }

# Per-step outcomes, aggregated at the end. Because -e is deliberately off, a
# failed step does NOT abort the run — so the final banner must be earned from
# these statuses, never printed unconditionally. Statuses:
#   completed        step produced its result
#   inconclusive     step ran but refused/failed to produce a scientific answer
#                    (e.g. quant-divergence REFUSING on its determinism control)
#   failed           infrastructure failure (nonzero exit, no refusal recorded)
STEP_NAMES=()
STEP_STATUSES=()
record() {
	STEP_NAMES+=("$1")
	STEP_STATUSES+=("$2")
	log "step $1 => $2"
}

log "=== STEP 1/4 — TIER C (12b dense family) ==="
if bash benchmarks/ollama/tools/run-pop-gemma4-authoritative.sh C; then
	record "1 tier-C" completed
else
	record "1 tier-C" "failed (exit $?)"
fi

log "=== STEP 2/4 — determinism characterization (12 probes/model) ==="
# --determinism-probes 12 with a nonsense reference pairing is fine: the script
# runs its control over every model BEFORE any comparison, and exits non-zero if
# the reference fails. We only care about the control output here — but a
# REFUSING exit is an inconclusive scientific outcome, not a completed step.
uv run python benchmarks/ollama/tools/quant-divergence.py \
	--reference gemma4:12b-mlx-bf16 \
	--candidates gemma4:12b-mxfp8,gemma4:12b-mlx \
	--determinism-probes 12 \
	--output "$R/divergence-12b-deep.json" \
	2>&1 | tee "$R/determinism-12b.log"
rc=$?
if [ "$rc" -eq 0 ]; then
	record "2 determinism" completed
elif grep -q "REFUSING" "$R/determinism-12b.log"; then
	record "2 determinism" "inconclusive (divergence comparison REFUSED; control output still valid)"
else
	record "2 determinism" "failed (exit $rc)"
fi
for m in gemma4:12b-mlx-bf16 gemma4:12b-mxfp8 gemma4:12b-mlx; do ollama stop "$m" >/dev/null 2>&1; done
sleep 20

log "=== STEP 3/4 — agentic quality bench ==="
step3=completed
for t in gemma4:26b-mxfp8 gemma4:31b-nvfp4; do
	s="${t//:/-}"
	log "  agentic: $t"
	uv run --with aiohttp python benchmarks/ollama/tools/agentic-coding-bench.py \
		--model "$t" --tiers 1,2,3 --out "$R" \
		>"$R/agentic-${s}.log" 2>&1
	rc=$?
	log "  agentic $t exit=$rc"
	[ "$rc" -ne 0 ] && step3="failed ($t exit $rc)"
	ollama stop "$t" >/dev/null 2>&1
	sleep 30
done
record "3 agentic" "$step3"

log "=== STEP 4/4 — TIER D (31b dense) ==="
if bash benchmarks/ollama/tools/run-pop-gemma4-authoritative.sh D; then
	record "4 tier-D" completed
else
	record "4 tier-D" "failed (exit $?)"
fi

log "=== SUMMARY ==="
overall=0
for i in "${!STEP_NAMES[@]}"; do
	log "  step ${STEP_NAMES[$i]}: ${STEP_STATUSES[$i]}"
	[ "${STEP_STATUSES[$i]}" = completed ] || overall=1
done
if [ "$overall" -eq 0 ]; then
	log "=== ALL REMAINING WORK COMPLETE ==="
else
	log "=== FINISHED WITH FAILED OR INCONCLUSIVE STEPS — read statuses above ==="
fi
exit "$overall"
