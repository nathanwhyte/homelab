#!/usr/bin/env bash
# qwen3.8:27b capability probe (PROJ-1003, TASK-1189).
#
# Runs BEFORE the throughput matrix. Answers the questions that decide how the
# matrix rows may be labelled, none of which are throughput questions:
#
#   1. What sampling parameters does Ollama actually expose for each tag?
#      Qwen documents min_p / presence_penalty / reasoning_effort, but support
#      is framework-dependent and a silently-ignored option is worse than an
#      absent one. Whatever the benchmark ran under must be recorded.
#   2. Does the vision path work on each tag? Per INFO-1127 and ollama#16700
#      the MLX runner is text-only, so qwen3.8:27b-mlx is EXPECTED to fail on
#      image input despite the tag advertising Text + Image. Confirming this is
#      the first acceptance criterion on TASK-1189 — if it holds, the two 18 GB
#      tags are comparable on decode but NOT on capability.
#   3. Does the `format` JSON-schema constraint hold? INFO-1127 root-causes the
#      MLX drop to x/mlxrunner/client.go never forwarding the field. Re-check
#      against this new family rather than assuming it inherits.
#
# Writes everything to a timestamped dir; prints a short summary to stdout.
set -euo pipefail

OUT_DIR="${OUT_DIR:-benchmarks/results/qwen38-capability-probe-$(date -u +%Y%m%dT%H%M%SZ)}"
URL="${URL:-http://localhost:11434}"
MLX_TAG="qwen3.8:27b-mlx"
GGUF_TAG="qwen3.8:27b-q4_K_M"

mkdir -p "$OUT_DIR"

log() { echo "[qwen38-probe] $*"; }

# A 1x1 red PNG. Enough to prove the image path is wired: a runner that
# supports vision returns a normal description-ish response, a text-only runner
# errors or silently ignores the images array.
TINY_PNG="iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="

probe_params() {
	local tag="$1" safe="$2"
	log "capturing parameters for ${tag}"
	{
		echo "=== ollama show ${tag} ==="
		ollama show "$tag" 2>&1 || true
		echo
		echo "=== ollama show ${tag} --parameters ==="
		ollama show "$tag" --parameters 2>&1 || true
		echo
		echo "=== ollama show ${tag} --modelfile ==="
		ollama show "$tag" --modelfile 2>&1 || true
	} >"${OUT_DIR}/${safe}-show.txt"
}

probe_vision() {
	local tag="$1" safe="$2"
	log "probing vision path for ${tag}"
	curl -sS "${URL}/api/chat" -d "{
    \"model\": \"${tag}\",
    \"stream\": false,
    \"messages\": [{
      \"role\": \"user\",
      \"content\": \"What color is this image? Answer in one word.\",
      \"images\": [\"${TINY_PNG}\"]
    }],
    \"options\": {\"temperature\": 0.7, \"top_p\": 0.8, \"top_k\": 20, \"num_predict\": 64}
  }" >"${OUT_DIR}/${safe}-vision.json" 2>&1 || true
}

probe_format() {
	local tag="$1" safe="$2"
	log "probing format JSON-schema constraint for ${tag}"
	curl -sS "${URL}/api/chat" -d "{
    \"model\": \"${tag}\",
    \"stream\": false,
    \"format\": {
      \"type\": \"object\",
      \"properties\": {\"color\": {\"type\": \"string\"}, \"confidence\": {\"type\": \"number\"}},
      \"required\": [\"color\", \"confidence\"]
    },
    \"messages\": [{\"role\": \"user\", \"content\": \"Name a primary color and your confidence 0-1.\"}],
    \"options\": {\"temperature\": 0.7, \"top_p\": 0.8, \"top_k\": 20, \"num_predict\": 256}
  }" >"${OUT_DIR}/${safe}-format.json" 2>&1 || true
}

probe_thinking() {
	local tag="$1" safe="$2"
	log "probing thinking default for ${tag}"
	curl -sS "${URL}/api/chat" -d "{
    \"model\": \"${tag}\",
    \"stream\": false,
    \"messages\": [{\"role\": \"user\", \"content\": \"What is 17 * 23? Reply with just the number.\"}],
    \"options\": {\"temperature\": 1.0, \"top_p\": 0.95, \"top_k\": 20, \"num_predict\": 2048}
  }" >"${OUT_DIR}/${safe}-thinking.json" 2>&1 || true
}

for pair in "${MLX_TAG}|mlx" "${GGUF_TAG}|gguf"; do
	tag="${pair%%|*}"
	safe="${pair##*|}"

	if ! ollama show "$tag" >/dev/null 2>&1; then
		log "SKIP ${tag} — not present locally"
		continue
	fi

	probe_params "$tag" "$safe"
	probe_vision "$tag" "$safe"
	probe_format "$tag" "$safe"
	probe_thinking "$tag" "$safe"

	ollama stop "$tag" >/dev/null 2>&1 || true
	log "unloaded ${tag}"
done

log "probe complete; artifacts in ${OUT_DIR}"
echo "$OUT_DIR"
