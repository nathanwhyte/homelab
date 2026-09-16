#!/usr/bin/env bash
# FIM + VLM dual-use benchmark on timmy's RX 9070 XT (IDEA-1105).
#
# Two deepseek-coder-v2 16B-lite tags cannot co-reside on the 16 GB card, so
# "FIM and VLM at the same time" means ONE resident runner serving both. This
# runs fim-contention-probe.py in two legs:
#
#   baseline  deepseek-coder-v2:fim       A (FIM alone) — today's serving tag
#   dual-use  deepseek-coder-v2:instruct  A,B,D,F,G — FIM alone, under decode,
#             under prefill, under 1 and 2 OV-shaped summarize loops
#
# Loading the instruct tag evicts FIM (OLLAMA_MAX_LOADED_MODELS=1), so editor
# autocomplete is down for the dual-use leg. The EXIT trap unloads the instruct
# tag and re-pins deepseek-coder-v2:fim with keep_alive -1, pass or fail.
#
# Run from the repo root on any LAN/Tailscale host:
#   benchmarks/ollama/tools/run-fim-vlm-dual-use.sh [REPS]
# Env: OLLAMA_HOST (default http://192.168.1.19:11434), OUTPUT_DIR,
#      DUAL_CONDITIONS (default A,B,D,F,G), VLM_DOC, VLM_TOKENS.
set -euo pipefail

REPS="${1:-8}"
export OLLAMA_HOST="${OLLAMA_HOST:-http://192.168.1.19:11434}"
FIM_TAG="deepseek-coder-v2:fim"
DUAL_TAG="deepseek-coder-v2:instruct"
DUAL_CONDITIONS="${DUAL_CONDITIONS:-A,B,D,F,G}"
OUTPUT_DIR="${OUTPUT_DIR:-benchmarks/results/fim-vlm-dual-use-$(date +%Y%m%d-%H%M)}"
PROBE="benchmarks/ollama/tools/fim-contention-probe.py"

log() {
  echo "[run-fim-vlm-dual-use] $*"
}

api() {
  curl -fsS -m 300 "${OLLAMA_HOST}$1" -H 'Content-Type: application/json' -d "$2"
}

restore_fim() {
  log "restoring ${FIM_TAG} (unload ${DUAL_TAG}, re-pin keep_alive -1)"
  api /api/generate "{\"model\":\"${DUAL_TAG}\",\"keep_alive\":0}" >/dev/null || true
  api /api/generate "{\"model\":\"${FIM_TAG}\",\"keep_alive\":-1,\"stream\":false}" >/dev/null
  curl -fsS -m 10 "${OLLAMA_HOST}/api/ps" | python3 -c 'import sys,json; print([(m["name"], m["expires_at"]) for m in json.load(sys.stdin)["models"]])'
}

mkdir -p "$OUTPUT_DIR"
api /api/show "{\"model\":\"${DUAL_TAG}\"}" >/dev/null ||
  {
    log "${DUAL_TAG} missing; build it from llama/ollama/deepseek-coder-v2-instruct.Modelfile"
    exit 1
  }
trap restore_fim EXIT

log "baseline: ${FIM_TAG}, condition A, reps=${REPS}"
FIM_MODEL="$FIM_TAG" CONDITIONS=A PROBE_JSON="${OUTPUT_DIR}/baseline-fim.json" \
  python3 "$PROBE" "$REPS" | tee "${OUTPUT_DIR}/baseline-fim.log"

log "dual-use: ${DUAL_TAG}, conditions ${DUAL_CONDITIONS}, reps=${REPS} (FIM evicted)"
FIM_MODEL="$DUAL_TAG" BG_MODEL="$DUAL_TAG" CONDITIONS="$DUAL_CONDITIONS" \
  PROBE_JSON="${OUTPUT_DIR}/dual-use-instruct.json" \
  python3 "$PROBE" "$REPS" | tee "${OUTPUT_DIR}/dual-use-instruct.log"

log "results in ${OUTPUT_DIR}"
