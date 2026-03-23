#!/usr/bin/env bash
set -euo pipefail

# Summarizer-API Stress Benchmark
# Heavy workload: long contexts, high token counts, complex agent chains, concurrent requests
# Tests GPU/model under OV indexing load

PORT=8083
BASE="http://localhost:${PORT}"
LOG="/tmp/stress-bench-$(date +%Y%m%d-%H%M%S).log"

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
    [ "$attempt" -eq 30 ] && { echo "Not healthy after 30 attempts"; exit 1; }
    sleep 2
done

RUN=0
run_bench() {
    RUN=$((RUN + 1))
    local ts
    ts=$(date '+%H:%M:%S')
    echo ""
    echo "╔══════════════════════════════════════════════════╗"
    echo "║  STRESS RUN #${RUN}  —  ${ts}                        "
    echo "╚══════════════════════════════════════════════════╝"

    PASS=0; FAIL=0; TOTAL=0

    run_test() {
        local test_name="$1" endpoint="$2" payload="$3" validator="$4"
        TOTAL=$((TOTAL + 1))
        echo -n "  [$TOTAL] $test_name... "

        local start_ms end_ms elapsed response
        start_ms=$(date +%s%3N)
        response=$(curl -sf --max-time 180 "${BASE}${endpoint}" \
            -H "Content-Type: application/json" \
            -d "$payload" 2>&1) || { echo "FAIL (curl error/timeout)"; FAIL=$((FAIL+1)); return; }
        end_ms=$(date +%s%3N)
        elapsed=$(( end_ms - start_ms ))

        local result
        result=$(python3 -c "
import json, sys, re
raw = sys.stdin.read()
try:
    data = json.loads(raw)
except:
    print('FAIL|0|0|0|0|json parse error')
    sys.exit(0)
ok = data.get('ok', False)
usage = data.get('usage', {})
perf = data.get('perf', {})
tokens_out = usage.get('completion_tokens', 0)
gen_tps = perf.get('gen_tok_s', 0)
tools_used = data.get('tools_used', [])
iterations = data.get('iterations', 0)
content = data.get('summary', data.get('content', ''))
content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
try:
    passed = bool($validator)
except Exception:
    passed = False
status = 'PASS' if (ok and passed) else 'FAIL'
preview = content[:100].replace(chr(10), ' ')
print(f'{status}|{tokens_out}|{gen_tps}|{len(tools_used)}|{iterations}|{preview}')
" <<< "$response") || { echo "FAIL (validator)"; FAIL=$((FAIL+1)); return; }

        IFS='|' read -r status tokens tps tools iters preview <<< "$result"
        if [[ "$status" == "PASS" ]]; then
            PASS=$((PASS+1))
            if [[ "$tools" != "0" ]]; then
                echo "PASS  ${elapsed}ms  ${tokens}tok  ${tps}t/s  ${tools}tools  ${iters}iter"
            else
                echo "PASS  ${elapsed}ms  ${tokens}tok  ${tps}t/s"
            fi
        else
            FAIL=$((FAIL+1))
            echo "FAIL  ${elapsed}ms  > ${preview:0:80}"
        fi
    }

    # --- Healthz under load ---
    echo "  --- Healthz ---"
    for i in 1 2 3; do
        start=$(date +%s%3N); curl -sf "${BASE}/healthz" >/dev/null; end=$(date +%s%3N)
        echo "  ping $i: $(( end - start ))ms"
    done

    # --- Heavy summarize: long context, high token output ---
    echo "  --- Summarize (heavy) ---"

    # T1: Very long technical context (~2k tokens input), 512 output
    run_test "Long context — multi-incident postmortem (512tok)" \
        "/v1/summarize" \
        '{
            "context": "INCIDENT POSTMORTEM — 2026-03-15 through 2026-03-18\n\nIncident 1: GPU OOM on timmy (March 15, 09:00 UTC)\nThe llama.cpp deployment running Qwen3-8B on the RX 9070 XT crashed after sustained load. Investigation showed the OOM killer was triggered when container RSS peaked at 11.2GiB against the 10Gi limit. Root cause: KV cache was configured with q8_0 quantization, consuming ~4.5GB for 8192 context length across 4 parallel slots. Combined with model weights (4.5GB in VRAM with spillover) and runtime overhead, total memory exceeded limits. Resolution: Switched KV cache to q4_0 quantization, reducing KV cache memory by approximately 50% to ~2.25GB. Peak RSS dropped to ~9.1GiB. Applied via llama-server flag --cache-type-k q4_0 --cache-type-v q4_0. Verified stable for 48 hours.\n\nIncident 2: Flannel subnet race condition (March 16, 03:00 UTC)\nNode wemby failed to rejoin the cluster after a scheduled reboot. K3s agent service started before flannel had written /run/flannel/subnet.env, causing the CNI plugin to fail pod scheduling. All pods on wemby entered CrashLoopBackOff. Symptom: kubectl get nodes showed wemby as Ready but pods showed network errors. Root cause: systemd service ordering — k3s-agent.service had After=network-online.target but flannel subnet allocation happens asynchronously after the k3s server connection. Resolution: Added ExecStartPre=/bin/bash -c while [ ! -f /run/flannel/subnet.env ]; do sleep 1; done to the k3s-agent systemd unit. Also added a 30-second timeout to prevent infinite wait.\n\nIncident 3: inotify limit exhaustion on manu (March 17, 14:00 UTC)\nHarbor registry pods on manu began failing with too many open files errors. Root cause: default inotify max_user_watches=8192 was insufficient for Harbor plus Longhorn plus monitoring stack. The node had 47 pods each needing inotify watches. Resolution: Set fs.inotify.max_user_watches=524288 and fs.inotify.max_user_instances=512 in /etc/sysctl.d/99-k3s.conf. Applied to all nodes proactively.\n\nIncident 4: Cloudflare Tunnel reconnection storm (March 18, 11:00 UTC)\nAll external services (k8s.nathanwhyte.dev, lamp.nathanwhyte.dev, registry.nathanwhyte.dev) became unreachable. The cloudflared deployment had 2 replicas with anti-affinity, but both pods were evicted during a Longhorn volume rebalance that caused node pressure on wemby and manu simultaneously. Resolution: Added PodDisruptionBudget with minAvailable=1 to the cloudflared deployment. Also added a dedicated node affinity to ensure at least one replica runs on a node not affected by storage operations.\n\nCross-cutting themes: Resource limits need headroom for spikes. Startup ordering matters in distributed systems. Proactive sysctl tuning prevents cascading failures. External access depends on internal stability.",
            "mode": "context",
            "max_tokens": 512
        }' \
        '("ooom" in content.lower() or "memory" in content.lower() or "flannel" in content.lower() or "incident" in content.lower()) and len(content) > 200'

    # T2: Dense conversation, max output
    run_test "Dense conversation — architecture review (512tok)" \
        "/v1/summarize" \
        '{
            "context": "User: Walk me through the full request path when someone queries the agent API.\nAssistant: Here is the full path: 1) Client sends POST to summarizer-api.llama.svc:80/v1/agent with messages array. 2) The summarizer-api Flask app receives the request and enters agent_loop(). 3) agent_loop formats the messages with the system prompt containing available tools (viking_search, viking_read, viking_find, viking_ls, viking_add_text). 4) It sends the messages to the shared LLM at qwen-summarizer-llm.llama.svc:80 which proxies to llamacpp-rocm:8000 on timmy. 5) The Qwen3-8B model generates a response. If it contains tool_calls, the agent loop executes each tool against the OpenViking API at openviking.viking.svc:1933. 6) Tool results are appended as tool role messages. 7) The loop repeats up to max_iterations times. 8) When the model generates a response without tool_calls (finish_reason=stop), the content is extracted and returned.\nUser: What happens if the LLM is busy with OV semantic generation?\nAssistant: The shared LLM has 4 parallel slots with 8192 context each. OV VLM uses up to 6 concurrent connections and the summarizer-api uses 2. When all slots are busy, new requests queue at the llama.cpp level. The server returns responses in order of completion, so latency increases but requests dont fail. However if the queue grows too deep, the HTTP request may timeout.\nUser: Could we add priority to certain requests?\nAssistant: llama.cpp doesnt support request priority natively. Options: 1) Separate model instances (but we only have one GPU). 2) Rate limit OV VLM connections to leave slots free. 3) Add a proxy layer that queues requests with priority. Option 2 is simplest — we already configure OV VLM concurrency in the OV settings.\nUser: What about the embedder? Does it compete for GPU?\nAssistant: No. The embedder runs nomic-embed-text-v1.5 on CPU only (f16 weights, 768-dim output). Its deployed as embedder-llamacpp in the viking namespace on timmy but uses zero VRAM. It has its own service at embedder-llamacpp.viking.svc:8080. The only GPU consumer is the shared Qwen3-8B model.",
            "mode": "conversation",
            "max_tokens": 512
        }' \
        '("agent" in content.lower() or "tool" in content.lower() or "llm" in content.lower()) and len(content) > 200'

    # T3: Complex code with high token output
    run_test "Code analysis — full agent loop (512tok)" \
        "/v1/summarize" \
        '{
            "context": "class AgentLoop:\n    def __init__(self, llm_url, tools, max_iterations=10, max_tokens=2048, temperature=0.3):\n        self.llm_url = llm_url\n        self.tools = {t.name: t for t in tools}\n        self.max_iterations = max_iterations\n        self.max_tokens = max_tokens\n        self.temperature = temperature\n        self.tool_schemas = [t.to_openai_schema() for t in tools]\n\n    def run(self, messages):\n        all_messages = list(messages)\n        tools_used = []\n        total_usage = {\"prompt_tokens\": 0, \"completion_tokens\": 0}\n        \n        for i in range(self.max_iterations):\n            response = self._llm_request(all_messages)\n            usage = response.get(\"usage\", {})\n            total_usage[\"prompt_tokens\"] += usage.get(\"prompt_tokens\", 0)\n            total_usage[\"completion_tokens\"] += usage.get(\"completion_tokens\", 0)\n            \n            choice = response[\"choices\"][0]\n            message = choice[\"message\"]\n            finish_reason = choice.get(\"finish_reason\", \"stop\")\n            all_messages.append(message)\n            \n            tool_calls = message.get(\"tool_calls\")\n            if not tool_calls or finish_reason == \"stop\":\n                content = self._extract_content(message)\n                return AgentResult(ok=True, content=content, tools_used=tools_used, iterations=i+1, usage=total_usage)\n            \n            for tc in tool_calls:\n                func_name = tc[\"function\"][\"name\"]\n                func_args = json.loads(tc[\"function\"][\"arguments\"])\n                if func_name not in self.tools:\n                    result = f\"Error: Unknown tool {func_name}\"\n                else:\n                    try:\n                        result = self.tools[func_name].execute(**func_args)\n                    except Exception as e:\n                        result = f\"Error executing {func_name}: {str(e)}\"\n                tools_used.append({\"tool\": func_name, \"args\": func_args})\n                all_messages.append({\"role\": \"tool\", \"tool_call_id\": tc[\"id\"], \"content\": str(result)})\n        \n        return AgentResult(ok=True, content=\"\", tools_used=tools_used, iterations=self.max_iterations, truncated=True)\n\n    def _llm_request(self, messages):\n        payload = {\"model\": \"qwen3-8b\", \"messages\": messages, \"max_tokens\": self.max_tokens, \"temperature\": self.temperature, \"tools\": self.tool_schemas}\n        resp = httpx.post(f\"{self.llm_url}/v1/chat/completions\", json=payload, timeout=120)\n        resp.raise_for_status()\n        return resp.json()\n\n    def _extract_content(self, message):\n        content = message.get(\"content\", \"\")\n        if \"<think>\" in content:\n            content = re.sub(r\"<think>.*?</think>\", \"\", content, flags=re.DOTALL)\n        return content.strip()",
            "mode": "code",
            "max_tokens": 512
        }' \
        '("agent" in content.lower() or "tool" in content.lower() or "loop" in content.lower()) and len(content) > 200'

    # --- Heavy agent: multi-step reasoning ---
    echo "  --- Agent (heavy) ---"

    # T4: Agent — multi-hop: search → read → synthesize
    run_test "Agent — multi-hop knowledge retrieval (5 iter)" \
        "/v1/agent" \
        '{
            "messages": [
                {"role": "user", "content": "I need a detailed summary of how the homelab LLM infrastructure works. First search for LLM-related content, then read the most relevant result, and give me a comprehensive answer covering: which model runs where, how requests are routed, what the resource limits are, and how failover works."}
            ],
            "max_tokens": 1024,
            "max_iterations": 5
        }' \
        'len(tools_used) >= 2 and iterations >= 2'

    # T5: Agent — exploratory: ls → find → read chain
    run_test "Agent — filesystem exploration (5 iter)" \
        "/v1/agent" \
        '{
            "messages": [
                {"role": "user", "content": "Explore the OpenViking knowledge base structure. First list the top-level directories, then drill into the homelab project to find GPU-related content, and read one document about GPU configuration. Report what you found."}
            ],
            "max_tokens": 1024,
            "max_iterations": 5
        }' \
        'len(tools_used) >= 2 and iterations >= 2'

    # T6: Agent — analytical: search + compare
    run_test "Agent — cross-reference search (5 iter)" \
        "/v1/agent" \
        '{
            "messages": [
                {"role": "user", "content": "Compare information about the network configuration and the GPU setup in the homelab. Search for both topics separately, then synthesize a report about how the network and compute resources relate to each other."}
            ],
            "max_tokens": 1024,
            "max_iterations": 5
        }' \
        'len(tools_used) >= 2'

    # --- Concurrent burst: 3 summarize requests at once ---
    echo "  --- Concurrent burst (3 parallel) ---"
    local pids=() results=() start_burst end_burst
    start_burst=$(date +%s%3N)

    for q in "Explain Kubernetes pod scheduling" "Describe how Prometheus scraping works" "What is a container runtime interface"; do
        curl -sf --max-time 120 "${BASE}/v1/summarize" \
            -H "Content-Type: application/json" \
            -d "{\"context\": \"${q}. Provide a detailed technical explanation covering architecture, components, and common configuration patterns.\", \"mode\": \"context\", \"max_tokens\": 256}" \
            -o /dev/null -w "%{http_code}" &
        pids+=($!)
    done

    local burst_pass=0
    for pid in "${pids[@]}"; do
        if wait "$pid" 2>/dev/null; then
            burst_pass=$((burst_pass + 1))
        fi
    done
    end_burst=$(date +%s%3N)
    local burst_elapsed=$(( end_burst - start_burst ))

    TOTAL=$((TOTAL + 1))
    if [ "$burst_pass" -eq 3 ]; then
        PASS=$((PASS + 1))
        echo "  [${TOTAL}] Concurrent burst (3x summarize)... PASS  ${burst_elapsed}ms  ${burst_pass}/3 ok"
    else
        FAIL=$((FAIL + 1))
        echo "  [${TOTAL}] Concurrent burst (3x summarize)... FAIL  ${burst_elapsed}ms  ${burst_pass}/3 ok"
    fi

    echo ""
    echo "  ══════════════════════════════════════════"
    echo "  Run #${RUN}: $PASS passed, $FAIL failed / $TOTAL tests"
    echo "  ══════════════════════════════════════════"
    echo "$(date '+%H:%M:%S') | Run #${RUN} | ${PASS}/${TOTAL} passed | ${FAIL} failed" >> "$LOG"
}

INTERVAL=${1:-180}  # default: every 3 minutes
echo "Stress benchmark — running every ${INTERVAL}s (log: $LOG)"
echo "Press Ctrl-C to stop"
echo ""

while true; do
    run_bench
    echo ""
    echo "  Next run in ${INTERVAL}s..."
    sleep "$INTERVAL"
done
