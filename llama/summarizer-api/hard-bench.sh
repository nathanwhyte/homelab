#!/usr/bin/env bash
set -euo pipefail

# Summarizer-API Hard Benchmark
# Pushes the model with: long contexts, high token outputs, complex reasoning,
# multi-step agent chains, and rapid-fire sequential requests.

PORT=8083
BASE="http://localhost:${PORT}"

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

PASS=0; FAIL=0; TOTAL=0

run_test() {
    local test_name="$1" endpoint="$2" payload="$3" validator="$4"
    TOTAL=$((TOTAL + 1))
    echo -n "  [$TOTAL] $test_name... "

    local start_ms end_ms elapsed response
    start_ms=$(date +%s%3N)
    response=$(curl -sf --max-time 300 "${BASE}${endpoint}" \
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
prompt_tps = perf.get('prompt_tok_s', 0)
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
print(f'{status}|{tokens_out}|{gen_tps:.1f}|{prompt_tps:.1f}|{len(tools_used)}|{iterations}|{preview}')
" <<< "$response") || { echo "FAIL (validator)"; FAIL=$((FAIL+1)); return; }

    IFS='|' read -r status tokens tps ptps tools iters preview <<< "$result"
    if [[ "$status" == "PASS" ]]; then
        PASS=$((PASS+1))
        if [[ "$tools" != "0" ]]; then
            echo "PASS  ${elapsed}ms  ${tokens}tok  gen:${tps}t/s  prompt:${ptps}t/s  ${tools}tools  ${iters}iter"
        else
            echo "PASS  ${elapsed}ms  ${tokens}tok  gen:${tps}t/s  prompt:${ptps}t/s"
        fi
    else
        FAIL=$((FAIL+1))
        echo "FAIL  ${elapsed}ms  > ${preview:0:80}"
    fi
}

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  HARD BENCHMARK — GTX 1080 Performance Squeeze             ║"
echo "╚══════════════════════════════════════════════════════════════╝"

# --- Healthz baseline ---
echo "  --- Healthz ---"
for i in 1 2 3 4 5; do
    start=$(date +%s%3N); curl -sf "${BASE}/healthz" >/dev/null; end=$(date +%s%3N)
    echo "  ping $i: $(( end - start ))ms"
done

# ============================================================
echo "  --- Summarize: Long Context / High Output ---"
# ============================================================

# T1: Monster context (~3k tokens in), max output (1024 tokens)
run_test "Monster context — full infra postmortem (1024tok)" \
    "/v1/summarize" \
    '{
        "context": "QUARTERLY INFRASTRUCTURE REVIEW — Q1 2026\n\n## Cluster Overview\nThe homelab K3s cluster consists of 5 nodes: wemby (Intel i5-12400, 32GB, 256GB NVMe, GTX 1060 6GB), manu (AMD Ryzen 5 3600, 16GB, 1TB NVMe, GTX 1080 8GB), timmy (AMD Ryzen 7 7800X3D, 32GB, 2TB NVMe, RX 9070 XT 16GB), with two additional ARM nodes for edge workloads. The cluster runs K3s v1.31 with Flannel CNI in host-gw mode, Longhorn for distributed storage, and Traefik as the ingress controller.\n\n## Incident Summary\n\nIncident 1 (Jan 15): GPU Operator Installation Failure\nThe NVIDIA GPU operator v25.3.0 failed to install on manu due to kernel header mismatch. The Ubuntu 24.04 HWE kernel (6.8.0-106-generic) shipped headers that did not match the running kernel after an incomplete dist-upgrade. Resolution: Completed the dist-upgrade, rebooted, reinstalled GPU operator. Downtime: 4 hours. Impact: All GPU workloads on manu (embedder, DCGM exporter) were unavailable.\n\nIncident 2 (Jan 28): Longhorn Volume Corruption\nA power outage caused simultaneous node failures on wemby and manu. Longhorn volumes with replication factor 2 that had replicas on both affected nodes entered degraded state. Three PVCs (harbor-registry, grafana-data, coach-postgres) lost their healthy replicas. Resolution: Rebuilt volumes from backup, increased replication factor to 3 for critical volumes. Downtime: 8 hours. Data loss: 2 hours of harbor push history.\n\nIncident 3 (Feb 12): Flannel Subnet Race Condition\nAfter scheduled maintenance reboot of wemby, K3s agent started before flannel wrote /run/flannel/subnet.env. All pods on wemby entered CrashLoopBackOff with network errors. Resolution: Added ExecStartPre wait loop in k3s-agent.service. Applied to all nodes.\n\nIncident 4 (Feb 25): OOM Kill Storm on Timmy\nThe shared Qwen3-8B llama.cpp deployment experienced 45 OOM kills in 6 hours. Root cause: container memory limit (6Gi) was insufficient for 4 parallel slots with q8_0 KV cache under concurrent load. RSS peaked at 11.2Gi. Resolution: Switched KV cache to q4_0, increased container limit to 10Gi. Added monitoring alert for container_memory_working_set_bytes > 80% of limit.\n\nIncident 5 (Mar 8): Cloudflare Tunnel Split Brain\nBoth cloudflared replicas attempted to register the same tunnel simultaneously after a rolling restart. Cloudflare API rejected the duplicate registration, causing 30 minutes of external service downtime. Resolution: Added PodDisruptionBudget with maxUnavailable=1 and increased terminationGracePeriodSeconds to 60.\n\nIncident 6 (Mar 15): inotify Exhaustion on Manu\nHarbor pods failed with too many open files. Default inotify max_user_watches=8192 was exhausted by 47 pods on manu. Resolution: Set fs.inotify.max_user_watches=524288 in /etc/sysctl.d/99-k3s.conf on all nodes.\n\n## Performance Metrics\nCluster uptime: 99.2% (excluding planned maintenance). Mean time to recovery: 2.3 hours. GPU utilization (timmy): 73% average during OV indexing, 12% idle. Network throughput: 940Mbps between nodes (host-gw mode, R7000 router). Storage: 1.8TB total Longhorn capacity, 62% utilized.\n\n## Recommendations\n1. Upgrade to 3-way replication for all stateful workloads\n2. Add UPS to prevent simultaneous node failures\n3. Implement node drain automation before maintenance\n4. Set up PagerDuty integration for critical alerts\n5. Consider dedicated storage nodes to separate compute and storage failure domains",
        "mode": "context",
        "max_tokens": 1024
    }' \
    'len(content) > 500 and ("incident" in content.lower() or "oom" in content.lower() or "resolution" in content.lower())'

# T2: Dense technical analysis requiring structured output
run_test "Technical analysis — architecture comparison (768tok)" \
    "/v1/summarize" \
    '{
        "context": "Compare the following Kubernetes storage solutions for a homelab environment:\n\n1. Longhorn (current): Distributed block storage built for K8s. Pros: Easy setup, built-in UI, incremental snapshots, backup to S3. Cons: Performance overhead from network replication, node failure can cause volume degradation, resource hungry (each volume gets its own engine pod). Current usage: 1.8TB across 3 nodes, replication factor 2-3.\n\n2. Rook-Ceph: Production-grade distributed storage. Pros: Mature, supports block/file/object, erasure coding reduces storage overhead, better performance under load. Cons: Complex setup, requires minimum 3 dedicated OSDs, higher resource requirements (monitor daemons, MDS for CephFS), steep learning curve.\n\n3. OpenEBS (Mayastor): NVMe-native container storage. Pros: Excellent NVMe performance, simple architecture, NVMe-oF for low-latency replication. Cons: Requires NVMe SSDs on all nodes, younger project, limited ecosystem.\n\n4. Local-path + Velero: Simple local volumes with external backup. Pros: Maximum performance (no network hop), zero overhead, simple to understand. Cons: No replication (single point of failure), manual failover, backup is async only.\n\nEnvironment constraints: 3 main nodes, mixed NVMe/SATA storage, budget is zero (homelab), need to support stateful workloads (PostgreSQL, Harbor registry, Grafana). Current pain points: Longhorn volume rebuild times are slow (hours for large volumes), occasionally causes node pressure during rebalancing.",
        "system_prompt": "/no_think Provide a structured comparison with a recommendation. Use a scoring matrix (1-5) for: Performance, Reliability, Complexity, Resource Usage, Homelab Fit. Then give your top recommendation with reasoning.",
        "max_tokens": 768
    }' \
    '("score" in content.lower() or "matrix" in content.lower() or "recommend" in content.lower() or "1" in content) and len(content) > 400'

# T3: Code review requiring deep understanding
run_test "Deep code review — async agent with error handling (768tok)" \
    "/v1/summarize" \
    '{
        "context": "Review this production code for bugs, performance issues, and security concerns:\n\nimport asyncio\nimport httpx\nimport json\nfrom typing import Optional\nfrom dataclasses import dataclass, field\n\n@dataclass\nclass AgentConfig:\n    llm_url: str\n    api_key: str = \"sk-no-key-required\"\n    max_iterations: int = 10\n    max_tokens: int = 2048\n    temperature: float = 0.3\n    timeout: float = 120.0\n    retry_count: int = 3\n    retry_delay: float = 1.0\n\n@dataclass\nclass AgentResult:\n    ok: bool\n    content: str = \"\"\n    tools_used: list = field(default_factory=list)\n    iterations: int = 0\n    usage: dict = field(default_factory=dict)\n    error: Optional[str] = None\n\nclass AsyncAgent:\n    def __init__(self, config: AgentConfig, tools: dict):\n        self.config = config\n        self.tools = tools\n        self.client = httpx.AsyncClient(timeout=config.timeout)\n        self._tool_schemas = [self._make_schema(name, fn) for name, fn in tools.items()]\n\n    async def run(self, messages: list) -> AgentResult:\n        all_messages = list(messages)\n        tools_used = []\n        total_usage = {}\n        for i in range(self.config.max_iterations):\n            try:\n                response = await self._llm_call(all_messages)\n            except httpx.TimeoutException:\n                return AgentResult(ok=False, error=\"LLM timeout\", tools_used=tools_used, iterations=i+1)\n            except httpx.HTTPStatusError as e:\n                if e.response.status_code == 503:\n                    await asyncio.sleep(self.config.retry_delay * (i + 1))\n                    continue\n                return AgentResult(ok=False, error=f\"HTTP {e.response.status_code}\", iterations=i+1)\n            choice = response[\"choices\"][0]\n            message = choice[\"message\"]\n            all_messages.append(message)\n            if not message.get(\"tool_calls\"):\n                content = self._extract_content(message)\n                return AgentResult(ok=True, content=content, tools_used=tools_used, iterations=i+1, usage=total_usage)\n            for tc in message[\"tool_calls\"]:\n                name = tc[\"function\"][\"name\"]\n                args = json.loads(tc[\"function\"][\"arguments\"])\n                if name not in self.tools:\n                    result = json.dumps({\"error\": f\"Unknown tool: {name}\"})\n                else:\n                    try:\n                        result = await self.tools[name](**args)\n                    except Exception as e:\n                        result = json.dumps({\"error\": str(e)})\n                tools_used.append({\"tool\": name, \"args\": args})\n                all_messages.append({\"role\": \"tool\", \"tool_call_id\": tc[\"id\"], \"content\": str(result)})\n        return AgentResult(ok=True, content=\"\", tools_used=tools_used, iterations=self.config.max_iterations, usage=total_usage)\n\n    async def _llm_call(self, messages):\n        for attempt in range(self.config.retry_count):\n            try:\n                resp = await self.client.post(f\"{self.config.llm_url}/v1/chat/completions\", json={\"messages\": messages, \"max_tokens\": self.config.max_tokens, \"temperature\": self.config.temperature, \"tools\": self._tool_schemas}, headers={\"Authorization\": f\"Bearer {self.config.api_key}\"})\n                resp.raise_for_status()\n                return resp.json()\n            except (httpx.TimeoutException, httpx.ConnectError) as e:\n                if attempt == self.config.retry_count - 1:\n                    raise\n                await asyncio.sleep(self.config.retry_delay * (attempt + 1))\n\n    async def close(self):\n        await self.client.aclose()\n\n    def _extract_content(self, message):\n        import re\n        content = message.get(\"content\", \"\")\n        return re.sub(r\"<think>.*?</think>\", \"\", content, flags=re.DOTALL).strip()\n\n    def _make_schema(self, name, fn):\n        return {\"type\": \"function\", \"function\": {\"name\": name, \"description\": fn.__doc__ or \"\", \"parameters\": {\"type\": \"object\", \"properties\": {}}}}",
        "mode": "code",
        "max_tokens": 768
    }' \
    '("bug" in content.lower() or "issue" in content.lower() or "security" in content.lower() or "review" in content.lower() or "concern" in content.lower()) and len(content) > 300'

# ============================================================
echo "  --- Summarize: Rapid-Fire Sequential (prompt cache test) ---"
# ============================================================

# T4-T6: Same system prompt, different questions — tests prompt cache reuse
for i in 1 2 3; do
    case $i in
        1) q="What are the key metrics to monitor for a Kubernetes cluster? Cover CPU, memory, disk, network, and pod-level metrics. Be specific about PromQL queries."; v="'promql' in content.lower() or 'cpu' in content.lower() or 'metric' in content.lower()" ;;
        2) q="How does Flannel host-gw mode compare to VXLAN for a 3-node homelab? Cover latency, throughput, MTU implications, and failure modes."; v="'flannel' in content.lower() or 'vxlan' in content.lower() or 'host-gw' in content.lower()" ;;
        3) q="Explain the tradeoffs between q4_0 and q8_0 KV cache quantization in llama.cpp. When would you choose each?"; v="'kv' in content.lower() or 'quantiz' in content.lower() or 'q4' in content.lower()" ;;
    esac
    run_test "Rapid-fire #${i} (prompt cache test, 512tok)" \
        "/v1/summarize" \
        "{
            \"context\": \"${q}\",
            \"system_prompt\": \"/no_think You are a senior infrastructure engineer. Give detailed, practical answers with specific examples and commands where relevant.\",
            \"max_tokens\": 512
        }" \
        "$v"
done

# ============================================================
echo "  --- Agent: Complex Multi-Step Chains ---"
# ============================================================

# T7: Deep dive — search → read → analyze → search again → synthesize
run_test "Agent — deep dive with re-search (8 iter)" \
    "/v1/agent" \
    '{
        "messages": [
            {"role": "user", "content": "I need a thorough analysis of the homelab GPU infrastructure. Start by searching for GPU-related content, then read the most detailed document you find. After reading it, search for any network configuration that might affect GPU workload performance. Finally, synthesize everything into a comprehensive report covering: 1) Current GPU allocation per node, 2) How GPU workloads are served (models, endpoints, routing), 3) Any network bottlenecks that could affect inference latency. Use multiple tools to gather all the information you need."}
        ],
        "max_tokens": 1024,
        "max_iterations": 8
    }' \
    'len(tools_used) >= 3 and iterations >= 3'

# T8: Exploratory with writing — search → explore → add knowledge
run_test "Agent — explore and document (8 iter)" \
    "/v1/agent" \
    '{
        "messages": [
            {"role": "user", "content": "Explore the OpenViking knowledge base to find information about the LLM infrastructure. List the homelab project structure, drill into the llama directory, read any configuration documents you find. Then create a brief summary note of what you discovered and add it to the knowledge base at viking://resources/projects/homelab/llama using viking_add_text. Report everything you did."}
        ],
        "max_tokens": 1024,
        "max_iterations": 8
    }' \
    'len(tools_used) >= 3 and any(t["tool"] == "viking_add_text" for t in tools_used)'

# T9: Analytical — compare two topics via separate searches
run_test "Agent — comparative analysis (8 iter)" \
    "/v1/agent" \
    '{
        "messages": [
            {"role": "user", "content": "Compare the storage and compute architectures of this homelab. First search for storage-related content (Longhorn, Garage, S3), then separately search for compute/GPU content. Read at least one document from each search. Write a comparison analyzing: How do the storage and compute layers interact? Are there any bottlenecks where storage performance could limit GPU workload throughput? What would you change?"}
        ],
        "max_tokens": 1024,
        "max_iterations": 8
    }' \
    'len(tools_used) >= 3 and iterations >= 3'

# ============================================================
echo "  --- Concurrent: Back-to-Back Burst ---"
# ============================================================

# T10: Two simultaneous summarize requests
echo -n "  [$((TOTAL+1))] Concurrent pair (2x summarize)... "
TOTAL=$((TOTAL + 1))
start_burst=$(date +%s%3N)
pids=()
curl -sf --max-time 120 "${BASE}/v1/summarize" \
    -H "Content-Type: application/json" \
    -d '{"context": "Explain how Kubernetes service discovery works with CoreDNS, including headless services and ExternalName resolution.", "mode": "context", "max_tokens": 512}' \
    -o /dev/null -w "%{http_code}" &
pids+=($!)
curl -sf --max-time 120 "${BASE}/v1/summarize" \
    -H "Content-Type: application/json" \
    -d '{"context": "Describe the lifecycle of a Kubernetes pod from scheduling to termination, including init containers, readiness gates, and graceful shutdown.", "mode": "context", "max_tokens": 512}' \
    -o /dev/null -w "%{http_code}" &
pids+=($!)

burst_pass=0
for pid in "${pids[@]}"; do
    wait "$pid" 2>/dev/null && burst_pass=$((burst_pass + 1))
done
end_burst=$(date +%s%3N)
burst_elapsed=$(( end_burst - start_burst ))
if [ "$burst_pass" -eq 2 ]; then
    PASS=$((PASS + 1))
    echo "PASS  ${burst_elapsed}ms  ${burst_pass}/2 ok"
else
    FAIL=$((FAIL + 1))
    echo "FAIL  ${burst_elapsed}ms  ${burst_pass}/2 ok"
fi

echo ""
echo "  ══════════════════════════════════════════════════════════════"
echo "  Results: $PASS passed, $FAIL failed / $TOTAL tests"
echo "  ══════════════════════════════════════════════════════════════"
