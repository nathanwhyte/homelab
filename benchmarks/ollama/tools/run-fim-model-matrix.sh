#!/usr/bin/env bash
# FIM model-selection matrix on pop (TASK-1111 / minuet model choice).
#
# Benchmarks the candidate FIM coder models one-at-a-time via the Ollama
# /api/generate raw+markers path with the correct per-family PSM sentinels
# (lib/prompts.py) and explicit per-family stop tokens (INFO-1005 — the fix
# for the Qwen2.5-Coder "doesn't stop in FIM" run-ons). FIM-realistic
# sampling: temperature 0, num_predict 128, raw=true, salted prompts.
#
# Each model is unloaded before the next loads (KEEP_ALIVE aside, we call
# `ollama stop`) so the run is isolated. Results land in benchmarks/results/.
set -euo pipefail

OLLAMA=/opt/homebrew/bin/ollama
HOST="http://localhost:11434"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BENCH="$SCRIPT_DIR/concurrency-bench.py"
TMPDIR_CFG="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_CFG"' EXIT

# model ~ workload ~ stop-tokens (TOML array) ~ slug
# NB: '~' is the field delimiter (the FIM/stop tokens themselves contain '|').
MATRIX=(
  'qwen2.5-coder:1.5b-base~edit_prediction_qwen~["<|endoftext|>","<|fim_pad|>","<|file_sep|>","<|repo_name|>"]~qwen25-coder-1_5b'
  'qwen2.5-coder:3b-base~edit_prediction_qwen~["<|endoftext|>","<|fim_pad|>","<|file_sep|>","<|repo_name|>"]~qwen25-coder-3b'
  'qwen2.5-coder:7b-base~edit_prediction_qwen~["<|endoftext|>","<|fim_pad|>","<|file_sep|>","<|repo_name|>"]~qwen25-coder-7b'
  'deepseek-coder-v2:16b-lite-base-q4_0~edit_prediction_deepseek~["<|EOT|>"]~deepseek-coder-v2-lite'
  'codegemma:2b~edit_prediction_codegemma~["<|file_separator|>","<end_of_turn>","<eos>"]~codegemma-2b'
  'codegemma:7b-code~edit_prediction_codegemma~["<|file_separator|>","<end_of_turn>","<eos>"]~codegemma-7b'
  'starcoder2:3b~edit_prediction_starcoder2~["<|endoftext|>","<file_sep>"]~starcoder2-3b'
  'starcoder2:7b~edit_prediction_starcoder2~["<|endoftext|>","<file_sep>"]~starcoder2-7b'
  'codestral:latest~edit_prediction_mistral~["[INST]","[/INST]","[PREFIX]","[MIDDLE]","[SUFFIX]"]~codestral-22b'
)

# Clear VRAM before starting.
for m in $($OLLAMA ps | awk 'NR>1{print $1}'); do $OLLAMA stop "$m" || true; done

for row in "${MATRIX[@]}"; do
  IFS='~' read -r model workload stops slug <<<"$row"
  cfg="$TMPDIR_CFG/$slug.toml"
  cat >"$cfg" <<EOF
[ollama]
url = "$HOST"
model = "$model"
num_ctx = 16384
num_predict = 128
temperature = 0.0
raw = true
stop = $stops

[workload]
name = "$workload"
max_concurrency = 1
requests_per_level = 15
repeats = 1
cooldown_seconds = 3

[prometheus]
url = ""
instance_label = ""
sample_interval_seconds = 2.0

[output]
dir = "benchmarks/results"
prefix = "ollama-pop-fim-$slug"
EOF
  echo "=== FIM bench: $model ($workload) ==="
  uv run --with aiohttp python "$BENCH" --config "$cfg" --no-gpu-sampling || echo "FAILED: $model"
  $OLLAMA stop "$model" || true   # isolate: unload before next model
done

echo "=== FIM matrix done — results in benchmarks/results/ollama-pop-fim-* ==="
