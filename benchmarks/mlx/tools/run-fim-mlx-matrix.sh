#!/usr/bin/env bash
# MLX-direct FIM leg of the model matrix (TASK-1111): same harness, same
# salted workloads + per-family markers/stops as the Ollama run, but served
# by mlx_lm.server and driven via the harness `openai_completions` mode
# (/v1/completions streaming, client-side TTFT). Compare head-to-head with
# benchmarks/results/ollama-pop-fim-* at matched 4bit quant.
#
# mlx_lm.server serves ONE model per process; /v1/completions applies no
# chat template by default, so the FIM sentinel tokens in the prompt reach
# the model verbatim (the same raw+markers control path as the Ollama run).
set -euo pipefail

PORT=8081
HOST="http://localhost:$PORT"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BENCH="$SCRIPT_DIR/../../ollama/tools/concurrency-bench.py"
TMPDIR_CFG="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_CFG"; [ -n "${SERVER_PID:-}" ] && kill "$SERVER_PID" 2>/dev/null || true' EXIT

# mlx-community base 4bit weights (match Ollama q4). model ~ workload ~ stops ~ slug
MATRIX=(
  'mlx-community/Qwen2.5-Coder-1.5B-4bit~edit_prediction_qwen~["<|endoftext|>","<|fim_pad|>","<|file_sep|>","<|repo_name|>"]~qwen25-coder-1_5b'
  'mlx-community/Qwen2.5-Coder-3B-4bit~edit_prediction_qwen~["<|endoftext|>","<|fim_pad|>","<|file_sep|>","<|repo_name|>"]~qwen25-coder-3b'
  'mlx-community/codegemma-2b-4bit~edit_prediction_codegemma~["<|file_separator|>","<end_of_turn>","<eos>"]~codegemma-2b'
)

for row in "${MATRIX[@]}"; do
  IFS='~' read -r model workload stops slug <<<"$row"
  echo "=== MLX FIM bench: $model ($workload) ==="

  # Launch the server (first run downloads weights from HF into the cache).
  mlx_lm.server --model "$model" --host 127.0.0.1 --port "$PORT" --log-level WARNING &
  SERVER_PID=$!

  # Wait for readiness — generous window to cover a cold HF download.
  ready=""
  for _ in $(seq 1 150); do
    if curl -sf "$HOST/v1/models" >/dev/null 2>&1; then ready=1; break; fi
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then echo "server died launching $model"; break; fi
    sleep 4
  done
  if [ -z "$ready" ]; then
    echo "FAILED: $model never became ready"
    kill "$SERVER_PID" 2>/dev/null || true; wait "$SERVER_PID" 2>/dev/null || true
    SERVER_PID=""
    continue
  fi

  cfg="$TMPDIR_CFG/$slug.toml"
  cat >"$cfg" <<EOF
[ollama]
url = "$HOST"
model = "$model"
api = "openai_completions"
num_ctx = 16384
num_predict = 128
temperature = 0.0
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
prefix = "mlx-pop-fim-$slug"
EOF
  uv run --with aiohttp python "$BENCH" --config "$cfg" --no-gpu-sampling || echo "FAILED bench: $model"

  kill "$SERVER_PID" 2>/dev/null || true
  wait "$SERVER_PID" 2>/dev/null || true
  SERVER_PID=""
  sleep 2   # let VRAM free before the next server
done

echo "=== MLX FIM matrix done — results in benchmarks/results/mlx-pop-fim-* ==="
