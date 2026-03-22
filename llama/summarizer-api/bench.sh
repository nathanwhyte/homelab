#!/usr/bin/env bash
set -euo pipefail

# Summarizer-API Benchmark
# Tests: /healthz latency, /summarize (3 modes), /v1/agent (tool-calling loop)
# Measures: latency, tokens, tok/s, output quality

PORT=8083
BASE="http://localhost:${PORT}"

# Kill any existing port-forward on this port
lsof -ti:${PORT} 2>/dev/null | xargs kill 2>/dev/null || true
sleep 1

echo "Starting port-forward to summarizer-api..."
kubectl port-forward -n llama svc/summarizer-api ${PORT}:80 &>/dev/null &
PF_PID=$!
trap "kill $PF_PID 2>/dev/null || true" EXIT

echo "Waiting for API..."
for attempt in $(seq 1 30); do
    if curl -sf "${BASE}/healthz" >/dev/null 2>&1; then
        echo "API healthy (attempt $attempt)"
        break
    fi
    if [ "$attempt" -eq 30 ]; then
        echo "Not healthy after 30 attempts"; exit 1
    fi
    sleep 2
done

echo ""
PASS=0
FAIL=0
TOTAL=0

run_test() {
    local test_name="$1"
    local endpoint="$2"
    local payload="$3"
    local validator="$4"  # python expression, receives 'data' (full response dict)

    TOTAL=$((TOTAL + 1))
    echo -n "[$TOTAL] $test_name... "

    local start_ms end_ms elapsed response
    start_ms=$(date +%s%3N)

    response=$(curl -sf --max-time 120 "${BASE}${endpoint}" \
        -H "Content-Type: application/json" \
        -d "$payload" 2>&1) || { echo "FAIL (curl error)"; FAIL=$((FAIL+1)); return; }

    end_ms=$(date +%s%3N)
    elapsed=$(( end_ms - start_ms ))

    # Validate and extract metrics
    local result
    result=$(python3 -c "
import json, sys, re

raw = sys.stdin.read()
try:
    data = json.loads(raw)
except:
    print('FAIL (json parse)')
    sys.exit(0)

# Extract key metrics
ok = data.get('ok', False)
usage = data.get('usage', {})
perf = data.get('perf', {})
tokens_out = usage.get('completion_tokens', 0)
gen_tps = perf.get('gen_tok_s', 0)
tools_used = data.get('tools_used', [])
iterations = data.get('iterations', 0)

# Content - could be 'summary' or 'content' depending on endpoint
content = data.get('summary', data.get('content', ''))
# Strip thinking tags
content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()

try:
    passed = bool($validator)
except Exception as e:
    passed = False

status = 'PASS' if (ok and passed) else 'FAIL'
preview = content[:120].replace(chr(10), ' ')

# Output: status|elapsed|tokens|tps|tools|iters|preview
print(f'{status}|{tokens_out}|{gen_tps}|{len(tools_used)}|{iterations}|{preview}')
" <<< "$response") || { echo "FAIL (validator error)"; FAIL=$((FAIL+1)); return; }

    IFS='|' read -r status tokens tps tools iters preview <<< "$result"

    if [[ "$status" == "PASS" ]]; then
        PASS=$((PASS+1))
        if [[ "$tools" != "0" ]]; then
            echo "PASS  ${elapsed}ms  ${tokens}tok  ${tps}t/s  ${tools}tools  ${iters}iter"
        else
            echo "PASS  ${elapsed}ms  ${tokens}tok  ${tps}t/s"
        fi
        echo "    > ${preview:0:100}"
    else
        FAIL=$((FAIL+1))
        echo "FAIL  ${elapsed}ms"
        echo "    > ${preview:0:100}"
    fi
}

# --- Healthz baseline ---
echo "=== Healthz Latency ==="
for i in 1 2 3; do
    start=$(date +%s%3N)
    curl -sf "${BASE}/healthz" >/dev/null
    end=$(date +%s%3N)
    echo "  ping $i: $(( end - start ))ms"
done

echo ""
echo "=== Summarize Endpoint ==="

# Test 1: Context summarization
run_test "Context mode — K8s incident" \
    "/v1/summarize" \
    '{
        "context": "The llama.cpp deployment on timmy crashed after 2 hours. Investigation showed OOM killer hitting the process — dmesg shows the container limit is 10Gi but Qwen3-8B with 8192 ctx and q8_0 KV cache peaks at 11.2Gi RSS. Solution: switched KV cache to q4_0 which cuts ~2GB, bringing peak RSS to ~9.1Gi. Pod is now stable. The model weights load into the 16GB VRAM on the RX 9070 XT, while the KV cache spills to system RAM bounded by the container memory limit.",
        "mode": "context",
        "max_tokens": 256
    }' \
    '"OOM" in content.lower() or "memory" in content.lower() or "kv" in content.lower()'

# Test 2: Conversation summarization
run_test "Conversation mode — GPU setup" \
    "/v1/summarize" \
    '{
        "context": "User: What GPUs do we have?\nAssistant: Three GPU nodes: timmy has an RX 9070 XT (16GB VRAM), manu has a GTX 1080 (8GB), and wemby has a GTX 1060 (6GB). Timmy runs the AMD amdgpu_exporter, while manu and wemby use NVIDIA DCGM.\nUser: Which one runs the LLM?\nAssistant: Timmy runs the shared Qwen3-8B model via llama.cpp with ROCm. The model fits entirely in VRAM with room to spare.\nUser: Can we run a second model on manu?\nAssistant: The 1080 has 8GB VRAM which can fit smaller models like Qwen3-1.7B or Phi-3-mini. However manu is also running the embedder, so you would need to check VRAM availability first.",
        "mode": "conversation",
        "max_tokens": 256
    }' \
    '("timmy" in content.lower() or "9070" in content) and ("gpu" in content.lower() or "vram" in content.lower())'

# Test 3: Code summarization
run_test "Code mode — Python function" \
    "/v1/summarize" \
    '{
        "context": "def agent_loop(messages, max_tokens=2048, temperature=0.3, max_iterations=10):\n    all_messages = list(messages)\n    tools_used = []\n    total_tokens = 0\n    for i in range(max_iterations):\n        result = llm_request_with_tools(all_messages, max_tokens, temperature, AGENT_TOOLS)\n        total_tokens += result.get(\"usage\", {}).get(\"total_tokens\", 0)\n        choice = result.get(\"choices\", [{}])[0]\n        message = choice.get(\"message\", {})\n        all_messages.append(message)\n        tool_calls = message.get(\"tool_calls\")\n        if not tool_calls:\n            content = extract_content(message)\n            return {\"ok\": True, \"content\": content, \"tools_used\": tools_used, \"iterations\": i + 1}\n        for tc in tool_calls:\n            tool_result = execute_tool(tc[\"function\"][\"name\"], json.loads(tc[\"function\"][\"arguments\"]))\n            tools_used.append({\"tool\": tc[\"function\"][\"name\"]})\n            all_messages.append({\"role\": \"tool\", \"tool_call_id\": tc[\"id\"], \"content\": tool_result})\n    return {\"ok\": True, \"content\": \"\", \"tools_used\": tools_used, \"iterations\": max_iterations, \"truncated\": True}",
        "mode": "code",
        "max_tokens": 256
    }' \
    '("loop" in content.lower() or "tool" in content.lower() or "agent" in content.lower()) and ("iteration" in content.lower() or "llm" in content.lower() or "message" in content.lower())'

# Test 4: Custom system prompt
run_test "Custom prompt — extract action items" \
    "/v1/summarize" \
    '{
        "context": "We need to migrate the database from PostgreSQL 14 to 16 before the end of Q2. Sarah will handle the schema changes, and Mike is testing the backup restore procedure. The staging environment should be updated first. After that, we need to update the connection pooler config for PgBouncer since some settings changed in PG16. Also, remember to update the Grafana dashboards to use the new pg_stat metrics available in 16.",
        "system_prompt": "/no_think Extract only the action items as a numbered list. Include who is responsible if mentioned.",
        "max_tokens": 256
    }' \
    '("1" in content or "1." in content) and ("sarah" in content.lower() or "migrate" in content.lower() or "schema" in content.lower())'

echo ""
echo "=== Agent Endpoint ==="

# Test 5: Agent — knowledge base search
run_test "Agent — search knowledge base" \
    "/v1/agent" \
    '{
        "messages": [
            {"role": "user", "content": "What directories exist in the OpenViking knowledge base? Use viking_ls to check."}
        ],
        "max_tokens": 512,
        "max_iterations": 3
    }' \
    'len(tools_used) >= 1 and any(t["tool"] == "viking_ls" for t in tools_used)'

# Test 6: Agent — semantic search
run_test "Agent — semantic search query" \
    "/v1/agent" \
    '{
        "messages": [
            {"role": "user", "content": "Search the knowledge base for information about GPU monitoring or dashboards."}
        ],
        "max_tokens": 512,
        "max_iterations": 3
    }' \
    'len(tools_used) >= 1 and any(t["tool"] in ("viking_search", "viking_find") for t in tools_used)'

echo ""
echo "================================"
echo "Results: $PASS passed, $FAIL failed out of $TOTAL tests"
echo "================================"
