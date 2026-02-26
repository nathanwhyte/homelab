#!/usr/bin/env bash

set -euo pipefail

LLAMA_DIR="$HOME/code/homelab/llama"
BENCH_SCRIPT="$LLAMA_DIR/benchmarks/run_benchmarks.py"

if [ ! -x "$(command -v kubectl)" ]; then
  echo "kubectl not installed."
  exit 1
fi

if ! kubectl cluster-info > /dev/null 2>&1; then
  echo "kubectl not connected to a cluster."
  exit 1
fi

if [ ! -f "$BENCH_SCRIPT" ]; then
  echo "Benchmark runner not found: $BENCH_SCRIPT"
  exit 1
fi

echo "Running llama benchmark suite..."
echo "  Cases: 12/category"
echo "  Categories: codegen, tech_qa, basic_qa"
echo "  Overall weighting: 60% accuracy, 40% speed"

PYTHONUNBUFFERED=1 python3 "$BENCH_SCRIPT" --max-cases-per-category 12 "$@"
