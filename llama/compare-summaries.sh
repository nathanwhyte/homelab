#!/usr/bin/env bash
set -euo pipefail

# Compare Qwen3 14B summary with Claude's summary of the same text.
# Usage:
#   bash compare-summaries.sh --file conversation.txt --mode conversation
#   cat conversation.txt | bash compare-summaries.sh
#   bash compare-summaries.sh --file transcript.jsonl --mode context --max-tokens 2048

MODE="conversation"
MAX_TOKENS="1024"
INPUT_FILE=""
API_URL="http://summarizer-api.llama.svc.cluster.local/summarize"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --file)   INPUT_FILE="$2"; shift 2 ;;
        --mode)   MODE="$2"; shift 2 ;;
        --max-tokens) MAX_TOKENS="$2"; shift 2 ;;
        --help|-h)
            echo "Usage: compare-summaries.sh [--file <path>] [--mode context|conversation|code] [--max-tokens <n>]"
            echo ""
            echo "Sends text to the Qwen3 14B summarizer API and prints the summary."
            echo "Reads from --file or stdin. Default mode: conversation."
            exit 0
            ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# Read input
if [[ -n "$INPUT_FILE" ]]; then
    if [[ ! -f "$INPUT_FILE" ]]; then
        echo "Error: file not found: $INPUT_FILE" >&2
        exit 1
    fi
    CONTEXT=$(cat "$INPUT_FILE")
else
    if [[ -t 0 ]]; then
        echo "Error: no input. Use --file <path> or pipe text via stdin." >&2
        exit 1
    fi
    CONTEXT=$(cat)
fi

if [[ -z "$CONTEXT" ]]; then
    echo "Error: empty input." >&2
    exit 1
fi

CHAR_COUNT=${#CONTEXT}
echo "Sending ${CHAR_COUNT} chars to Qwen3 14B (mode: ${MODE})..." >&2

# Truncate to ~28K chars to stay within context window
if [[ $CHAR_COUNT -gt 28000 ]]; then
    CONTEXT="${CONTEXT:0:28000}"
    echo "Truncated to 28000 chars to fit context window." >&2
fi

# Escape the context for JSON
ESCAPED_CONTEXT=$(printf '%s' "$CONTEXT" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read()))')

PAYLOAD=$(cat <<ENDJSON
{
  "context": ${ESCAPED_CONTEXT},
  "mode": "${MODE}",
  "max_tokens": ${MAX_TOKENS}
}
ENDJSON
)

# Send to the cluster-internal API via a temporary pod
RESULT=$(kubectl run summarize-tmp \
    --rm -i \
    --restart=Never \
    --namespace=llama \
    --image=curlimages/curl:8.12.1 \
    --override-type=strategic \
    -- \
    curl -s -X POST "$API_URL" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD" 2>/dev/null)

# Extract summary from JSON response
SUMMARY=$(printf '%s' "$RESULT" | python3 -c '
import sys, json
try:
    data = json.loads(sys.stdin.read())
    if data.get("ok"):
        print(data["summary"])
    else:
        print(f"Error: {data.get(\"error\", \"unknown\")}: {data.get(\"detail\", \"\")}", file=sys.stderr)
        sys.exit(1)
except Exception as e:
    print(f"Failed to parse response: {e}", file=sys.stderr)
    print(f"Raw response: {sys.stdin.read()}", file=sys.stderr)
    sys.exit(1)
')

echo "$SUMMARY"
