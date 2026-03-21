#!/usr/bin/env bash
set -euo pipefail

# Model Showdown: Claude vs Mistral-Small-24B
# Sends identical coding/debugging prompts to both models and compares responses.
# Uses `claude --print` CLI and the local llama.cpp OpenAI-compatible API.
#
# Requirements:
#   - claude CLI installed (Claude Code)
#   - llamacpp-rocm deployment running with Traefik ingress
#
# Usage:
#   bash model-showdown.sh
#   bash model-showdown.sh --local-url http://llama.local --api-key <token>

NAMESPACE="llama"
LOCAL_MODEL_URL=""
API_KEY=""

# Parse args
while [[ $# -gt 0 ]]; do
    case "$1" in
        --local-url) LOCAL_MODEL_URL="$2"; shift 2 ;;
        --api-key) API_KEY="$2"; shift 2 ;;
        --help|-h)
            echo "Usage: model-showdown.sh [--local-url <url>] [--api-key <token>]"
            echo ""
            echo "Compares Claude (via CLI) and a local model on coding/debugging tasks."
            echo "Defaults to http://llama.local. Auto-fetches API key from K8s secret."
            exit 0
            ;;
        *) shift ;;
    esac
done

# Check for claude CLI
if ! command -v claude &>/dev/null; then
    echo "Error: claude CLI not found. Install Claude Code first." >&2
    exit 1
fi

# Default to ingress URL
if [[ -z "$LOCAL_MODEL_URL" ]]; then
    LOCAL_MODEL_URL="http://llama.local"
fi

# Auto-fetch API key from K8s secret if not provided
if [[ -z "$API_KEY" ]]; then
    API_KEY=$(kubectl get secret llama-rocm-api-key -n "$NAMESPACE" -o jsonpath='{.data.API_KEY}' 2>/dev/null | base64 -d 2>/dev/null || echo "")
    if [[ -n "$API_KEY" ]]; then
        echo "Loaded API key from K8s secret."
    fi
fi

CURL_AUTH=()
if [[ -n "$API_KEY" ]]; then
    CURL_AUTH=(-H "Authorization: Bearer $API_KEY")
fi

# Verify local model is reachable
if ! curl -sf "${CURL_AUTH[@]}" "$LOCAL_MODEL_URL/health" >/dev/null 2>&1; then
    echo "Error: local model not reachable at $LOCAL_MODEL_URL/health" >&2
    echo "Is llamacpp-rocm running? Check: kubectl get pods -n $NAMESPACE -l app=llamacpp-rocm" >&2
    echo "Is llama.local in /etc/hosts? Try: echo '192.168.1.10 llama.local' | sudo tee -a /etc/hosts" >&2
    exit 1
fi

# Detect which model is loaded
LOCAL_MODEL_NAME=$(curl -sf "${CURL_AUTH[@]}" "$LOCAL_MODEL_URL/v1/models" | python3 -c "
import sys, json
try:
    data = json.loads(sys.stdin.read())
    model_id = data['data'][0]['id']
    # Extract a friendly name from the path
    name = model_id.rsplit('/', 1)[-1].replace('.gguf', '')
    print(name)
except: print('unknown-model')
" 2>/dev/null || echo "unknown-model")

DIVIDER="$(printf '%.0s─' {1..72})"
THIN_DIV="$(printf '%.0s┄' {1..72})"

call_local() {
    local prompt="$1"
    local max_tokens="${2:-1024}"
    curl -sf "${CURL_AUTH[@]}" "$LOCAL_MODEL_URL/v1/chat/completions" \
        -H "Content-Type: application/json" \
        -d "$(python3 -c "
import json, sys
print(json.dumps({
    'messages': [{'role': 'user', 'content': sys.argv[1]}],
    'max_tokens': int(sys.argv[2]),
    'temperature': 0.3
}))
" "$prompt" "$max_tokens")" | python3 -c "
import sys, json
data = json.loads(sys.stdin.read())
print(data['choices'][0]['message']['content'])
" 2>/dev/null
}

call_claude() {
    local prompt="$1"
    claude --print --model haiku "$prompt" 2>/dev/null
}

print_result() {
    local label="$1"
    local text="$2"
    local elapsed="$3"
    echo ""
    echo "  [$label] (${elapsed}s)"
    echo "$THIN_DIV"
    echo "$text" | fold -s -w 72 | sed 's/^/  /'
}

run_round() {
    local round_num="$1"
    local difficulty="$2"
    local prompt="$3"
    local max_tokens="${4:-1024}"

    echo ""
    echo "$DIVIDER"
    echo "  ROUND $round_num [$difficulty]"
    echo "$DIVIDER"
    echo ""
    echo "  Prompt:"
    echo "$prompt" | fold -s -w 68 | sed 's/^/    /'

    # Call both models
    local start_local start_claude end_local end_claude
    local result_local result_claude

    start_local=$(date +%s)
    result_local=$(call_local "$prompt" "$max_tokens" 2>&1) || result_local="[ERROR: local model failed]"
    end_local=$(date +%s)

    start_claude=$(date +%s)
    result_claude=$(call_claude "$prompt" "$max_tokens" 2>&1) || result_claude="[ERROR: Claude API failed]"
    end_claude=$(date +%s)

    print_result "$LOCAL_MODEL_NAME (local)" "$result_local" "$((end_local - start_local))"
    echo ""
    print_result "Claude (haiku via CLI)" "$result_claude" "$((end_claude - start_claude))"
    echo ""
}

echo ""
echo "  ╔══════════════════════════════════════════════════════════════╗"
echo "  ║          MODEL SHOWDOWN: Claude vs Local Model             ║"
echo "  ║         Coding Knowledge / Debug Skills / Reasoning         ║"
echo "  ╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "  Claude:        haiku (via claude CLI)"
echo "  Local model:   $LOCAL_MODEL_NAME (ROCm)"
echo "  Local URL:     $LOCAL_MODEL_URL"

# ── Round 1: Simple ──────────────────────────────────────────────────

run_round 1 "EASY" \
"What is the difference between a mutex and a semaphore? Give a short, concrete example of when you'd use each." \
512

# ── Round 2: Moderate ────────────────────────────────────────────────

run_round 2 "MODERATE" \
"Read this Go code and find the bug:

func process(items []string) []string {
    var result []string
    for _, item := range items {
        go func() {
            result = append(result, strings.ToUpper(item))
        }()
    }
    return result
}" \
512

# ── Round 3: Moderate ────────────────────────────────────────────────

run_round 3 "MODERATE" \
"A Kubernetes pod keeps getting OOMKilled despite the container only using 200Mi according to 'kubectl top pod'. The pod's memory limit is 512Mi. What are the most likely causes? Walk through your debugging steps." \
768

# ── Round 4: Hard ────────────────────────────────────────────────────

run_round 4 "HARD" \
"Explain this bash one-liner — what does it do, and what's the subtle bug?

find . -name '*.log' -mtime +7 | xargs rm -f

Then rewrite it to be safe for filenames with spaces, newlines, and special characters." \
512

# ── Round 5: Hard ────────────────────────────────────────────────────

run_round 5 "HARD" \
"I have a Python FastAPI app that works fine with 1 worker but returns stale data with multiple uvicorn workers. The app uses a module-level dict as a cache that gets populated on first request. Explain exactly why this breaks with multiple workers, and propose two different solutions — one simple, one robust." \
768

# ── Round 6: Expert ──────────────────────────────────────────────────

run_round 6 "EXPERT" \
"You're debugging a production issue: a Rust service using tokio is consuming 100% CPU but its request throughput has dropped to near zero. Logs show no errors. Metrics show the task count is very high but tasks_completed_per_sec is near zero. What is the most likely cause? How would you confirm it? How would you fix it without a full rewrite?" \
768

# ── Round 7: Expert ──────────────────────────────────────────────────

run_round 7 "EXPERT" \
"Write a PostgreSQL query that finds all customers who made purchases in every month of 2024 (Jan through Dec), but ONLY if their average order value increased month-over-month for at least 6 consecutive months during that period. Return customer_id, their longest streak of increasing monthly averages, and their total spend. Tables: orders(id, customer_id, amount, created_at)." \
1024

echo ""
echo "$DIVIDER"
echo "  SHOWDOWN COMPLETE"
echo "$DIVIDER"
echo ""
