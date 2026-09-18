#!/usr/bin/env bash
# FIM + VLM dual-use benchmark on timmy's RX 9070 XT (IDEA-1105).
#
# Two deepseek-coder-v2 16B-lite tags cannot co-reside on the 16 GB card, so
# "FIM and VLM at the same time" means ONE resident runner serving both. This
# runs fim-contention-probe.py in up to three legs:
#
#   baseline  $BASELINE_TAG               A (FIM alone) — the pre-dual-use tag
#   dual-use  deepseek-coder-v2:instruct  A,B,D,F,G,H — FIM alone, under decode,
#             under prefill, under 1, 2 and 3 OV-shaped summarize loops
#   num_batch deepseek-coder-v2:instruct-nb<N>, one temporary tag per value in
#             NUM_BATCH_VARIANTS (e.g. "1024 2048"), SWEEP_CONDITIONS only;
#             the tags are deleted on exit
#
# Loading a benchmark tag evicts what the editor uses, so autocomplete is down
# for the dual-use and num_batch legs. The EXIT trap restores $RESIDENT_TAG with
# keep_alive -1, pass or fail.
#
# RESIDENT_TAG is NOT hardcoded, and must not be. It was deepseek-coder-v2:fim,
# then :instruct (homelab #127), and is qwen2.5-coder:fim since the OV pair
# landed (#130/#132) -- each time a hardcoded restore silently re-pinned a stale
# tag, leaving the editor to pay a reload on its next completion (BUG-1154).
# The default tracks RESIDENT_TAG in llama/host/ollama-warm.sh, which is the
# source of truth; override the env var for a host on another posture.
#
# Run from the repo root on any LAN/Tailscale host:
#   benchmarks/ollama/tools/run-fim-vlm-dual-use.sh [REPS]
# Env: OLLAMA_HOST (default http://192.168.1.19:11434), OUTPUT_DIR,
#      DUAL_CONDITIONS (default A,B,D,F,G,H), NUM_BATCH_VARIANTS (default none),
#      SWEEP_CONDITIONS (default A,F,H), VLM_DOC, VLM_TOKENS, PROBE (probe
#      path; override for testing). The probe exits non-zero when a condition's
#      background load was not sustained, which stops the run (cleanup still runs).
set -euo pipefail

REPS="${1:-8}"
export OLLAMA_HOST="${OLLAMA_HOST:-http://192.168.1.19:11434}"
# What cleanup() restores. Keep in step with RESIDENT_TAG in
# llama/host/ollama-warm.sh -- this is only its benchmark-side mirror.
RESIDENT_TAG="${RESIDENT_TAG:-qwen2.5-coder:fim}"
# The historical FIM tag the baseline leg measures. Deliberately separate from
# RESIDENT_TAG: the baseline is about the OLD serving tag, not what is pinned.
BASELINE_TAG="${BASELINE_TAG:-deepseek-coder-v2:fim}"
FIM_TAG="$BASELINE_TAG"
DUAL_TAG="deepseek-coder-v2:instruct"
DUAL_CONDITIONS="${DUAL_CONDITIONS:-A,B,D,F,G,H}"
NUM_BATCH_VARIANTS="${NUM_BATCH_VARIANTS:-}"
SWEEP_CONDITIONS="${SWEEP_CONDITIONS:-A,F,H}"
OUTPUT_DIR="${OUTPUT_DIR:-benchmarks/results/fim-vlm-dual-use-$(date +%Y%m%d-%H%M)}"
PROBE="${PROBE:-benchmarks/ollama/tools/fim-contention-probe.py}"
TEMP_TAGS=()

log() {
  echo "[run-fim-vlm-dual-use] $*"
}

api() {
  curl -fsS -m 300 "${OLLAMA_HOST}$1" -H 'Content-Type: application/json' -d "$2"
}

cleanup() {
  # ${arr[@]+...}: Bash 3.2 (macOS /bin/bash) treats "${arr[@]}" on an EMPTY
  # array as unbound under set -u, which aborted cleanup before FIM was restored.
  for tag in ${TEMP_TAGS[@]+"${TEMP_TAGS[@]}"}; do
    log "deleting temporary tag ${tag}"
    curl -fsS -m 30 -X DELETE "${OLLAMA_HOST}/api/delete" -d "{\"model\":\"${tag}\"}" || true
  done
  log "restoring ${RESIDENT_TAG} (re-pin keep_alive -1)"
  api /api/generate "{\"model\":\"${RESIDENT_TAG}\",\"keep_alive\":-1,\"stream\":false}" >/dev/null
  curl -fsS -m 10 "${OLLAMA_HOST}/api/ps" | python3 -c 'import sys,json; print([(m["name"], m["context_length"], m["expires_at"]) for m in json.load(sys.stdin)["models"]])'
}

run_leg() {
  # $1 label, $2 model tag, $3 conditions
  log "$1: $2, conditions $3, reps=${REPS}"
  FIM_MODEL="$2" BG_MODEL="$2" CONDITIONS="$3" PROBE_JSON="${OUTPUT_DIR}/$1.json" \
    python3 "$PROBE" "$REPS" | tee "${OUTPUT_DIR}/$1.log"
}

mkdir -p "$OUTPUT_DIR"
api /api/show "{\"model\":\"${DUAL_TAG}\"}" >/dev/null ||
  {
    log "${DUAL_TAG} missing; build it from llama/ollama/deepseek-coder-v2-instruct.Modelfile"
    exit 1
  }
trap cleanup EXIT
curl -fsS -m 10 "${OLLAMA_HOST}/api/version" >"${OUTPUT_DIR}/ollama-version.json"

log "baseline: ${FIM_TAG}, condition A, reps=${REPS}"
FIM_MODEL="$FIM_TAG" CONDITIONS=A PROBE_JSON="${OUTPUT_DIR}/baseline-fim.json" \
  python3 "$PROBE" "$REPS" | tee "${OUTPUT_DIR}/baseline-fim.log"

run_leg dual-use-instruct "$DUAL_TAG" "$DUAL_CONDITIONS"

for nb in $NUM_BATCH_VARIANTS; do
  tag="${DUAL_TAG}-nb${nb}"
  api /api/create "{\"model\":\"${tag}\",\"from\":\"${DUAL_TAG}\",\"parameters\":{\"num_batch\":${nb}},\"stream\":false}" >/dev/null
  TEMP_TAGS+=("$tag")
  run_leg "dual-use-instruct-nb${nb}" "$tag" "$SWEEP_CONDITIONS"
done

log "results in ${OUTPUT_DIR}"
