#!/usr/bin/env bash
# Agentic-coding comparison across every `:coding` tag on pop (PROJ-1003).
#
# Strictly sequential, and that is not a performance oversight: every row drives
# the locally-served model, and the homelab rule is at most one local-model
# consumer in flight. Parallel rows would contend for one GPU and multiply RAM
# pressure for no throughput.
#
# Two passes, deliberately kept apart:
#   PRIMARY     — every tag in its native thinking default. This is the ranking.
#   SENSITIVITY — the qwen pair again with --no-think, because BUG-1024 means the
#                 deployed Claude Code preset hard-disables thinking. Those rows
#                 answer "what does the deployed config do", and must never be
#                 merged into the primary ranking: they use the thinking-mode
#                 sampling row with thinking off, which is not a published Qwen
#                 configuration.
#
# Usage: ./run-coding-tag-matrix.sh [results_dir]
#
# BENCH_BASE selects the Ollama endpoint (default the local server). For the
# TASK-1186 timmy rows, run this wrapper on pop with BENCH_BASE pointing at
# timmy's native Ollama service — the harness sandbox is macOS-only, so the
# client stays on pop while the model runs remote. The endpoint lands in each
# result's provenance block (base_url) and in row-status.tsv.
set -euo pipefail

BENCH_BASE="${BENCH_BASE:-http://localhost:11434}"

TOOLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BENCH="$TOOLS_DIR/agentic-coding-bench.py"
# Anchored to the repository, not the caller's cwd. The documented invocation
# runs this from the tools directory, where a relative default would have written
# results to tools/benchmarks/results.
REPO_ROOT="$(cd "$TOOLS_DIR/../../.." && pwd)"
# Timestamped to the second, not date-only: a same-day rerun into a date-only
# directory kept the previous attempt's JSON while truncating only the
# manifest, so stale and current rows coexisted with a manifest describing
# only the newest attempt.
RESULTS_DIR="${1:-$REPO_ROOT/benchmarks/results/agentic-coding-$(date +%Y-%m-%d-%H%M%S)}"
# Repeats matter more than they look: t1b failed and then passed on consecutive
# runs with an identical seed, so the MLX path is not deterministic and a
# single pass is a pilot, not a ranking.
REPEATS="${REPEATS:-3}"
# The bench refuses --repeats < 1 too; guard here as well so REPEATS=0 cannot
# mark every row "ok" and declare the matrix complete.
if ! [[ $REPEATS =~ ^[0-9]+$ ]] || [ "$REPEATS" -lt 1 ]; then
	echo "REPEATS=$REPEATS is not a positive integer; refusing" >&2
	exit 2
fi
MANIFEST="$RESULTS_DIR/row-status.tsv"
FAILED_ROWS=0

PRIMARY_TAGS=(
	"gemma4:coding-12b"
	"gemma4:coding-26b"
	"qwen3.6:coding"
	"qwen3.6:coding-gguf"
	"nemotron3:coding"
	"laguna:coding"
)

SENSITIVITY_TAGS=(
	"qwen3.6:coding"
	"qwen3.6:coding-gguf"
)

mkdir -p "$RESULTS_DIR"
: >"$MANIFEST"
echo "results -> $RESULTS_DIR"
echo "endpoint: $BENCH_BASE"
printf '# endpoint\t%s\n' "$BENCH_BASE" >>"$MANIFEST"
echo "primary: ${#PRIMARY_TAGS[@]} tags, sensitivity: ${#SENSITIVITY_TAGS[@]} tags, repeats=$REPEATS"
if [ "$REPEATS" -lt 2 ]; then
	echo "NOTE: REPEATS=$REPEATS -- this run is a pilot, not a ranking"
fi

for tag in "${PRIMARY_TAGS[@]}"; do
	echo ""
	echo "================================================================"
	echo "PRIMARY  $tag  (native thinking default)  $(date +%H:%M:%S)"
	echo "================================================================"
	if python3 -u "$BENCH" --model "$tag" --base "$BENCH_BASE" --tiers 1,2,3 --repeats "$REPEATS" \
		--out "$RESULTS_DIR"; then
		printf 'primary\t%s\tok\n' "$tag" >>"$MANIFEST"
	else
		status=$?
		printf 'primary\t%s\tFAILED(exit %d)\n' "$tag" "$status" >>"$MANIFEST"
		FAILED_ROWS=$((FAILED_ROWS + 1))
		echo "!! $tag failed (exit $status); continuing with the remaining tags"
	fi
	# Let the machine settle and ensure the next row is a genuine cold load.
	sleep 20
done

for tag in "${SENSITIVITY_TAGS[@]}"; do
	echo ""
	echo "================================================================"
	echo "SENSITIVITY  $tag  --no-think  $(date +%H:%M:%S)"
	echo "================================================================"
	if python3 -u "$BENCH" --model "$tag" --base "$BENCH_BASE" --tiers 1,2,3 --no-think \
		--repeats "$REPEATS" --out "$RESULTS_DIR/no-think"; then
		printf 'sensitivity\t%s\tok\n' "$tag" >>"$MANIFEST"
	else
		status=$?
		printf 'sensitivity\t%s\tFAILED(exit %d)\n' "$tag" "$status" >>"$MANIFEST"
		FAILED_ROWS=$((FAILED_ROWS + 1))
		echo "!! $tag (no-think) failed (exit $status); continuing"
	fi
	sleep 20
done

echo ""
echo "row status:"
cat "$MANIFEST"
expected=$((${#PRIMARY_TAGS[@]} + ${#SENSITIVITY_TAGS[@]}))
# Comment lines (the endpoint header) don't count toward the row total.
actual=$(grep -cv '^#' "$MANIFEST" || true)
echo ""
if [ "$FAILED_ROWS" -gt 0 ] || [ "$actual" -ne "$expected" ]; then
	echo "matrix INCOMPLETE $(date +%H:%M:%S): $FAILED_ROWS failed, $actual/$expected rows recorded"
	exit 1
fi
echo "matrix complete $(date +%H:%M:%S); $actual/$expected rows in $RESULTS_DIR"
