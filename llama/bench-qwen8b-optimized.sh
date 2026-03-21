#!/usr/bin/env bash
set -euo pipefail

# Benchmark Qwen3-8B OPTIMIZED on manu's GTX 1080
# Config G: ctx=16384, q4_0 KV, parallel=6, flash-attn, batch=2048, max_tokens=2048

OUTPUT_DIR="./summarization-showdown-results"
mkdir -p "$OUTPUT_DIR"

PORT=8001
BASE="http://localhost:${PORT}"

# Kill any existing port-forward on this port
lsof -ti:${PORT} 2>/dev/null | xargs kill 2>/dev/null || true
sleep 1

echo "Starting port-forward to qwen-summarizer..."
kubectl port-forward -n llama deployment/qwen-summarizer ${PORT}:${PORT} &>/dev/null &
PF_PID=$!
trap "kill $PF_PID 2>/dev/null || true" EXIT

echo "Waiting for model to be ready..."
for attempt in $(seq 1 30); do
    if python3 -c "
import urllib.request
urllib.request.urlopen('${BASE}/health', timeout=5)
" 2>/dev/null; then
        echo "healthy (attempt $attempt)"
        break
    fi
    if [ "$attempt" -eq 30 ]; then
        echo "Not healthy after 30 attempts"; exit 1
    fi
    sleep 3
done

echo "Qwen3-8B OPTIMIZED on manu. Running 5 rounds..."
echo ""

declare -a SYS_PROMPTS=(
    "/no_think You are a documentation summarizer. Given a directory listing with file descriptions, write a concise .abstract.md summary. Include: purpose, key files, component relationships. Markdown, 3-8 sentences."
    "/no_think You are a context summarizer for an AI agent. Preserve: 1) Key decisions and rationale, 2) Important facts/names/values, 3) Current state and pending actions, 4) Errors or blockers. Bullet points. Terse."
    "/no_think You are a code summarizer. Describe: 1) Purpose and what it does, 2) Key functions and roles, 3) Notable patterns, dependencies, design decisions. Be specific about names and values."
    "/no_think You are a session memory extractor. Extract from the conversation: 1) User preferences/corrections, 2) Decisions and rationale, 3) Technical facts about the project, 4) Patterns for future sessions. Structured list only."
    "/no_think You are a technical document summarizer. Condense preserving all critical technical details, decisions, trade-offs, and action items. Omit boilerplate. Structured markdown."
)

INPUT_1='Directory: gpu/dashboards/
Files:
  gpu-overview-dashboard.yaml (ConfigMap, 450 lines)
    - Grafana dashboard JSON in ConfigMap, panels for temp/util/memory/power
    - Queries DCGM (NVIDIA) + amdgpu_exporter (AMD) Prometheus metrics
    - Legend: {{exported_instance}} - {{modelName}}, variable: $datasource
  README.md (12 lines) - deploy instructions: kubectl apply -f ...
Context:
  - Parent gpu/ has gpu/nvidia/ and gpu/amd/ with vendor-specific exporters
  - 3 GPU nodes: timmy (RX 9070 XT), manu (GTX 1080), wemby (GTX 1060)
  - Dashboard auto-discovered by Grafana sidecar
  - Had duplicate DCGM dashboards (Helm + ConfigMap) - resolved by removing Helm 12239'

INPUT_2='User: The llama.cpp deployment keeps crashing on timmy after ~2 hours.
Assistant: Checked the pod logs. The OOM killer is hitting it - dmesg shows "Out of memory: Killed process 1234 (llama-server)". The container limit is 10Gi but llama-server with Qwen3.5-27B Q3_K_M + 8192 ctx + q8_0 KV cache peaks at ~11.2Gi RSS. Two options: 1) raise memory limit to 14Gi, 2) drop KV cache to q4_0 which cuts ~2GB.
User: Lets go with option 2, I dont want to squeeze manu. What about the context size?
Assistant: Reduced KV cache to q4_0 in the deployment. Context size 8192 is fine - the memory issue was purely KV cache precision. With q4_0 KV the RSS should peak around 9.1Gi, well within the 10Gi limit. Redeployed - pod is stable now, 45 minutes uptime and counting.
User: Perfect. Also, for future reference - timmy has 32GB system RAM and 16GB VRAM on the 9070 XT. The model itself loads into VRAM, the KV cache spills to system RAM.
Assistant: Good to know. So the 10Gi container limit bounds the system RAM portion (KV cache + overhead), not the VRAM model weights. Updated the deployment annotation with this note.'

INPUT_3='#!/usr/bin/env python3
"""Agent context summarization API.
POST /summarize  - summarize context text
POST /v1/summarize - alias
GET  /healthz    - health check
"""
from __future__ import annotations
import json, os, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LLM_URL = os.getenv("LLM_URL", "http://qwen-summarizer-llm.llama.svc.cluster.local/v1/chat/completions")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen3-14b")
BIND_PORT = int(os.getenv("BIND_PORT", "8082"))
DEFAULT_MAX_TOKENS = int(os.getenv("DEFAULT_MAX_TOKENS", "1024"))

SYSTEM_PROMPTS = {
    "context": "/no_think You are a precise context summarizer...",
    "conversation": "/no_think You are a conversation summarizer...",
    "code": "/no_think You are a code change summarizer...",
}

def llm_request(messages, max_tokens, temperature):
    payload = {"model": LLM_MODEL, "messages": messages,
               "max_tokens": max_tokens, "temperature": temperature,
               "min_p": 0.05, "repeat_penalty": 1.1, "stream": False}
    req = urllib.request.Request(url=LLM_URL, data=json.dumps(payload).encode(),
                                method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        result = json.loads(resp.read())
    return {"content": result["choices"][0]["message"]["content"],
            "timings": result.get("timings", {}), "usage": result.get("usage", {})}

class SummarizerHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path in ("/summarize", "/v1/summarize"):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            context = body.get("context", "")
            mode = body.get("mode", "context")
            system_prompt = SYSTEM_PROMPTS.get(mode, SYSTEM_PROMPTS["context"])
            if body.get("system_prompt"): system_prompt = body["system_prompt"]
            result = llm_request([{"role":"system","content":system_prompt},
                                  {"role":"user","content":context}],
                                 int(body.get("max_tokens", DEFAULT_MAX_TOKENS)),
                                 float(body.get("temperature", 0.3)))
            self.send_json(200, {"ok": True, "summary": result["content"]})'

INPUT_4='User: Can you check what GPU monitoring we have set up?
Assistant: Found three exporters: NVIDIA DCGM on manu/wemby, AMD amdgpu_exporter on timmy. Created a combined dashboard in gpu/dashboards/gpu-overview-dashboard.yaml that queries both. Deployed to Grafana.
User: Nice. Use tables when showing me structured data, not bullet lists.
Assistant: Got it - tables for structured data going forward. Here is the GPU inventory:
| Node | GPU | VRAM | Exporter |
|------|-----|------|----------|
| timmy | RX 9070 XT | 16GB | amdgpu_exporter |
| manu | GTX 1080 | 8GB | DCGM |
| wemby | GTX 1060 | 6GB | DCGM |
User: We had duplicate DCGM dashboards. Did you fix that?
Assistant: Yes - the old one came from Helm downloading dashboard 12239 from grafana.com. Removed it from kube-prometheus-stack-values.yaml and the download-dashboards init container. The new one lives as a ConfigMap with sidecar label. Restarted Grafana, confirmed only one DCGM dashboard remains.
User: Good. Also, dont summarize what you just did at the end of every response. I can read the diff. Just get to the point.
Assistant: Understood.'

INPUT_5='## Longhorn Backup Architecture Decision

### Current State
- Longhorn v1.8.1 running on K3s cluster (3 nodes: manu, wemby, patty)
- All PVCs use local-path or Longhorn with 1 replica (no redundancy)
- No off-cluster backups configured
- Total PVC usage: ~47GB across 12 volumes

### Options Evaluated

**Option A: S3 to Cloudflare R2**
- Cost: $0 for storage (10GB free), $0.015/GB after. Egress free.
- Longhorn native S3 backup target - minimal config
- Encryption at rest via Longhorn (AES-256)
- Incremental backups supported
- Con: requires creating R2 bucket, generating API keys, configuring secret

**Option B: NFS to local NAS**
- Cost: $0 (existing Synology NAS)
- Simple NFS backup target
- Con: NAS is in same room - no disaster recovery
- Con: NAS is old, disk health unknown

**Option C: Restic to Backblaze B2**
- Cost: $0.006/GB/mo storage, $0.01/GB egress
- Velero + Restic for full cluster backup (not just volumes)
- Con: more complex setup, separate tooling from Longhorn
- Con: restore requires Velero, not just Longhorn

### Decision
Going with Option A (R2) as primary, Option B (NFS) as local fast-restore cache.
- R2 gives us off-site disaster recovery at near-zero cost
- NFS gives fast local restores without egress latency
- Longhorn handles both natively - no extra tooling
- Schedule: daily incremental, weekly full, 30-day retention

### Action Items
- [ ] Create R2 bucket "homelab-longhorn-backup"
- [ ] Generate R2 API credentials with write access
- [ ] Create K8s secret with S3 endpoint, key, secret
- [ ] Configure Longhorn backup target via UI or settings
- [ ] Test backup + restore cycle on a non-critical volume
- [ ] Set up recurring backup schedule
- [ ] Configure NFS backup target as secondary'

INPUTS=("$INPUT_1" "$INPUT_2" "$INPUT_3" "$INPUT_4" "$INPUT_5")

CONFIG="G"

echo "  Config  Round  Time   Tokens  tok/s   Preview"
echo "  ------  -----  ----   ------  -----   -------"

for i in 0 1 2 3 4; do
    round=$((i + 1))
    sys="${SYS_PROMPTS[$i]}"
    input="${INPUTS[$i]}"

    start_ts=$(date +%s)

    result=$(python3 -c "
import json, urllib.request, sys

payload = json.dumps({
    'messages': [
        {'role': 'system', 'content': sys.argv[1]},
        {'role': 'user', 'content': sys.argv[2]}
    ],
    'max_tokens': 2048,
    'temperature': 0.3,
    'min_p': 0.05,
    'repeat_penalty': 1.1,
    'stream': False
}).encode()

req = urllib.request.Request(
    '${BASE}/v1/chat/completions',
    data=payload,
    method='POST',
    headers={'Content-Type': 'application/json'}
)
with urllib.request.urlopen(req, timeout=300) as resp:
    r = json.loads(resp.read())

c = r['choices'][0]['message']['content']
u = r.get('usage', {})
t = r.get('timings', {})
print(json.dumps({
    'content': c,
    'completion_tokens': u.get('completion_tokens', 0),
    'tok_s': round(t.get('predicted_per_second', 0), 1)
}))
" "$sys" "$input" 2>&1) || result='{"content":"ERROR","completion_tokens":0,"tok_s":0}'

    end_ts=$(date +%s)
    elapsed=$((end_ts - start_ts))

    content=$(printf '%s' "$result" | python3 -c "import json,sys; print(json.loads(sys.stdin.read()).get('content','ERROR'))" 2>/dev/null || echo "ERROR")
    tokens=$(printf '%s' "$result" | python3 -c "import json,sys; print(json.loads(sys.stdin.read()).get('completion_tokens',0))" 2>/dev/null || echo "0")
    tok_s=$(printf '%s' "$result" | python3 -c "import json,sys; print(json.loads(sys.stdin.read()).get('tok_s',0))" 2>/dev/null || echo "0")

    preview=$(echo "$content" | head -1 | cut -c1-40)
    printf "  %-8s R%-5s %3ss   %5s   %5s   %s\n" "[$CONFIG]" "$round" "$elapsed" "$tokens" "$tok_s" "$preview"

    cat > "$OUTPUT_DIR/R${round}_${CONFIG}.md" <<ENDOUT
# Round $round — Config $CONFIG (Qwen3-8B OPTIMIZED on manu/1080)
**Settings:** ctx=4096, q4_0 KV, parallel=8, flash-attn, batch=2048
**Time:** ${elapsed}s | **Tokens out:** $tokens | **Gen tok/s:** $tok_s

## Output
$content
ENDOUT

    python3 -c "
import json,sys
print(json.dumps({
    'round': int(sys.argv[1]),
    'config': sys.argv[2],
    'elapsed_s': int(sys.argv[3]),
    'completion_tokens': int(sys.argv[4]),
    'tok_s': float(sys.argv[5])
}))
" "$round" "$CONFIG" "$elapsed" "$tokens" "$tok_s" >> "$OUTPUT_DIR/results.jsonl"

done

echo ""
echo "Done. Results in $OUTPUT_DIR/R*_F.md"
