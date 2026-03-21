#!/usr/bin/env bash
set -euo pipefail

# ══════════════════════════════════════════════════════════════════════
# Summarization Showdown
#   A: Mistral-Small-24B (/no_think)     — fast baseline
#   B: Qwen3.5-27B (/no_think)           — smart, thinking disabled
#   C: Qwen3.5-27B (thinking enabled)    — smart, full reasoning
#   D: Claude Haiku 4.5                  — cloud reference
#
# Rounds mirror OpenViking's actual VLM workload:
#   R1: Directory abstracting (.abstract.md)
#   R2: Conversation context summarization
#   R3: Code summarization
#   R4: Session memory extraction
#   R5: Mixed technical content with nuance
#
# NOTE: A uses Mistral, B/C use Qwen — you must swap the loaded model
#       between groups. The script pauses to let you do this.
#
# Usage:
#   bash summarization-showdown.sh
#   bash summarization-showdown.sh --config A,D
#   bash summarization-showdown.sh --round 3
# ══════════════════════════════════════════════════════════════════════

NAMESPACE="llama"
LOCAL_MODEL_URL=""
API_KEY=""
ONLY_CONFIGS=""
ONLY_ROUND=""
OUTPUT_DIR=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --local-url) LOCAL_MODEL_URL="$2"; shift 2 ;;
        --api-key)   API_KEY="$2"; shift 2 ;;
        --config)    ONLY_CONFIGS="$2"; shift 2 ;;
        --round)     ONLY_ROUND="$2"; shift 2 ;;
        --output)    OUTPUT_DIR="$2"; shift 2 ;;
        --help|-h)
            sed -n '3,/^$/p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *) shift ;;
    esac
done

# ── Helpers ────────────────────────────────────────────────────────

die()  { echo "Error: $*" >&2; exit 1; }
info() { echo "  $*"; }

DIVIDER="$(printf '%.0s─' {1..72})"

should_run() {
    local type="$1" val="$2" filter="$3"
    [[ -z "$filter" ]] && return 0
    [[ "$filter" == *"$val"* ]] && return 0
    return 1
}

# ── Setup ──────────────────────────────────────────────────────────

command -v claude &>/dev/null || die "claude CLI not found"
command -v jq    &>/dev/null || die "jq not found (sudo apt install jq)"

[[ -z "$LOCAL_MODEL_URL" ]] && LOCAL_MODEL_URL="http://llama.local"

if [[ -z "$API_KEY" ]]; then
    API_KEY=$(kubectl get secret llama-rocm-api-key -n "$NAMESPACE" \
        -o jsonpath='{.data.API_KEY}' 2>/dev/null | base64 -d 2>/dev/null || true)
    [[ -n "$API_KEY" ]] && info "Loaded API key from K8s secret."
fi

CURL_AUTH=()
[[ -n "$API_KEY" ]] && CURL_AUTH=(-H "Authorization: Bearer $API_KEY")

[[ -z "$OUTPUT_DIR" ]] && OUTPUT_DIR="./summarization-showdown-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$OUTPUT_DIR"

RESULTS_JSON="$OUTPUT_DIR/results.jsonl"
: > "$RESULTS_JSON"

# ── Model functions ────────────────────────────────────────────────

detect_model() {
    curl -sf "${CURL_AUTH[@]}" "$LOCAL_MODEL_URL/v1/models" 2>/dev/null \
    | jq -r '.data[0].id // "unknown"' \
    | sed 's|.*/||; s|\.gguf$||' || echo "unknown-model"
}

check_health() {
    curl -sf "${CURL_AUTH[@]}" "$LOCAL_MODEL_URL/health" >/dev/null 2>&1
}

wait_for_model() {
    local label="$1"
    if check_health; then return 0; fi
    info "Waiting for local model ($label) to become healthy..."
    for i in $(seq 1 60); do
        sleep 5
        if check_health; then
            info "Model is ready."
            return 0
        fi
        printf "."
    done
    die "Model did not become healthy after 5 minutes"
}

call_local() {
    local sys="$1" user="$2" max_tok="${3:-1024}"
    local resp
    resp=$(curl -sf "${CURL_AUTH[@]}" "$LOCAL_MODEL_URL/v1/chat/completions" \
        --max-time 600 \
        -H "Content-Type: application/json" \
        -d "$(python3 -c "
import json,sys
print(json.dumps({
    'messages':[
        {'role':'system','content':sys.argv[1]},
        {'role':'user','content':sys.argv[2]}
    ],
    'max_tokens':int(sys.argv[3]),
    'temperature':0.3,
    'min_p':0.05,
    'repeat_penalty':1.1,
    'stream':False
}))
" "$sys" "$user" "$max_tok")" 2>/dev/null) || { echo "ERROR"; return; }

    python3 -c "
import json,sys
r=json.loads(sys.stdin.read())
c=r.get('choices',[{}])[0].get('message',{}).get('content','ERROR')
u=r.get('usage',{})
t=r.get('timings',{})
print(json.dumps({
    'content':c,
    'prompt_tokens':u.get('prompt_tokens',0),
    'completion_tokens':u.get('completion_tokens',0),
    'tok_s':round(t.get('predicted_per_second',0),1)
}))
" <<< "$resp"
}

call_claude() {
    local sys="$1" user="$2"
    local out
    out=$(claude --print --model haiku "System instruction: $sys

$user" 2>/dev/null) || out="ERROR: claude failed"
    python3 -c "
import json,sys
print(json.dumps({'content':sys.argv[1],'prompt_tokens':0,'completion_tokens':0,'tok_s':0}))
" "$out"
}

# ── Run a single config+round ─────────────────────────────────────

run_test() {
    local config="$1" round="$2" sys_prompt="$3" user_input="$4" max_tok="${5:-1024}"

    local start_ts end_ts elapsed result content tokens_out
    start_ts=$(date +%s)

    if [[ "$config" == "D" ]]; then
        result=$(call_claude "$sys_prompt" "$user_input")
    else
        result=$(call_local "$sys_prompt" "$user_input" "$max_tok")
    fi

    end_ts=$(date +%s)
    elapsed=$((end_ts - start_ts))

    if [[ "$result" == "ERROR" ]]; then
        content="ERROR: request failed"
        tokens_out=0
    else
        content=$(printf '%s' "$result" | jq -r '.content // "ERROR"')
        tokens_out=$(printf '%s' "$result" | jq -r '.completion_tokens // 0')
    fi

    # Save full output
    local outfile="$OUTPUT_DIR/R${round}_${config}.md"
    cat > "$outfile" <<ENDOUT
# Round $round — Config $config
**Time:** ${elapsed}s | **Tokens out:** $tokens_out

## Output
$content
ENDOUT

    # Append to JSONL
    python3 -c "
import json,sys
print(json.dumps({
    'round':int(sys.argv[1]),
    'config':sys.argv[2],
    'elapsed_s':int(sys.argv[3]),
    'completion_tokens':int(sys.argv[4]),
    'output_file':sys.argv[5],
    'content_preview':sys.argv[6][:200]
}))
" "$round" "$config" "$elapsed" "$tokens_out" "$outfile" "$content" >> "$RESULTS_JSON"

    # Print live status
    printf "  %-8s Round %-2s  %3ss  %5s tok  %s\n" \
        "[$config]" "$round" "$elapsed" "$tokens_out" \
        "$(echo "$content" | head -1 | cut -c1-50)"
}

# ── Round definitions ──────────────────────────────────────────────
# Each round: description, /no_think sys prompt, thinking sys prompt, user input

declare -a ROUND_NAMES=(
    "Directory Abstract"
    "Context Summarization"
    "Code Summarization"
    "Memory Extraction"
    "Mixed Technical"
)

SYS_NT_1="/no_think You are a documentation summarizer. Given a directory listing with file descriptions, write a concise .abstract.md summary. Include: purpose, key files, component relationships. Markdown, 3-8 sentences."
SYS_TH_1="You are a documentation summarizer. Given a directory listing with file descriptions, write a concise .abstract.md summary. Include: purpose, key files, component relationships. Markdown, 3-8 sentences."

SYS_NT_2="/no_think You are a context summarizer for an AI agent. Preserve: 1) Key decisions and rationale, 2) Important facts/names/values, 3) Current state and pending actions, 4) Errors or blockers. Bullet points. Terse."
SYS_TH_2="You are a context summarizer for an AI agent. Preserve: 1) Key decisions and rationale, 2) Important facts/names/values, 3) Current state and pending actions, 4) Errors or blockers. Bullet points. Terse."

SYS_NT_3="/no_think You are a code summarizer. Describe: 1) Purpose and what it does, 2) Key functions and roles, 3) Notable patterns, dependencies, design decisions. Be specific about names and values."
SYS_TH_3="You are a code summarizer. Describe: 1) Purpose and what it does, 2) Key functions and roles, 3) Notable patterns, dependencies, design decisions. Be specific about names and values."

SYS_NT_4="/no_think You are a session memory extractor. Extract from the conversation: 1) User preferences/corrections, 2) Decisions and rationale, 3) Technical facts about the project, 4) Patterns for future sessions. Structured list only."
SYS_TH_4="You are a session memory extractor. Extract from the conversation: 1) User preferences/corrections, 2) Decisions and rationale, 3) Technical facts about the project, 4) Patterns for future sessions. Structured list only."

SYS_NT_5="/no_think You are a technical document summarizer. Condense preserving all critical technical details, decisions, trade-offs, and action items. Omit boilerplate. Structured markdown."
SYS_TH_5="You are a technical document summarizer. Condense preserving all critical technical details, decisions, trade-offs, and action items. Omit boilerplate. Structured markdown."

# ── Round inputs ───────────────────────────────────────────────────

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
  - Had duplicate DCGM dashboards (Helm + ConfigMap) — resolved by removing Helm 12239'

INPUT_2='User: The llama.cpp deployment keeps crashing on timmy after ~2 hours.
Assistant: Checked the pod logs. The OOM killer is hitting it — dmesg shows "Out of memory: Killed process 1234 (llama-server)". The container limit is 10Gi but llama-server with Qwen3.5-27B Q3_K_M + 8192 ctx + q8_0 KV cache peaks at ~11.2Gi RSS. Two options: 1) raise memory limit to 14Gi, 2) drop KV cache to q4_0 which cuts ~2GB.
User: Lets go with option 2, I dont want to squeeze manu. What about the context size?
Assistant: Reduced KV cache to q4_0 in the deployment. Context size 8192 is fine — the memory issue was purely KV cache precision. With q4_0 KV the RSS should peak around 9.1Gi, well within the 10Gi limit. Redeployed — pod is stable now, 45 minutes uptime and counting.
User: Perfect. Also, for future reference — timmy has 32GB system RAM and 16GB VRAM on the 9070 XT. The model itself loads into VRAM, the KV cache spills to system RAM.
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
Assistant: Got it — tables for structured data going forward. Here is the GPU inventory:
| Node | GPU | VRAM | Exporter |
|------|-----|------|----------|
| timmy | RX 9070 XT | 16GB | amdgpu_exporter |
| manu | GTX 1080 | 8GB | DCGM |
| wemby | GTX 1060 | 6GB | DCGM |
User: We had duplicate DCGM dashboards. Did you fix that?
Assistant: Yes — the old one came from Helm downloading dashboard 12239 from grafana.com. Removed it from kube-prometheus-stack-values.yaml and the download-dashboards init container. The new one lives as a ConfigMap with sidecar label. Restarted Grafana, confirmed only one DCGM dashboard remains.
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
- Longhorn native S3 backup target — minimal config
- Encryption at rest via Longhorn (AES-256)
- Incremental backups supported
- Con: requires creating R2 bucket, generating API keys, configuring secret

**Option B: NFS to local NAS**
- Cost: $0 (existing Synology NAS)
- Simple NFS backup target
- Con: NAS is in same room — no disaster recovery
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
- Longhorn handles both natively — no extra tooling
- Schedule: daily incremental, weekly full, 30-day retention

### Action Items
- [ ] Create R2 bucket "homelab-longhorn-backup"
- [ ] Generate R2 API credentials with write access
- [ ] Create K8s secret with S3 endpoint, key, secret
- [ ] Configure Longhorn backup target via UI or settings
- [ ] Test backup + restore cycle on a non-critical volume
- [ ] Set up recurring backup schedule
- [ ] Configure NFS backup target as secondary'

# ── Main execution ─────────────────────────────────────────────────

echo ""
echo "  ╔══════════════════════════════════════════════════════════════╗"
echo "  ║           SUMMARIZATION SHOWDOWN                           ║"
echo "  ║   Mistral vs Qwen(/no_think) vs Qwen(think) vs Haiku     ║"
echo "  ╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "  Output dir:  $OUTPUT_DIR"
echo "  Local URL:   $LOCAL_MODEL_URL"
echo ""

# Helper to get correct system prompt for a config
get_sys_prompt() {
    local config="$1" round="$2"
    local var_nt="SYS_NT_${round}"
    local var_th="SYS_TH_${round}"
    if [[ "$config" == "C" ]]; then
        echo "${!var_th}"
    else
        echo "${!var_nt}"
    fi
}

get_max_tokens() {
    local config="$1"
    if [[ "$config" == "C" ]]; then
        echo "8192"
    else
        echo "1024"
    fi
}

get_input() {
    local round="$1"
    local var="INPUT_${round}"
    echo "${!var}"
}

# ── Run configs that need Mistral loaded (A) ───────────────────────

NEED_MISTRAL=false
NEED_QWEN=false
NEED_CLAUDE=false

should_run config A "$ONLY_CONFIGS" && NEED_MISTRAL=true
should_run config B "$ONLY_CONFIGS" && NEED_QWEN=true
should_run config C "$ONLY_CONFIGS" && NEED_QWEN=true
should_run config D "$ONLY_CONFIGS" && NEED_CLAUDE=true

if $NEED_MISTRAL; then
    echo "$DIVIDER"
    echo "  CONFIG A: Mistral-Small-24B (/no_think)"
    echo "$DIVIDER"
    echo ""
    echo "  Ensure Mistral-Small-24B is loaded on llamacpp-rocm."
    echo "  Current model: $(detect_model 2>/dev/null || echo 'unreachable')"
    echo ""
    read -rp "  Press ENTER when Mistral is ready (or 's' to skip): " ans
    if [[ "$ans" != "s" ]]; then
        wait_for_model "Mistral"
        info "Model: $(detect_model)"
        echo ""
        for r in 1 2 3 4 5; do
            should_run round "$r" "$ONLY_ROUND" || continue
            run_test "A" "$r" "$(get_sys_prompt A "$r")" "$(get_input "$r")" "$(get_max_tokens A)"
        done
    fi
    echo ""
fi

if $NEED_QWEN; then
    echo "$DIVIDER"
    echo "  CONFIGS B & C: Qwen3.5-27B"
    echo "$DIVIDER"
    echo ""
    echo "  Ensure Qwen3.5-27B is loaded on llamacpp-rocm."
    echo "  Current model: $(detect_model 2>/dev/null || echo 'unreachable')"
    echo ""
    read -rp "  Press ENTER when Qwen is ready (or 's' to skip): " ans
    if [[ "$ans" != "s" ]]; then
        wait_for_model "Qwen"
        info "Model: $(detect_model)"
        echo ""

        if should_run config B "$ONLY_CONFIGS"; then
            info "── Config B: Qwen + /no_think ──"
            for r in 1 2 3 4 5; do
                should_run round "$r" "$ONLY_ROUND" || continue
                run_test "B" "$r" "$(get_sys_prompt B "$r")" "$(get_input "$r")" "$(get_max_tokens B)"
            done
            echo ""
        fi

        if should_run config C "$ONLY_CONFIGS"; then
            info "── Config C: Qwen + thinking ──"
            for r in 1 2 3 4 5; do
                should_run round "$r" "$ONLY_ROUND" || continue
                run_test "C" "$r" "$(get_sys_prompt C "$r")" "$(get_input "$r")" "$(get_max_tokens C)"
            done
            echo ""
        fi
    fi
fi

if $NEED_CLAUDE; then
    echo "$DIVIDER"
    echo "  CONFIG D: Claude Haiku 4.5"
    echo "$DIVIDER"
    echo ""
    for r in 1 2 3 4 5; do
        should_run round "$r" "$ONLY_ROUND" || continue
        run_test "D" "$r" "$(get_sys_prompt D "$r")" "$(get_input "$r")" 1024
    done
    echo ""
fi

# ── Summary ────────────────────────────────────────────────────────

echo "$DIVIDER"
echo "  SHOWDOWN COMPLETE"
echo "$DIVIDER"
echo ""
echo "  Results: $OUTPUT_DIR/"
echo "  JSONL:   $RESULTS_JSON"
echo ""

if [[ -s "$RESULTS_JSON" ]]; then
    echo "  Config  Round  Time   Tokens"
    echo "  ------  -----  ----   ------"
    while IFS= read -r line; do
        cfg=$(printf '%s' "$line" | jq -r '.config')
        rnd=$(printf '%s' "$line" | jq -r '.round')
        sec=$(printf '%s' "$line" | jq -r '.elapsed_s')
        tok=$(printf '%s' "$line" | jq -r '.completion_tokens')
        printf "  %-8s %-6s %3ss   %s\n" "$cfg" "R$rnd" "$sec" "$tok"
    done < "$RESULTS_JSON"
fi

echo ""
echo "  Next: review the .md files in $OUTPUT_DIR/ and score quality."
echo "  Tip: diff R1_A.md R1_B.md to compare side-by-side."
echo ""
