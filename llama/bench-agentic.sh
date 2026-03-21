#!/usr/bin/env bash
set -euo pipefail

# Agentic Benchmark for Qwen3-14B on timmy's RX 9070 XT
# Tests: tool calling, multi-step reasoning, code generation, structured output
# Designed to mirror Claude Code + Sonnet/Opus capabilities

OUTPUT_DIR="./agentic-bench-results"
mkdir -p "$OUTPUT_DIR"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
REPORT="$OUTPUT_DIR/report-${TIMESTAMP}.md"

PORT=8002
BASE="http://localhost:${PORT}"

# Kill any existing port-forward on this port
lsof -ti:${PORT} 2>/dev/null | xargs kill 2>/dev/null || true
sleep 1

echo "Starting port-forward to llamacpp-rocm..."
kubectl port-forward -n llama deployment/llamacpp-rocm ${PORT}:8000 &>/dev/null &
PF_PID=$!
trap "kill $PF_PID 2>/dev/null || true" EXIT

echo "Waiting for model to be ready..."
for attempt in $(seq 1 60); do
    if curl -sf "${BASE}/health" >/dev/null 2>&1; then
        echo "Model healthy (attempt $attempt)"
        break
    fi
    if [ "$attempt" -eq 60 ]; then
        echo "Not healthy after 60 attempts"; exit 1
    fi
    sleep 5
done

# Get model info
MODEL_INFO=$(curl -sf "${BASE}/v1/models" | python3 -c "import sys,json; m=json.load(sys.stdin); print(m.get('data',[{}])[0].get('id','unknown'))" 2>/dev/null || echo "unknown")
echo "Model: $MODEL_INFO"
echo ""

cat > "$REPORT" <<EOF
# Agentic Benchmark Report
- **Model**: ${MODEL_INFO}
- **Date**: $(date -Iseconds)
- **Endpoint**: llamacpp-rocm on timmy (RX 9070 XT, 16GB VRAM)
- **Config**: ctx=16384, parallel=2, KV=q8_0, flash-attn

## Results

EOF

PASS=0
FAIL=0
TOTAL=0

run_test() {
    local test_name="$1"
    local description="$2"
    local payload="$3"
    local validator="$4"  # python expression that receives 'content' variable
    local max_tokens="${5:-2048}"

    TOTAL=$((TOTAL + 1))
    echo -n "[$TOTAL] $test_name... "

    local start_ms
    start_ms=$(date +%s%3N)

    local response
    response=$(curl -sf --max-time 120 "${BASE}/v1/chat/completions" \
        -H "Content-Type: application/json" \
        -d "$payload" 2>&1) || { echo "FAIL (curl error)"; FAIL=$((FAIL+1)); return; }

    local end_ms
    end_ms=$(date +%s%3N)
    local elapsed=$(( end_ms - start_ms ))

    local content
    content=$(echo "$response" | python3 -c "
import sys, json
r = json.load(sys.stdin)
c = r.get('choices',[{}])[0].get('message',{}).get('content','')
# Strip thinking tags if present
import re
c = re.sub(r'<think>.*?</think>', '', c, flags=re.DOTALL).strip()
print(c)
" 2>/dev/null) || { echo "FAIL (parse error)"; FAIL=$((FAIL+1)); return; }

    local tokens_out
    tokens_out=$(echo "$response" | python3 -c "
import sys, json
r = json.load(sys.stdin)
print(r.get('usage',{}).get('completion_tokens',0))
" 2>/dev/null || echo "0")

    local tps="0"
    if [ "$elapsed" -gt 0 ] && [ "$tokens_out" -gt 0 ]; then
        tps=$(python3 -c "print(f'{${tokens_out} / (${elapsed}/1000):.1f}')")
    fi

    # Validate
    local result
    result=$(python3 -c "
content = '''$( echo "$content" | sed "s/'''/\\\\'\\\\'\\\\'/" )'''
try:
    passed = bool($validator)
    print('PASS' if passed else 'FAIL')
except Exception as e:
    print(f'FAIL ({e})')
" 2>/dev/null) || result="FAIL (validator error)"

    if [[ "$result" == "PASS" ]]; then
        PASS=$((PASS+1))
        echo "PASS (${elapsed}ms, ${tokens_out} tokens, ${tps} t/s)"
    else
        FAIL=$((FAIL+1))
        echo "FAIL (${elapsed}ms)"
        echo "  Response preview: ${content:0:200}"
    fi

    cat >> "$REPORT" <<ENTRY
### $TOTAL. $test_name
- **Description**: $description
- **Result**: $result
- **Time**: ${elapsed}ms | Tokens: ${tokens_out} | Speed: ${tps} t/s
- **Response** (first 500 chars):
\`\`\`
${content:0:500}
\`\`\`

ENTRY
}

echo "=== Tool Calling Tests ==="
echo ""

# Test 1: Single tool call with structured JSON
run_test "Single Tool Call" \
    "Model generates a single well-formed tool call with correct JSON arguments" \
    '{
        "model": "qwen3-14b",
        "max_tokens": 1024,
        "messages": [
            {"role": "system", "content": "/no_think You are a coding assistant with access to tools. When you need to use a tool, respond with a JSON tool call in this format:\n{\"tool\": \"tool_name\", \"arguments\": {\"key\": \"value\"}}\n\nAvailable tools:\n- read_file(path: string) - Read a file from disk\n- write_file(path: string, content: string) - Write content to a file\n- run_command(command: string) - Execute a shell command\n- search_code(pattern: string, path: string) - Search for code patterns"},
            {"role": "user", "content": "Find all Python files in src/ that import requests"}
        ]
    }' \
    '"search_code" in content or "run_command" in content and "pattern" in content.lower() or "requests" in content'

# Test 2: Multi-step tool chain reasoning
run_test "Multi-Step Tool Plan" \
    "Model plans a sequence of tool calls to accomplish a multi-step task" \
    '{
        "model": "qwen3-14b",
        "max_tokens": 1024,
        "messages": [
            {"role": "system", "content": "/no_think You are a coding assistant. Plan and describe the sequence of tool calls needed (do not execute, just plan). Available tools: read_file, write_file, run_command, search_code, git_diff, create_pr."},
            {"role": "user", "content": "I need you to find the function calculate_tax in our codebase, add a 5% surcharge parameter with a default of False, update all callers to pass the new parameter, run the tests, and create a PR."}
        ]
    }' \
    'content.count("search_code") + content.count("read_file") + content.count("write_file") + content.count("run_command") + content.count("git") + content.count("create_pr") >= 4'

# Test 3: Tool call with observation loop
run_test "Tool-Observation Loop" \
    "Model correctly processes tool results and decides next action" \
    '{
        "model": "qwen3-14b",
        "max_tokens": 1024,
        "messages": [
            {"role": "system", "content": "/no_think You are a debugging assistant. Use tools to investigate and fix issues. Respond with your next tool call or your conclusion."},
            {"role": "user", "content": "The API is returning 500 errors on /api/users endpoint"},
            {"role": "assistant", "content": "{\"tool\": \"run_command\", \"arguments\": {\"command\": \"kubectl logs deploy/api-server --tail=20\"}}"},
            {"role": "user", "content": "Tool result: TypeError: Cannot read property '\''email'\'' of null\n    at /app/src/handlers/users.js:42\n    at processTicksAndRejections"},
            {"role": "assistant", "content": "{\"tool\": \"read_file\", \"arguments\": {\"path\": \"/app/src/handlers/users.js\"}}"},
            {"role": "user", "content": "Tool result:\n38: async function getUsers(req, res) {\n39:   const users = await db.query(\"SELECT * FROM users\");\n40:   const enriched = users.map(u => ({\n41:     name: u.name,\n42:     email: u.email.toLowerCase(),\n43:     role: u.role\n44:   }));\n45:   res.json(enriched);\n46: }"},
            {"role": "user", "content": "What is the bug and how do you fix it?"}
        ]
    }' \
    '"null" in content.lower() and ("optional" in content.lower() or "?." in content or "&&" in content or "u.email" in content or "undefined" in content.lower())'

echo ""
echo "=== Code Generation Tests ==="
echo ""

# Test 4: Generate a complete function with edge cases
run_test "Function Generation" \
    "Model generates a correct, complete function handling edge cases" \
    '{
        "model": "qwen3-14b",
        "max_tokens": 1024,
        "messages": [
            {"role": "system", "content": "/no_think You are an expert programmer. Write clean, correct code."},
            {"role": "user", "content": "Write a Python function `merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]` that merges overlapping intervals. Example: [(1,3),(2,6),(8,10),(15,18)] -> [(1,6),(8,10),(15,18)]"}
        ]
    }' \
    '"def merge_intervals" in content and "sort" in content.lower()'

# Test 5: Bug fix with explanation
run_test "Bug Fix" \
    "Model identifies and fixes a subtle bug in code" \
    '{
        "model": "qwen3-14b",
        "max_tokens": 1024,
        "messages": [
            {"role": "system", "content": "/no_think You are a code reviewer. Find the bug, explain it, and provide the fix."},
            {"role": "user", "content": "This function should return the longest common subsequence but it is wrong:\n\ndef lcs(s1, s2):\n    m, n = len(s1), len(s2)\n    dp = [[0] * (n + 1)] * (m + 1)\n    for i in range(1, m + 1):\n        for j in range(1, n + 1):\n            if s1[i-1] == s2[j-1]:\n                dp[i][j] = dp[i-1][j-1] + 1\n            else:\n                dp[i][j] = max(dp[i-1][j], dp[i][j-1])\n    return dp[m][n]"}
        ]
    }' \
    '("same" in content.lower() and "row" in content.lower()) or ("reference" in content.lower()) or ("shallow" in content.lower()) or ("copy" in content.lower()) or ("[[0]" in content and "for" in content)'

# Test 6: Kubernetes YAML generation
run_test "K8s Manifest Generation" \
    "Model generates correct Kubernetes YAML for a real deployment scenario" \
    '{
        "model": "qwen3-14b",
        "max_tokens": 1536,
        "messages": [
            {"role": "system", "content": "/no_think You are a Kubernetes expert. Generate valid YAML manifests."},
            {"role": "user", "content": "Create a Kubernetes CronJob that runs every 6 hours to clean up images older than 7 days from a container registry. It should:\n- Run in namespace \"registry\"\n- Use image harbor.internal/tools/registry-cleaner:latest\n- Set env var REGISTRY_URL from a secret called registry-creds key url\n- Set memory limit 256Mi, cpu limit 500m\n- Keep last 3 successful and 1 failed job"}
        ]
    }' \
    '"CronJob" in content and "schedule" in content and "registry" in content and "secretKeyRef" in content'

echo ""
echo "=== Structured Output Tests ==="
echo ""

# Test 7: JSON schema adherence
run_test "JSON Schema Output" \
    "Model produces valid JSON matching a specified schema" \
    '{
        "model": "qwen3-14b",
        "max_tokens": 1024,
        "messages": [
            {"role": "system", "content": "/no_think Respond with ONLY valid JSON matching the schema. No markdown, no explanation.\nSchema: {\"type\": \"object\", \"properties\": {\"name\": {\"type\": \"string\"}, \"severity\": {\"enum\": [\"low\", \"medium\", \"high\", \"critical\"]}, \"affected_files\": {\"type\": \"array\", \"items\": {\"type\": \"string\"}}, \"fix_description\": {\"type\": \"string\"}, \"estimated_effort_hours\": {\"type\": \"number\"}}, \"required\": [\"name\", \"severity\", \"affected_files\", \"fix_description\"]}"},
            {"role": "user", "content": "SQL injection vulnerability found in the login endpoint at src/auth/login.py where user input is concatenated into a SQL query string on line 45."}
        ]
    }' \
    'content.strip().startswith("{") and "severity" in content and "affected_files" in content and "fix_description" in content'

# Test 8: Diff/patch generation
run_test "Diff Generation" \
    "Model generates a correct unified diff for a code change" \
    '{
        "model": "qwen3-14b",
        "max_tokens": 1024,
        "messages": [
            {"role": "system", "content": "/no_think Generate a unified diff (like git diff) for the requested change. Output only the diff, no explanation."},
            {"role": "user", "content": "File: src/config.py\nCurrent content:\nDATABASE_URL = \"postgresql://localhost:5432/mydb\"\nDEBUG = True\nMAX_RETRIES = 3\nTIMEOUT = 30\n\nChange: Read DATABASE_URL from environment variable with the current value as fallback, and set DEBUG to False."}
        ]
    }' \
    '("---" in content or "@@" in content or "diff" in content.lower()) and ("os.environ" in content or "os.getenv" in content) and "False" in content'

echo ""
echo "=== Reasoning & Planning Tests ==="
echo ""

# Test 9: Architecture decision
run_test "Architecture Reasoning" \
    "Model reasons about trade-offs and recommends an architecture" \
    '{
        "model": "qwen3-14b",
        "max_tokens": 1536,
        "messages": [
            {"role": "system", "content": "/no_think You are a senior software architect. Be specific and practical."},
            {"role": "user", "content": "We have a monolithic Python Django app (50k LOC) serving 100 req/s. The image processing pipeline (resize, watermark, optimize) takes 2-5 seconds and blocks the request thread. Users are complaining about slow page loads. What is the best approach to fix this without a full microservices migration? Consider our small team (3 devs) and existing infra (K8s cluster, Redis, PostgreSQL)."}
        ]
    }' \
    '("celery" in content.lower() or "queue" in content.lower() or "async" in content.lower() or "background" in content.lower()) and ("redis" in content.lower() or "worker" in content.lower())'

# Test 10: Error analysis and root cause
run_test "Error Root Cause Analysis" \
    "Model traces through a stack trace to identify root cause and fix" \
    '{
        "model": "qwen3-14b",
        "max_tokens": 1024,
        "messages": [
            {"role": "system", "content": "/no_think You are a production incident responder. Analyze the error and provide root cause + fix."},
            {"role": "user", "content": "Production alert - pod crashlooping:\n\nkubectl logs api-server-7f8b9c-x2k4j:\nStarting server on :8080...\nConnecting to database...\npanic: runtime error: invalid memory address or nil pointer dereference\n[signal SIGSEGV: segmentation violation]\n\ngoroutine 1 [running]:\nmain.(*Server).initRoutes(0x0)\n    /app/server.go:45 +0x28\nmain.main()\n    /app/main.go:23 +0x158\n\nkubectl describe pod api-server-7f8b9c-x2k4j:\n  Environment:\n    DB_HOST: postgres.db.svc\n    DB_PORT: 5432\n    DB_NAME: appdb\n    DB_PASSWORD: <set from secret db-creds key password>\n  Last State: Terminated - Exit Code 2\n  Restart Count: 47"}
        ]
    }' \
    '"nil" in content.lower() and ("server" in content.lower() or "initRoutes" in content.lower() or "pointer" in content.lower())'

echo ""
echo "================================"
echo "Results: $PASS passed, $FAIL failed, $TOTAL total"
echo "Report: $REPORT"

cat >> "$REPORT" <<SUMMARY
## Summary

| Metric | Value |
|--------|-------|
| Passed | $PASS / $TOTAL |
| Failed | $FAIL |
| Model | $MODEL_INFO |

### Test Categories
| Category | Tests |
|----------|-------|
| Tool Calling | 1-3 |
| Code Generation | 4-6 |
| Structured Output | 7-8 |
| Reasoning & Planning | 9-10 |
SUMMARY

echo ""
echo "Done. Full report at: $REPORT"
