#!/usr/bin/env bash
# Local image generation and understanding CLI for MacBook M5 Max.
#
# Manages two services:
#   - Image generation via FLUX.2 Klein (Ollama)
#   - Image understanding via Qwen3.6-27B+mmproj (llama-server)
#
# Both services can be started on demand and stopped after use.
# Ollama is started/stopped via brew services (macOS). llama-server
# is started/stopped as a background process with PID tracking.
#
# Usage:
#   img-pipeline.sh generate "prompt"            Generate an image via FLUX.2 Klein
#   img-pipeline.sh generate --file prompt.txt  Generate from a prompt file
#   img-pipeline.sh generate --manage-ollama    Auto start/stop Ollama for this request
#   img-pipeline.sh understand <image_path>     Describe an image via Qwen3.6+mmproj
#   img-pipeline.sh up                           Start llama-server; ensure FLUX model
#   img-pipeline.sh down                         Stop llama-server
#   img-pipeline.sh ollama-up                    Start Ollama via brew services
#   img-pipeline.sh ollama-down                  Stop Ollama via brew services
#   img-pipeline.sh status                       Show both services' state
#   img-pipeline.sh run -- <command...>          up → command → down (EXIT trap)
#
# Options (understand):
#   --prompt "question"   Custom prompt (auto-appends /no_think unless --raw)
#   --raw                 Include thinking tokens (don't append /no_think)
#   --server <url>        Override llama-server URL
#
# Options (generate):
#   --model <model>        Override FLUX model name
#   --output <path>        Override output file path
#   --file <path>          Read prompt from a file (useful for long/complex prompts)
#   --manage-ollama        Auto start Ollama if not running, stop it after generation
#
# Environment overrides (see img-pipeline.conf):
#   LLAMACPP_PORT, LLAMACPP_HOST, LLAMACPP_CTX, LLAMACPP_NGL,
#   LLAMACPP_MODEL, LLAMACPP_MMPROJ, FLUX_MODEL,
#   UP_TIMEOUT, DOWN_TIMEOUT, OUTPUT_DIR, PID_FILE, OLLAMA_HOST
#
# Examples:
#   img-pipeline.sh generate "a sunset over mountains"
#   img-pipeline.sh generate --manage-ollama "a sunset over mountains"
#   img-pipeline.sh generate --file my-prompt.txt
#   img-pipeline.sh understand ~/Pictures/ai-generated/20260606-143000-flux.png
#   img-pipeline.sh understand image.png --prompt "How many cats are in this image?"
#   img-pipeline.sh run -- img-pipeline.sh understand image.png
set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONF="${SCRIPT_DIR}/img-pipeline.conf"

if [[ -f "$CONF" ]]; then
  # shellcheck source=img-pipeline.conf
  source "$CONF"
else
  echo "ERROR: config not found: $CONF" >&2
  exit 1
fi

# ── Helpers ────────────────────────────────────────────────────────────────
log() { printf '%s %s\n' "$(date +%H:%M:%S)" "$*" >&2; }

usage() {
  sed -n '2,33p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

llamaserver_url() { echo "http://${LLAMACPP_HOST}:${LLAMACPP_PORT}"; }

# ── Ollama lifecycle ───────────────────────────────────────────────────────
# Track whether we started Ollama so we can stop it on exit.
_OLLAMA_STARTED_BY_US=false

do_ollama_up() {
  # Check if Ollama is already responding
  if curl -sf "${OLLAMA_HOST}/api/tags" >/dev/null 2>&1; then
    log "Ollama already running at ${OLLAMA_HOST}"
    return 0
  fi

  log "starting Ollama via brew services..."
  brew services start ollama >/dev/null 2>&1 || {
    # Fallback: try direct start (for non-brew installs)
    log "brew services start failed, trying ollama serve..."
    nohup ollama serve >/tmp/img-pipeline-ollama.log 2>&1 &
  }

  # Wait for Ollama to become ready
  local elapsed=0
  log "waiting up to ${UP_TIMEOUT}s for Ollama readiness..."
  while (( elapsed < UP_TIMEOUT )); do
    if curl -sf "${OLLAMA_HOST}/api/tags" >/dev/null 2>&1; then
      log "Ollama is ready"
      _OLLAMA_STARTED_BY_US=true
      return 0
    fi
    sleep 2
    elapsed=$((elapsed + 2))
  done

  log "ERROR: Ollama did not become ready within ${UP_TIMEOUT}s"
  log "check: brew services info ollama, or /tmp/img-pipeline-ollama.log"
  return 1
}

do_ollama_down() {
  if [[ "${_OLLAMA_STARTED_BY_US}" == "true" ]]; then
    log "stopping Ollama (we started it)..."
    brew services stop ollama >/dev/null 2>&1 || true
    _OLLAMA_STARTED_BY_US=false
  else
    log "Ollama was already running — leaving it up"
  fi
}

# ── llama-server lifecycle ────────────────────────────────────────────────
do_up() {
  # Check if llama-server is already running
  if curl -sf "$(llamaserver_url)/health" >/dev/null 2>&1; then
    log "llama-server already running on port ${LLAMACPP_PORT}"
  else
    log "starting llama-server on port ${LLAMACPP_PORT}"
    llama-server \
      -m "${LLAMACPP_MODEL}" \
      --mmproj "${LLAMACPP_MMPROJ}" \
      -c "${LLAMACPP_CTX}" \
      -ngl "${LLAMACPP_NGL}" \
      --port "${LLAMACPP_PORT}" \
      --host "${LLAMACPP_HOST}" \
      >/tmp/img-pipeline-llamaserver.log 2>&1 &
    echo $! > "${PID_FILE}"
    log "llama-server PID $(cat "${PID_FILE}")"

    # Wait for readiness
    log "waiting up to ${UP_TIMEOUT}s for llama-server readiness..."
    elapsed=0
    while (( elapsed < UP_TIMEOUT )); do
      if curl -sf "$(llamaserver_url)/health" >/dev/null 2>&1; then
        log "llama-server is ready"
        break
      fi
      sleep 2
      elapsed=$((elapsed + 2))
    done

    if ! curl -sf "$(llamaserver_url)/health" >/dev/null 2>&1; then
      log "ERROR: llama-server did not become ready within ${UP_TIMEOUT}s"
      log "check logs: /tmp/img-pipeline-llamaserver.log"
      exit 1
    fi
  fi

  # Ensure FLUX model is available in Ollama
  if ! ollama list 2>/dev/null | grep -q "${FLUX_MODEL}"; then
    log "pulling ${FLUX_MODEL} model (first time)..."
    ollama pull "${FLUX_MODEL}"
  else
    log "FLUX model ${FLUX_MODEL} available in Ollama"
  fi
}

do_down() {
  if [[ -f "${PID_FILE}" ]]; then
    local pid
    pid=$(cat "${PID_FILE}")
    if kill -0 "$pid" 2>/dev/null; then
      log "stopping llama-server (PID $pid)"
      kill "$pid" 2>/dev/null || true
    fi
    rm -f "${PID_FILE}"
  fi

  # Fallback: kill any llama-server on our port
  local pids
  pids=$(lsof -ti:"${LLAMACPP_PORT}" 2>/dev/null || true)
  if [[ -n "$pids" ]]; then
    log "killing residual llama-server processes on port ${LLAMACPP_PORT}"
    echo "$pids" | xargs kill 2>/dev/null || true
  fi

  # Wait for port to close
  local elapsed=0
  while (( elapsed < DOWN_TIMEOUT )); do
    if ! curl -sf "$(llamaserver_url)/health" >/dev/null 2>&1; then
      log "llama-server stopped"
      return 0
    fi
    sleep 1
    elapsed=$((elapsed + 1))
  done
  log "WARN: llama-server did not stop within ${DOWN_TIMEOUT}s"
}

do_status() {
  echo "=== img-pipeline status ==="

  # llama-server
  if curl -sf "$(llamaserver_url)/health" >/dev/null 2>&1; then
    local pid_info=""
    if [[ -f "${PID_FILE}" ]]; then
      pid_info=" (PID $(cat "${PID_FILE}"))"
    fi
    echo "llama-server: RUNNING on port ${LLAMACPP_HOST}:${LLAMACPP_PORT}${pid_info}"
    echo "  model:   ${LLAMACPP_MODEL##*/}"  # basename
    echo "  mmproj:  ${LLAMACPP_MMPROJ##*/}"
  else
    echo "llama-server: STOPPED"
  fi

  # Ollama
  if curl -sf "${OLLAMA_HOST}/api/tags" >/dev/null 2>&1; then
    echo "Ollama:       RUNNING at ${OLLAMA_HOST}"
  else
    echo "Ollama:       STOPPED (run 'img-pipeline.sh ollama-up' or 'brew services start ollama')"
  fi

  # Ollama FLUX model (only meaningful if Ollama is running)
  if curl -sf "${OLLAMA_HOST}/api/tags" >/dev/null 2>&1; then
    if ollama list 2>/dev/null | grep -q "${FLUX_MODEL}"; then
      echo "FLUX model:  ${FLUX_MODEL} (available in Ollama)"
    else
      echo "FLUX model:  ${FLUX_MODEL} (NOT PULLED — run 'ollama pull ${FLUX_MODEL}')"
    fi
  else
    echo "FLUX model:  ${FLUX_MODEL} (Ollama not running — cannot check)"
  fi

  echo "  Output:    ${OUTPUT_DIR}"
}

# ── Image generation ──────────────────────────────────────────────────────
do_generate() {
  local prompt="" model="${FLUX_MODEL}" output_path="" prompt_file="" manage_ollama=false

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --model)          model="$2"; shift 2 ;;
      --output)         output_path="$2"; shift 2 ;;
      --file)           prompt_file="$2"; shift 2 ;;
      --manage-ollama)  manage_ollama=true; shift ;;
      -h|--help)        usage 0 ;;
      *)                prompt="$1"; shift ;;
    esac
  done

  # Read prompt from file if --file was given
  if [[ -n "$prompt_file" ]]; then
    if [[ ! -f "$prompt_file" ]]; then
      log "ERROR: prompt file not found: ${prompt_file}"
      exit 1
    fi
    prompt=$(cat "$prompt_file")
  fi

  if [[ -z "$prompt" ]]; then
    log "ERROR: generate requires a prompt string or --file"
    usage 1
  fi

  # Start Ollama if --manage-ollama was given and it's not running
  if [[ "$manage_ollama" == "true" ]]; then
    do_ollama_up || exit 1
    # Set EXIT trap to stop Ollama when we're done
    trap do_ollama_down EXIT
  fi

  mkdir -p "${OUTPUT_DIR}"

  local timestamp
  timestamp=$(date +%Y%m%d-%H%M%S)
  if [[ -z "$output_path" ]]; then
    output_path="${OUTPUT_DIR}/${timestamp}-flux.png"
  fi

  log "generating image with model ${model} (prompt: $(echo "$prompt" | head -c 80)...)"

  # Build JSON payload with jq to properly escape the prompt (handles quotes,
  # newlines, backslashes, and special characters in long prompts)
  local json_payload
  json_payload=$(jq -n \
    --arg model "$model" \
    --arg prompt "$prompt" \
    '{model: $model, prompt: $prompt, stream: false}')

  # Call Ollama with stream:false to get a single JSON response
  local tmpfile
  tmpfile=$(mktemp /tmp/img-pipeline-generate.XXXXXX.json)

  local rc=0
  curl -sf "${OLLAMA_HOST}/api/generate" \
    -H "Content-Type: application/json" \
    -d "$json_payload" \
    -o "$tmpfile" || rc=$?

  if [[ $rc -ne 0 ]]; then
    log "ERROR: Ollama request failed (curl exit $rc)"
    rm -f "$tmpfile"
    exit 1
  fi

  # Check for error in response
  if jq -e '.error' "$tmpfile" >/dev/null 2>&1; then
    local err
    err=$(jq -r '.error' "$tmpfile")
    log "ERROR: Ollama returned error: ${err}"
    rm -f "$tmpfile"
    exit 1
  fi

  # Extract base64 image data and decode
  if ! jq -e '.image' "$tmpfile" >/dev/null 2>&1; then
    log "ERROR: no image field in Ollama response"
    log "  response saved to: ${tmpfile}"
    exit 1
  fi

  jq -r '.image' "$tmpfile" | base64 -d > "$output_path"
  rm -f "$tmpfile"

  if [[ -s "$output_path" ]]; then
    echo "$output_path"
  else
    log "ERROR: generated file is empty"
    rm -f "$output_path"
    exit 1
  fi
}

# ── Image understanding ───────────────────────────────────────────────────
do_understand() {
  local image_path="" prompt="Describe this image in detail." raw=false server=""

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --prompt)  prompt="$2"; shift 2 ;;
      --raw)    raw=true; shift ;;
      --server)  server="$2"; shift 2 ;;
      -h|--help) usage 0 ;;
      *)         image_path="$1"; shift ;;
    esac
  done

  if [[ -z "$image_path" ]]; then
    log "ERROR: understand requires an image path"
    usage 1
  fi

  if [[ ! -f "$image_path" ]]; then
    log "ERROR: image not found: ${image_path}"
    exit 1
  fi

  # Append /no_think unless --raw
  if [[ "$raw" == "false" ]]; then
    prompt="${prompt}/no_think"
  fi

  local api_url
  if [[ -n "$server" ]]; then
    api_url="$server"
  else
    api_url="$(llamaserver_url)"
  fi

  log "understanding image: ${image_path}"
  log "  prompt: ${prompt}"
  log "  server: ${api_url}"

  # Base64-encode the image
  local b64
  b64=$(base64 -i "$image_path")

  # Determine content type from extension
  local content_type="image/png"
  case "${image_path##*.}" in
    jpg|jpeg) content_type="image/jpeg" ;;
    gif)      content_type="image/gif" ;;
    webp)     content_type="image/webp" ;;
  esac

  # Build the OpenAI vision request
  local json_payload
  json_payload=$(jq -n \
    --arg prompt "$prompt" \
    --arg b64 "$b64" \
    --arg ct "$content_type" \
    '{
      model: "qwen3.6-27b-uncensored-heretic-v2",
      messages: [
        {
          role: "user",
          content: [
            {type: "text", text: $prompt},
            {type: "image_url", image_url: {url: ("data:" + $ct + ";base64," + $b64)}}
          ]
        }
      ],
      max_tokens: 2048
    }')

  # Call llama-server
  local tmpfile
  tmpfile=$(mktemp /tmp/img-pipeline-understand.XXXXXX.json)

  local rc=0
  curl -sf "${api_url}/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -d "$json_payload" \
    -o "$tmpfile" || rc=$?

  if [[ $rc -ne 0 ]]; then
    log "ERROR: llama-server request failed (curl exit $rc)"
    rm -f "$tmpfile"
    exit 1
  fi

  # Extract and print the response
  jq -r '.choices[0].message.content' "$tmpfile"
  rm -f "$tmpfile"
}

# ── Main dispatch ─────────────────────────────────────────────────────────
main() {
  [[ $# -ge 1 ]] || usage 1

  case "$1" in
    up)
      do_up
      ;;
    down)
      do_down
      ;;
    ollama-up)
      do_ollama_up
      ;;
    ollama-down)
      do_ollama_down
      ;;
    status)
      do_status
      ;;
    generate)
      shift
      do_generate "$@"
      ;;
    understand)
      shift
      do_understand "$@"
      ;;
    run)
      shift
      [[ "${1:-}" = "--" ]] && shift
      [[ $# -ge 1 ]] || { log "run: no command given"; usage 1; }
      # Always clean up llama-server on any exit path (including Ctrl-C).
      trap do_down EXIT
      do_up
      rc=0
      "$@" || rc=$?
      exit "$rc"
      ;;
    -h|--help|help)
      usage 0
      ;;
    *)
      log "unknown command: $1"
      usage 1
      ;;
  esac
}

main "$@"