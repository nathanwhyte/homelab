#!/usr/bin/env bash
set -euo pipefail

NS="${HERMES_NAMESPACE:-hermes}"
SERVICE="${HERMES_SERVICE:-hermes-agent}"
DEPLOY="${HERMES_DEPLOYMENT:-hermes-agent}"
LOCAL_PORT="${HERMES_LOCAL_PORT:-8642}"
REMOTE_PORT="${HERMES_REMOTE_PORT:-8642}"
BASE_URL="${HERMES_BASE_URL:-http://127.0.0.1:${LOCAL_PORT}}"
PF_LOG="${HERMES_PORT_FORWARD_LOG:-/tmp/hermes-port-forward.log}"
MODEL="${HERMES_MODEL:-glm-5.1:cloud}"

usage() {
  cat <<'EOF'
Usage: hermes/operator.sh <command> [args]

Cluster-internal operator helper for the Hermes Agent deployment.
No external exposure is created; API commands use kubectl port-forward.

Commands:
  port-forward              Run kubectl port-forward in the foreground
  health                    GET /health through a temporary port-forward
  models                    GET /v1/models through a temporary port-forward
  ask <prompt>              POST /v1/chat/completions and print assistant text
  ask-file <path>           POST /v1/chat/completions with prompt read from file
  run <prompt>              POST /v1/runs and stream lifecycle SSE events
  run-file <path>           POST /v1/runs with prompt read from file
  logs [args...]            kubectl logs deploy/hermes-agent (default: --tail=120)
  sessions [args...]        kubectl exec hermes sessions ... (default: list)
  status                    kubectl exec hermes status
  config                    kubectl exec hermes config show
  jump-check                Ask Hermes to run a terminal command and report where it lands
  rollout-status            kubectl rollout status deployment/hermes-agent
  restart                   kubectl rollout restart deployment/hermes-agent

Environment:
  HERMES_NAMESPACE          default: hermes
  HERMES_SERVICE            default: hermes-agent
  HERMES_DEPLOYMENT         default: hermes-agent
  HERMES_LOCAL_PORT         default: 8642
  HERMES_REMOTE_PORT        default: 8642
  HERMES_API_KEY            optional; otherwise read from Secret hermes-api-server-key
  HERMES_MODEL              model for ask/run commands (default: glm-5.1:cloud)
EOF
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "missing required command: $1" >&2
    exit 127
  }
}

api_key() {
  if [[ -n "${HERMES_API_KEY:-}" ]]; then
    printf '%s' "$HERMES_API_KEY"
    return
  fi
  kubectl get secret hermes-api-server-key -n "$NS" \
    -o jsonpath='{.data.api-key}' | base64 --decode
}

start_temp_port_forward() {
  if curl -fsS "${BASE_URL}/health" >/dev/null 2>&1; then
    echo ""
    return
  fi

  : >"$PF_LOG"
  kubectl -n "$NS" port-forward "svc/${SERVICE}" "${LOCAL_PORT}:${REMOTE_PORT}" >"$PF_LOG" 2>&1 &
  local pid=$!

  for _ in $(seq 1 40); do
    if curl -fsS "${BASE_URL}/health" >/dev/null 2>&1; then
      echo "$pid"
      return
    fi
    if ! kill -0 "$pid" >/dev/null 2>&1; then
      echo "port-forward exited early; log follows:" >&2
      cat "$PF_LOG" >&2
      exit 1
    fi
    sleep 0.25
  done

  echo "timed out waiting for port-forward; log follows:" >&2
  cat "$PF_LOG" >&2
  kill "$pid" >/dev/null 2>&1 || true
  exit 1
}

with_port_forward() {
  local pf_pid
  pf_pid="$(start_temp_port_forward)"
  if [[ -n "$pf_pid" ]]; then
    trap "kill '$pf_pid' >/dev/null 2>&1 || true" EXIT
  fi
}

json_escape() {
  python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'
}

cmd="${1:-}"
if [[ -z "$cmd" || "$cmd" == "-h" || "$cmd" == "--help" ]]; then
  usage
  exit 0
fi
shift || true

require_cmd kubectl
require_cmd curl

case "$cmd" in
  port-forward)
    exec kubectl -n "$NS" port-forward "svc/${SERVICE}" "${LOCAL_PORT}:${REMOTE_PORT}"
    ;;
  health)
    with_port_forward
    curl -fsS "${BASE_URL}/health"
    echo
    ;;
  models)
    with_port_forward
    key="$(api_key)"
    curl -fsS -H "Authorization: Bearer ${key}" "${BASE_URL}/v1/models"
    echo
    ;;
  ask|ask-file)
    if [[ "$cmd" == "ask-file" ]]; then
      [[ $# -eq 1 ]] || { echo "ask-file requires exactly one path" >&2; exit 2; }
      [[ -f "$1" ]] || { echo "prompt file not found: $1" >&2; exit 2; }
      prompt="$(json_escape <"$1")"
    else
      [[ $# -gt 0 ]] || { echo "ask requires a prompt" >&2; exit 2; }
      prompt="$(printf '%s' "$*" | json_escape)"
    fi
    with_port_forward
    key="$(api_key)"
    curl -fsS -X POST "${BASE_URL}/v1/chat/completions" \
      -H "Authorization: Bearer ${key}" \
      -H "Content-Type: application/json" \
      -d "{\"model\":\"${MODEL}\",\"messages\":[{\"role\":\"user\",\"content\":${prompt}}],\"stream\":false}" \
      | python3 -c 'import json,sys; print(json.load(sys.stdin)["choices"][0]["message"]["content"])'
    ;;
  run|run-file)
    if [[ "$cmd" == "run-file" ]]; then
      [[ $# -eq 1 ]] || { echo "run-file requires exactly one path" >&2; exit 2; }
      [[ -f "$1" ]] || { echo "prompt file not found: $1" >&2; exit 2; }
      prompt="$(json_escape <"$1")"
    else
      [[ $# -gt 0 ]] || { echo "run requires a prompt" >&2; exit 2; }
      prompt="$(printf '%s' "$*" | json_escape)"
    fi
    with_port_forward
    key="$(api_key)"
    response="$(curl -fsS -X POST "${BASE_URL}/v1/runs" \
      -H "Authorization: Bearer ${key}" \
      -H "Content-Type: application/json" \
      -d "{\"model\":\"${MODEL}\",\"input\":${prompt}}")"
    echo "$response"
    run_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["run_id"])' <<<"$response")"
    curl -fsS -N -H "Authorization: Bearer ${key}" "${BASE_URL}/v1/runs/${run_id}/events"
    ;;
  logs)
    if [[ $# -eq 0 ]]; then
      set -- --tail=120
    fi
    exec kubectl logs -n "$NS" "deploy/${DEPLOY}" "$@"
    ;;
  sessions)
    if [[ $# -eq 0 ]]; then
      set -- list
    fi
    exec kubectl exec -n "$NS" "deploy/${DEPLOY}" -- hermes sessions "$@"
    ;;
  status)
    exec kubectl exec -n "$NS" "deploy/${DEPLOY}" -- hermes status
    ;;
  config)
    exec kubectl exec -n "$NS" "deploy/${DEPLOY}" -- hermes config show
    ;;
  jump-check)
    exec "$0" run "Use your terminal tool to run: hostname && pwd. Then answer with the raw command output only."
    ;;
  rollout-status)
    exec kubectl rollout status -n "$NS" "deployment/${DEPLOY}"
    ;;
  restart)
    kubectl rollout restart -n "$NS" "deployment/${DEPLOY}"
    exec kubectl rollout status -n "$NS" "deployment/${DEPLOY}"
    ;;
  *)
    echo "unknown command: $cmd" >&2
    usage >&2
    exit 2
    ;;
esac
