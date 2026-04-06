#!/usr/bin/env bash
set -euo pipefail

# Model Benchmark: Run coding/debugging prompts against a local llama.cpp model.
#
# Usage:
#   bash model-bench.sh
#   bash model-bench.sh --local-url http://llama.local --api-key <token>
#   bash model-bench.sh --max-tokens 8192

NAMESPACE="llama"
LOCAL_MODEL_URL=""
API_KEY=""
MAX_TOKENS=8192

# Parse args
while [[ $# -gt 0 ]]; do
    case "$1" in
        --local-url) LOCAL_MODEL_URL="$2"; shift 2 ;;
        --api-key) API_KEY="$2"; shift 2 ;;
        --max-tokens) MAX_TOKENS="$2"; shift 2 ;;
        --help|-h)
            echo "Usage: model-bench.sh [--local-url <url>] [--api-key <token>] [--max-tokens <n>]"
            echo ""
            echo "Benchmarks a local llama.cpp model on 7 coding/debugging tasks."
            echo "Default max_tokens: 8192. Increase for models with thinking mode."
            echo "If --local-url is not given, uses http://llama.local (Traefik ingress)."
            echo "If --api-key is not given, reads from llama-rocm-api-key K8s secret."
            exit 0
            ;;
        *) shift ;;
    esac
done

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

AUTH_HEADER=""
if [[ -n "$API_KEY" ]]; then
    AUTH_HEADER="Authorization: Bearer $API_KEY"
fi

# Verify model is reachable
CURL_AUTH=()
if [[ -n "$AUTH_HEADER" ]]; then
    CURL_AUTH=(-H "$AUTH_HEADER")
fi

if ! curl -sf "${CURL_AUTH[@]}" "$LOCAL_MODEL_URL/health" >/dev/null 2>&1; then
    echo "Error: model not reachable at $LOCAL_MODEL_URL/health" >&2
    echo "Is llamacpp-rocm running? Check: kubectl get pods -n $NAMESPACE -l app=llamacpp-rocm" >&2
    echo "Is llama.local in /etc/hosts? Try: echo '192.168.1.10 llama.local' | sudo tee -a /etc/hosts" >&2
    exit 1
fi

# Detect which model is loaded
MODEL_NAME=$(curl -sf "${CURL_AUTH[@]}" "$LOCAL_MODEL_URL/v1/models" | python3 -c "
import sys, json
try:
    data = json.loads(sys.stdin.read())
    model_id = data['data'][0]['id']
    name = model_id.rsplit('/', 1)[-1].replace('.gguf', '')
    print(name)
except: print('unknown-model')
" 2>/dev/null || echo "unknown-model")

SYSTEM_PROMPT="You are an expert software engineer. Think step by step. Identify the most likely root cause first, then explain why. Be precise and specific — avoid generic advice. Include concrete commands, code, or examples. When debugging, focus on the exact symptoms described."

DIVIDER="$(printf '%.0s─' {1..72})"
THIN_DIV="$(printf '%.0s┄' {1..72})"

call_model() {
    local prompt="$1"
    curl -sf "${CURL_AUTH[@]}" "$LOCAL_MODEL_URL/v1/chat/completions" \
        -H "Content-Type: application/json" \
        -d "$(python3 -c "
import json, sys
print(json.dumps({
    'messages': [
        {'role': 'system', 'content': sys.argv[3]},
        {'role': 'user', 'content': sys.argv[1]}
    ],
    'max_tokens': int(sys.argv[2]),
    'temperature': 0.5,
    'min_p': 0.05,
    'repetition_penalty': 1.05
}))
" "$prompt" "$MAX_TOKENS" "$SYSTEM_PROMPT")" | python3 -c "
import sys, json
data = json.loads(sys.stdin.read())
choice = data['choices'][0]
content = choice['message']['content']
usage = data.get('usage', {})
prompt_tokens = usage.get('prompt_tokens', '?')
completion_tokens = usage.get('completion_tokens', '?')
finish = choice.get('finish_reason', '?')
print(f'__TOKENS__:{prompt_tokens}:{completion_tokens}:{finish}')
print(content)
" 2>/dev/null
}

run_round() {
    local round_num="$1"
    local difficulty="$2"
    local prompt="$3"

    echo ""
    echo "$DIVIDER"
    echo "  ROUND $round_num [$difficulty]"
    echo "$DIVIDER"
    echo ""
    echo "  Prompt:"
    echo "$prompt" | fold -s -w 68 | sed 's/^/    /'

    local start_time end_time elapsed
    local raw_result result token_info
    local prompt_tokens completion_tokens finish_reason

    start_time=$(date +%s)
    raw_result=$(call_model "$prompt" 2>&1) || raw_result="__TOKENS__:?:?:error
[ERROR: model failed to respond]"
    end_time=$(date +%s)
    elapsed=$((end_time - start_time))

    # Parse token info from first line
    token_info=$(echo "$raw_result" | head -1)
    result=$(echo "$raw_result" | tail -n +2)

    if [[ "$token_info" == __TOKENS__:* ]]; then
        IFS=: read -r _ prompt_tokens completion_tokens finish_reason <<< "$token_info"
    else
        prompt_tokens="?"; completion_tokens="?"; finish_reason="?"
        result="$raw_result"
    fi

    # Check if response is empty
    local trimmed
    trimmed=$(echo "$result" | tr -d '[:space:]')
    local status="OK"
    [[ -z "$trimmed" ]] && status="EMPTY"
    [[ "$finish_reason" == "length" ]] && status="TRUNCATED"

    echo ""
    echo "  [${elapsed}s | ${completion_tokens} tokens | ${finish_reason} | ${status}]"
    echo "$THIN_DIV"
    if [[ "$status" == "EMPTY" ]]; then
        echo "  (no visible output — thinking mode may have consumed all tokens)"
    else
        echo "$result" | fold -s -w 72 | sed 's/^/  /'
    fi
    echo ""

    # Append to summary
    SUMMARY+="  ${round_num}  ${difficulty}  ${elapsed}s  ${completion_tokens} tok  ${finish_reason}  ${status}\n"
}

SUMMARY=""

echo ""
echo "  ╔══════════════════════════════════════════════════════════════╗"
echo "  ║                    MODEL BENCHMARK                          ║"
echo "  ║         Coding Knowledge / Debug Skills / Reasoning         ║"
echo "  ╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "  Model:       $MODEL_NAME"
echo "  URL:         $LOCAL_MODEL_URL"
echo "  Max tokens:  $MAX_TOKENS"

# ── Round 1: Simple ──────────────────────────────────────────────────

run_round 1 "EASY" \
"What is the difference between a mutex and a semaphore? Give a short, concrete example of when you'd use each."

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
}"

# ── Round 3: Moderate ────────────────────────────────────────────────

run_round 3 "MODERATE" \
"A Kubernetes pod keeps getting OOMKilled despite the container only using 200Mi according to 'kubectl top pod'. The pod's memory limit is 512Mi. What are the most likely causes? Walk through your debugging steps."

# ── Round 4: Hard ────────────────────────────────────────────────────

run_round 4 "HARD" \
"Explain this bash one-liner — what does it do, and what's the subtle bug?

find . -name '*.log' -mtime +7 | xargs rm -f

Then rewrite it to be safe for filenames with spaces, newlines, and special characters."

# ── Round 5: Hard ────────────────────────────────────────────────────

run_round 5 "HARD" \
"I have a Python FastAPI app that works fine with 1 worker but returns stale data with multiple uvicorn workers. The app uses a module-level dict as a cache that gets populated on first request. Explain exactly why this breaks with multiple workers, and propose two different solutions — one simple, one robust."

# ── Round 6: Expert ──────────────────────────────────────────────────

run_round 6 "EXPERT" \
"You're debugging a production issue: a Rust service using tokio is consuming 100% CPU but its request throughput has dropped to near zero. Logs show no errors. Metrics show the task count is very high but tasks_completed_per_sec is near zero. What is the most likely cause? How would you confirm it? How would you fix it without a full rewrite?"

# ── Round 7: Expert ──────────────────────────────────────────────────

run_round 7 "EXPERT" \
"Write a PostgreSQL query that finds all customers who made purchases in every month of 2024 (Jan through Dec), but ONLY if their average order value increased month-over-month for at least 6 consecutive months during that period. Return customer_id, their longest streak of increasing monthly averages, and their total spend. Tables: orders(id, customer_id, amount, created_at)."

echo ""
echo "$DIVIDER"
echo "  BENCHMARK COMPLETE — $MODEL_NAME (max_tokens=$MAX_TOKENS)"
echo "$DIVIDER"
echo ""
echo "  Round  Difficulty  Time  Tokens  Finish     Status"
echo "  ─────  ──────────  ────  ──────  ─────────  ──────"
echo -e "$SUMMARY"
