#!/usr/bin/env bash

set -euo pipefail

MEMORY_BASE_URL="${MEMORY_BASE_URL:-http://memory-service.llama.svc.cluster.local}"
USER_ID="${USER_ID:-memory-test-user}"
CHAT_ID="${CHAT_ID:-memory-test-chat}"
FACT_TEXT="${FACT_TEXT:-I have a GTX 1080.}"
QUERY_TEXT="${QUERY_TEXT:-What GPU do I have?}"
KUBE_NAMESPACE="${KUBE_NAMESPACE:-llama}"
KUBECTL_IMAGE="${KUBECTL_IMAGE:-curlimages/curl:8.12.1}"

if ! command -v jq >/dev/null 2>&1; then
  echo "jq is required"
  exit 1
fi

if ! command -v kubectl >/dev/null 2>&1; then
  echo "kubectl is required"
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required"
  exit 1
fi

extract_json() {
  python3 -c 'import json, sys
text = sys.stdin.read()
decoder = json.JSONDecoder()
for idx, ch in enumerate(text):
    if ch != "{":
        continue
    try:
        obj, _ = decoder.raw_decode(text[idx:])
        print(json.dumps(obj))
        raise SystemExit(0)
    except json.JSONDecodeError:
        pass
print("No JSON object found in kubectl output", file=sys.stderr)
print(text, file=sys.stderr)
raise SystemExit(1)'
}

kube_curl() {
  local method="$1"
  local url="$2"
  local data="$3"
  local pod_name="memory-test-$(date +%s)-$RANDOM"

  kubectl run "$pod_name" \
    -n "$KUBE_NAMESPACE" \
    --rm \
    -i \
    --quiet \
    --restart=Never \
    --image "$KUBECTL_IMAGE" \
    --command -- \
    curl -sS -X "$method" "$url" -H "Content-Type: application/json" -d "$data"
}

echo "Upserting test memory..."
UPSERT_PAYLOAD="$(jq -cn \
  --arg user_id "$USER_ID" \
  --arg chat_id "$CHAT_ID" \
  --arg fact_text "$FACT_TEXT" \
  '{
    user_id: $user_id,
    chat_id: $chat_id,
    turns: [
      {role: "user", content: $fact_text},
      {role: "assistant", content: "Got it, I will remember that."}
    ]
  }')"

kube_curl "POST" "$MEMORY_BASE_URL/memory/upsert-turn" "$UPSERT_PAYLOAD" | extract_json >/tmp/memory-upsert.json

echo "Retrieving memory..."
RETRIEVE_PAYLOAD="$(jq -cn \
  --arg user_id "$USER_ID" \
  --arg chat_id "$CHAT_ID" \
  --arg query "$QUERY_TEXT" \
  '{
    user_id: $user_id,
    chat_id: $chat_id,
    query: $query
  }')"

kube_curl "POST" "$MEMORY_BASE_URL/memory/retrieve" "$RETRIEVE_PAYLOAD" | extract_json >/tmp/memory-retrieve.json

MEMORY_BLOCK="$(jq -r '.memory_block // ""' /tmp/memory-retrieve.json)"

echo
echo "Memory block:"
echo "-------------"
echo "$MEMORY_BLOCK"
echo

if grep -qi "gtx 1080" <<<"$MEMORY_BLOCK"; then
  echo "PASS: Memory recall includes GTX 1080"
  exit 0
fi

echo "FAIL: Memory recall did not include GTX 1080"
echo "Raw response:"
jq '.' /tmp/memory-retrieve.json
exit 1
