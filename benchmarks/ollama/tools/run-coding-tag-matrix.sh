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
set -euo pipefail

RESULTS_DIR="${1:-benchmarks/results/agentic-coding-$(date +%Y-%m-%d)}"
TOOLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BENCH="$TOOLS_DIR/agentic-coding-bench.py"

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
echo "results -> $RESULTS_DIR"
echo "primary: ${#PRIMARY_TAGS[@]} tags, sensitivity: ${#SENSITIVITY_TAGS[@]} tags"

for tag in "${PRIMARY_TAGS[@]}"; do
	echo ""
	echo "================================================================"
	echo "PRIMARY  $tag  (native thinking default)  $(date +%H:%M:%S)"
	echo "================================================================"
	python3 -u "$BENCH" --model "$tag" --tiers 1,2,3 --out "$RESULTS_DIR" || {
		echo "!! $tag failed; continuing with the remaining tags"
	}
	# Let the machine settle and ensure the next row is a genuine cold load.
	sleep 20
done

for tag in "${SENSITIVITY_TAGS[@]}"; do
	echo ""
	echo "================================================================"
	echo "SENSITIVITY  $tag  --no-think  $(date +%H:%M:%S)"
	echo "================================================================"
	python3 -u "$BENCH" --model "$tag" --tiers 1,2,3 --no-think \
		--out "$RESULTS_DIR/no-think" || {
		echo "!! $tag (no-think) failed; continuing"
	}
	sleep 20
done

echo ""
echo "matrix complete $(date +%H:%M:%S); results in $RESULTS_DIR"
