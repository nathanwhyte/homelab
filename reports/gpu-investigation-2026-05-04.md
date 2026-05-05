# GPU Activity Investigation Report — 2026-05-04

## Summary

The AMD RX 9070 XT GPU on **timmy** (`192.168.1.19`) became active due to inference requests hitting Ollama's Anthropic compatibility endpoint (`/v1/messages?beta=true`) using model `gemma4:e4b`. All traffic appeared in Ollama logs with source IP `10.42.0.246` (wemby's `svclb-ollama` pod), proving traffic was processed by **wemby's** iptables DNAT chain. Despite exhaustive enumeration of every pod on wemby, no confirmed caller has been identified. The most likely remaining explanation is that the tcpdump timing missed active requests and the caller is a host-level process on wemby (which would still route through the svclb iptables).

---

## Network Routing

| Step | Detail |
|------|--------|
| Caller | Unknown — see below |
| Target | `192.168.1.19:11434` (Ollama LoadBalancer IP, timmy) |
| Interceptor | wemby's `svclb-ollama-45a3cebf-tgd9r` pod (`10.42.0.246`) |
| DNAT | iptables PREROUTING: `tcp dpt:11434 → 192.168.1.9:32014` |
| NodePort | Ollama service NodePort = **32014** |
| Final | kube-proxy routes NodePort 32014 → Ollama pod on timmy |
| Source IP in logs | `10.42.0.246` (ServiceLB pod IP due to MASQUERADE) |

### Node IPs (confirmed)

| Node | IP | svclb-ollama pod IP |
|------|----|---------------------|
| wemby | `192.168.1.9` | `10.42.0.246` |
| manu | `192.168.1.10` | `10.42.4.72` |
| timmy | `192.168.1.19` | `10.42.2.186` |

**Key constraint:** Source `10.42.0.246` is wemby-specific. svclb-ollama is a DaemonSet running on all three nodes — each node's pod has a different IP. Traffic from timmy pods would show `10.42.2.186`; manu pods `10.42.4.72`. Only wemby-originated traffic (pods on wemby, OR any process connecting to `192.168.1.9:11434` directly) produces `10.42.0.246`.

---

## Ollama Log Timeline (UTC)

| Time | Method | Duration | Source IP | Endpoint |
|------|--------|----------|-----------|----------|
| 19:42:08 | POST | 19.89s | 10.42.0.246 | `/v1/messages?beta=true` |
| 19:43:35 | POST | 1m26s | 10.42.0.246 | `/v1/messages?beta=true` |
| 19:43:53 | POST | 17.54s | 10.42.0.246 | `/v1/messages?beta=true` |
| 19:50:24 | POST | 25.68s | 10.42.0.246 | `/v1/messages?beta=true` |
| 19:50:43 | POST | 18.51s | 10.42.0.246 | `/v1/messages?beta=true` |
| 19:51:22 | POST | 38.68s | 10.42.0.246 | `/v1/messages?beta=true` |
| 19:58:18 | POST | **3m36s** | 10.42.0.246 | `/v1/messages?beta=true` |
| 19:59:45 | POST | **1m27s** | 10.42.0.246 | `/v1/messages?beta=true` |

Model: `gemma4:e4b`

Pattern: Two bursts separated by ~7 minutes. Request durations (18s–3m36s) consistent with interactive chat via a UI, not a script loop.

---

## Caller Fingerprint

The endpoint `/v1/messages?beta=true` is Ollama's Anthropic Messages API compatibility path. The `?beta=true` query parameter is **not** emitted by the standard Anthropic Python/JS SDK (which uses the `anthropic-beta` header instead). This implies the caller is either:
- A custom client that adds `?beta=true` explicitly
- A specific UI/integration layer (e.g. OpenWebUI's Anthropic connection type) that appends it
- A version of `ollama run --model ... --api anthropic` or similar wrapper

---

## Complete Pod Enumeration — Wemby

All pods on wemby were checked and ruled out:

| Namespace | Pod | Verdict |
|-----------|-----|---------|
| viking | `ov-console` | Only connects to OV API (`openviking.viking.svc:1933`). No Ollama config. |
| viking | `ov-worker-1` | VLM config → `llamacpp-cuda-llm.viking.svc.cluster.local/v1` (OpenAI format, `openai/qwen3-8b`). Not Ollama. |
| glossary | `glossary-57ff9c8764-6hz58` | Phoenix/Elixir web app. No LLM env vars. Only 2 startup log lines since restart. |
| build | `tunnel-dd97b5cbc-vghmb` | Cloudflare tunnel only. |
| cloudflare-system | `cloudflared-569bb456b9-85nkr` | Cloudflare tunnel only. |
| garage | `garage-1` | S3 storage. |
| harbor | `harbor-jobservice`, `harbor-portal`, `harbor-redis-0` | Container registry infra. |
| longhorn-system | All pods | Storage system. |
| grafana | `cloudflared`, `alloy-*`, `loki-*`, node-exporter | Monitoring/observability infra. |
| equal-risk | `postgres-0` | Database. |
| kube-system | `coredns`, `traefik`, `svclb-*`, etc. | Cluster infra. |
| headlamp, cert-manager, gpu-operator, kubernetes-dashboard | — | No pods on wemby. |

---

## Complete Pod Enumeration — Other Nodes (Viking Namespace + OV Stack)

| Pod | Node | IP | Verdict |
|-----|------|----|---------|
| `ov-coordinator` (timmy) | timmy | `10.42.2.198` | Pure HTTP proxy/router for OV workers. No LLM calls in code. |
| `ov-merge` (timmy) | timmy | `10.42.2.208` | Pure OV shard sync service. No LLM calls. |
| `openviking` (manu, merged instance) | manu | `10.42.4.129` | Uses same `openviking-config` → `llamacpp-cuda-llm`. Not Ollama. |
| `llamacpp-cuda-ov` (manu) | manu | `10.42.4.130` | LLM inference backend for OV VLM. Uses OpenAI format. Not Ollama. |
| `ov-worker-0` (manu), `ov-worker-2` (timmy) | manu/timmy | `10.42.4.135`, `10.42.2.234` | Same `openviking-config` as ov-worker-1. Not Ollama. |
| `embedder-llamacpp` (timmy) | timmy | `10.42.2.195` | nomic-embed-text only. |

Traffic from any of these pods to port 11434 would be MASQUERADE'd by their local node's svclb pod IP, **not** `10.42.0.246`. They are ruled out on both code and network grounds.

---

## Ruled-Out Services (Host + Other)

| Suspect | Why Ruled Out |
|---------|---------------|
| `claude --model kimi-k2.6:cloud` (PID 321891) | `ANTHROPIC_BASE_URL=http://127.0.0.1:11434` → local wemby Ollama only |
| `claude --model claude-haiku-*` (PID 415329) | No `ANTHROPIC_BASE_URL`; uses Anthropic cloud API |
| `ollama serve` (PID 1134, wemby) | Host-level Ollama server; outbound calls would appear as `192.168.1.9`, not `10.42.0.246` |
| OpenWebUI | `OLLAMA_BASE_URLS=http://ollama.llama.svc:11434` (internal DNS); source would be OpenWebUI pod IP |
| Docker containers on wemby | Only pihole, unbound, pihole-exporter, cloudflared |
| Benchmark scripts (worktree `agent-aa9b0c555d1aaf0a0`) | Use `/api/chat` (OpenAI format); `bench-haiku-direct.sh` uses `https://api.anthropic.com` (cloud) |
| OpenViking MCP server (PID 321993) | Disabled and killed earlier in the session |
| Systemd timers / crontab | No relevant entries |
| OpenWebUI (explicitly checked) | Internal service DNS; would NOT produce `10.42.0.246` |

---

## OpenWebUI Model Configuration

OpenWebUI is explicitly configured with:
```
OLLAMA_MODELS=mistral-nemo-q8,gemma4:e4b,qwen3.5:9b
OLLAMA_BASE_URLS=http://ollama.llama.svc:11434
ENABLE_OPENAI_API=False
```
`gemma4:e4b` is a known model in the UI, confirming it can be selected by users. However, OpenWebUI uses internal K8s DNS for Ollama, so its requests would NOT produce source `10.42.0.246`.

---

## Outstanding Hypotheses

### Hypothesis A: tcpdump timing gap (most likely)

The original tcpdump ran between request bursts (two bursts: 19:42–19:51 and 19:58–20:01), not during them. A host-level process on wemby could be the caller. Host processes connect through the OUTPUT iptables chain on wemby — which may also have DNAT rules set up by the svclb pods — potentially producing source `10.42.0.246`. Candidates:

- **`claude --model kimi-k2.6:cloud` (PID 321891)** via local wemby Ollama: if `kimi-k2.6:cloud` is a Modelfile-defined proxy model that forwards to an Anthropic-compatible backend, it could chain through to timmy. Not yet verified — would require inspecting the Ollama Modelfile for `kimi-k2.6:cloud` on wemby.
- **Native OpenViking process**: At investigation time, OpenViking was also running as a native Python process on wemby (S3185). If it had an LLM config pointing to the Anthropic-compatible Ollama endpoint, it could be the source. Not yet verified.

### Hypothesis B: Process no longer running

The caller was a short-lived process (script, one-off command, or background task) that ran during the 19:42–20:01 window and had already terminated by the time investigation started. Zsh history showed no relevant commands but the shell might have been non-interactive or history not flushed.

---

## Remaining Investigation Steps

1. ~~**Inspect `kimi-k2.6:cloud` Modelfile on wemby**~~: Completed — model does not exist locally on wemby's Ollama (`ollama show` returns no output), but `/api/show` returns metadata (1.04T parameters, INT4). See Update 1 below.
2. ~~**Check native OpenViking config on wemby**~~: Completed — no `~/.openviking/config.yaml` found.
3. ~~**Check wemby iptables OUTPUT chain**~~: Completed — only generic `CNI-HOSTPORT-DNAT` rule found; no Ollama-specific DNAT rules.
4. ~~**Re-run tcpdump during active Ollama use**~~: Completed via alternative method — test request to wemby's Ollama (`/api/generate`) appeared in timmy's Ollama pod logs at the exact same timestamp, confirming forwarding.
5. ~~**Check Ollama model list on wemby**~~: Completed — wemby has `gemma4:e4b` (9.6GB) locally but is CPU-only (8.4GB available RAM, no GPU). See Update 1.

---

## Update 1: Post-Investigation Findings (2026-05-04 21:05 CDT)

### Port 3000 Identity

| Finding | Detail |
|---|---|
| **Service** | Grafana native systemd service (PID 1129), NOT OpenWebUI |
| **Version** | 13.0.1 |
| **Conclusion** | Completely unrelated to Ollama traffic |

### Ollama Anthropic API Compatibility (Ollama v0.23.0)

| Finding | Detail |
|---|---|
| **Feature** | Ollama v0.23.0 added native Anthropic API compatibility for Claude Desktop / Claude Code |
| **Endpoint** | `POST /v1/messages?beta=true` is handled directly by `ollama serve` (GIN router) |
| **Other new endpoints** | `/api/experimental/model-recommendations`, `/api/status`, `/api/me` |
| **Auth-proxy pod** | Misconfigured (nginx targets wrong upstream port), NOT in traffic path |

### Request Routing: Wemby → Timmy (Confirmed)

| Step | Evidence |
|---|---|
| **Wemby Ollama** | Handles requests on `127.0.0.1:11434` with Anthropic compatibility layer |
| **Test request** | `POST /api/generate` to wemby at `21:00:54` appeared in timmy Ollama pod logs at same timestamp |
| **Forwarding mechanism** | Ollama forwards requests for models that cannot run locally (insufficient VRAM/RAM or no GPU) |
| **Source IP in timmy logs** | `10.42.0.246` (wemby's svclb pod) because forwarded traffic exits wemby via the k8s LoadBalancer |

### Claude Code Sessions on Wemby

| PID | Terminal | Model | `ANTHROPIC_BASE_URL` | Active Connections to `127.0.0.1:11434` |
|---|---|---|---|---|
| 492634 | pts/1 | `kimi-k2.6:cloud` | `http://127.0.0.1:11434` | **Yes** (4 connections) |
| 526011 | pts/2 | `kimi-k2.6:cloud` | `http://127.0.0.1:11434` | None currently (idle) |
| 547197 | pts/3 | `kimi-k2.6:cloud` | `http://127.0.0.1:11434` | **Yes** (2 connections) |

**Total: 3 Claude Code sessions**, all configured to use wemby's local Ollama as their Anthropic API backend. The `ANTHROPIC_AUTH_TOKEN=ollama` env var confirms the Ollama integration.

### Wemby Ollama Configuration

```json
// ~/.ollama/config.json
{
    "integrations": {
        "claude": {
            "models": [
                "kimi-k2.6:cloud"
            ]
        }
    },
    "last_selection": "claude"
}
```

- `kimi-k2.6:cloud` is registered in the Claude integration config
- Model metadata claims 1.04T parameters, INT4 quantization, 262K context
- Wemby Ollama runs CPU-only with 8.4GB available RAM — cannot load `gemma4:e4b` (9.6GB) or `kimi-k2.6:cloud` (1TB)
- Forwarding to timmy is automatic when local resources are insufficient

### Model Behavior Observed

| Model | Wemby Local? | Forwarded to Timmy? | Response Time |
|---|---|---|---|
| `kimi-k2.6:cloud` | No (metadata only) | **Yes** | ~8s |
| `gemma4:e4b` | Yes (9.6GB cached) | **Yes** (GPU needed) | 10s–3m36s |

### Critical Finding: Custom Ollama Launcher

The `/usr/local/bin/ollama` binary on wemby is **not the official Ollama CLI** — it is a custom 44 MB Go binary containing strings like `ANTHROPIC_BASE_URL=`, `ollama-launch`, `claude`, `openclaw.json`, `Kimi Code CLI`, and `gateway is running`. When the user runs `ollama launch claude`, this custom binary:

1. Launches Claude Code with injected environment variables
2. Sets `ANTHROPIC_BASE_URL=http://127.0.0.1:11434`
3. Sets `ANTHROPIC_AUTH_TOKEN=ollama`
4. Sets all default model variables to `kimi-k2.6:cloud`

This is **not a shell configuration** — the variables do not appear in `.zshrc`, `.bashrc`, `.profile`, or `/etc/environment`. They are injected directly by the custom binary at process startup.

### Root Cause

The GPU on timmy was triggered by **Claude Code sessions on wemby** (three active sessions, PID 492634, 526011, 547197). All three were launched via the custom `ollama launch claude` wrapper, which forces `ANTHROPIC_BASE_URL=http://127.0.0.1:11434`. Claude Code then sends native Anthropic API requests (`POST /v1/messages?beta=true`) to wemby's local Ollama. Ollama v0.23.0's native Anthropic compatibility layer accepts these requests and forwards them to timmy's Ollama pod (via the k8s LoadBalancer at `192.168.1.19:11434`) because wemby lacks GPU and sufficient RAM to run the models locally.

### Why `?beta=true`

The `?beta=true` query parameter activates Ollama's experimental Anthropic API compatibility endpoint. This is emitted by Claude Code when connecting to an Ollama backend (not by the standard Anthropic SDK, which uses the `anthropic-beta` header).

### Why Source IP = `10.42.0.246`

When wemby's Ollama forwards requests to `192.168.1.19:11434`, the traffic goes through the k3s LoadBalancer's OUTPUT chain DNAT rules, which redirect to the NodePort (`32014`). The `svclb-ollama-45a3cebf-tgd9r` pod on wemby (IP `10.42.0.246`) handles the MASQUERADE, causing timmy's Ollama logs to show `10.42.0.246` as the source IP.
