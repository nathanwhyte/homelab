# llama

LLM serving infrastructure in the `llama` namespace.

## Active services

### Ollama (timmy — AMD RX 9070 XT)

- **Models**: gemma4:vlm (gemma4:12b-it-qat Q4_0, 8.2 GB, num_ctx 65536 — OpenViking's summarizer and the **sole resident model since 2026-10-13**, IMPR-1303). qwen2.5-coder:fim (2.7 GB, num_ctx 8192) stays built by ollama-warm.sh as the rollback tag of the retired IDEA-1105 editor/summarizer pair, which never co-resided in steady state (the daemon evicted the FIM half at every restart once gemma's runner was budgeted); editor FIM clients must not target this host, since any other tag evicts gemma. Before that: deepseek-coder-v2:fim (16B-lite MoE Q4, ~13 GB) was the single edit-prediction model from the 2026-07-24 cutover, restored 2026-09-04 after the 2026-08-30→09-04 agentpair pair was rolled back, then qwen2.5-coder:fim beside gemma4:vlm from 2026-09-18. The `agentpair:*` tags (fim, agent, agent-gemma4-e4b, agent-gemma4-12b) were retired on 2026-10-06: ollama-warm.sh no longer builds them, their Modelfiles are deleted, and the tags are removed from the host store. zeta2.1 + qwen2.5-coder:fim retired from the warm list (BUG-1037: open-weights Zeta degenerates below ~3k prompt tokens vs Zed's hardcoded ~500-token self-hosted excerpts; tags kept on the PVC for rollback). gemma4:12b-it-qat orphaned (Hermes/mem0 retired, no longer warmed); OV's `gemma4:31b-cloud` VLM tag routes through this pod but occupies no local slot
- **Runs on the host, not in a pod** (IMPR-1075, 2026-09): `ollama.service` on timmy with the drop-in in `llama/host/`; the cluster reaches it through a selector-less Service + EndpointSlice (`llama/ollama-service.yaml`) and runs model jobs against it as CronJobs (`llama/ollama-jobs.yaml`). Runbook and day-to-day: `llama/host/README.md`
- **Endpoint**: `http://ollama.llama.svc:11434` (selector-less ClusterIP → host) or `192.168.1.19:11434` / `100.95.215.105:11434` (host socket, LAN / Tailscale)
- **Backend**: Vulkan on the discrete RX 9070 XT (`OLLAMA_VULKAN=1`, `OLLAMA_LLM_LIBRARY=vulkan`, `GGML_VK_VISIBLE_DEVICES=0` — RADV enumerates in PCI order, so the discrete card is device 0 and the Raphael iGPU is device 1; BUG-1094)
- **Config**: `OLLAMA_MAX_LOADED_MODELS=1` (one pinned runner, no co-load — dropped from 2 at the 2026-07-24 cutover, raised to 2 for the agentpair pair, back to 1 on 2026-09-04, 2 again for the IDEA-1105 pair 2026-09-18, back to 1 on 2026-10-13 when the pair was retired), `OLLAMA_NUM_PARALLEL=4` (slots are per runner: gemma4:vlm 4 × 65536 since 2026-10-13, IMPR-1303, ~3.1 GiB KV measured from the runner; each request admits a prompt up to 65535 tokens where 32768 cut OpenViking's 33-35K-token overview prompts to half the slot. Both directions were tried the same day: 2 slots halved completions per minute and stalled the Semantic queue, and 8 slots gained nothing because the card is decode-bound at ~100 t/s for this model, so per-call speed halved from 25.6 to 12.7 t/s while completions per minute stayed flat. The tag also bakes `num_batch 2048` for ~4x fewer prefill passes; it overflowed VRAM at 8 slots and fits at 4), `OLLAMA_CONTEXT_LENGTH=131072` (TASK-1188/IMPR-1114; KV allocated on demand), `OLLAMA_KV_CACHE_TYPE=q8_0` (bumped from `q4_0` — 4.2 GB headroom at 2 slots covers it), `OLLAMA_KEEP_ALIVE=-1` (pinned). All of it lives in `llama/host/ollama.service.d/homelab.conf`; `CAP_PERFMON` is no longer needed — the host daemon reads VRAM directly
- **Deploy**: `sudo llama/host/install-host-ollama.sh` on timmy (binary pinned by `OLLAMA_VERSION` in that script) + `kubectl apply -f llama/ollama-service.yaml -f llama/ollama-jobs.yaml`

### Ollama Auth Proxy

- nginx Bearer-token auth proxy for Ollama (`Authorization: Bearer <...>`)
- No nodeSelector — not pinned to any specific node
- **Purpose**: nginx Bearer-token auth (`Authorization: Bearer <ollama-api-key>`) for external access
- **Endpoint**: `http://ollama-auth-proxy.llama.svc:80`
- **Deploy**: `llama/ollama-auth-proxy.yaml`

### Chat Ollama Proxy

- **Purpose**: Reasoning-suppressing shim (`INJECT_REASONING_NONE=true`) routing to `ollama.llama.svc:11434`. **⚠️ Load-bearing — do NOT delete:** originally a Hermes shim, but since Hermes was retired (2026-07-18) this pod is **OpenViking's VLM route** — the `chat-ollama` Service selects `app=chat-ollama-proxy`, and `openviking-standalone-configmap.yaml`'s `vlm.api_base` is `http://chat-ollama.llama.svc.cluster.local:11434/v1` (`gemma4:vlm`, the local tag on timmy's host daemon since homelab #131, 2026-09-18; the shim's `reasoning_effort: none` injection is load-bearing for it — see the header comment in `llama/chat-ollama-proxy.yaml`).
- **Endpoint**: `http://chat-ollama-proxy.llama.svc:11434` (also reached as `chat-ollama.llama.svc` — same pod)
- **Deploy**: `llama/chat-ollama-proxy.yaml`

### Cloud LLM Counter

- **Purpose**: Daily cloud API call usage tracker (`DAILY_CLOUD_CALL_BUDGET=1000000` — effectively uncapped since 2026-07-06; advisory metrics only, nothing enforces it in the request path). Fed by `chat-ollama-proxy`'s `METRICS_PUSH_URL` — its live metrics sink, not a Hermes leftover.
- **Endpoint**: `http://cloud-llm-counter.llama.svc:80`
- **Deploy**: `llama/cloud-llm-counter.yaml`

## Retired services

### ROCm llamacpp (timmy — AMD RX 9070 XT)

**Permanently retired** (IDEA-009 Phase 4, 2026-06-06). Manifest retained for rollback only. The VLM now runs exclusively on manu's GTX 1080 via `llamacpp-cuda-ov` in the `viking` namespace.

## Benchmarking

`llama/tools/ollama-concurrency-benchmark.py` measures how `gemma4:12b-it-qat` behaves under 1..N concurrent client requests. Use it to validate `OLLAMA_NUM_PARALLEL` sizing for Hermes sub-agents and mem0 extraction sharing the same GPU.

```bash
# Basic sweep 1..6 against the current Ollama config
uv run --with aiohttp python llama/tools/ollama-concurrency-benchmark.py \
  --max-concurrency 6 --output-json results.json --output-csv results.csv

# Mixed workload: one mem0-style long extraction prompt plus sub-agent prompts
uv run --with aiohttp python llama/tools/ollama-concurrency-benchmark.py \
  --max-concurrency 6 --mixed --output-json results-mixed.json

# Run inside the cluster from a throwaway pod
kubectl run -it --rm bench --image=python:3.12-slim --restart=Never -- \
  sh -c "pip install aiohttp && curl -sSLO https://raw.githubusercontent.com/nathanwhyte/homelab/main/llama/tools/ollama-concurrency-benchmark.py && python ollama-concurrency-benchmark.py --url http://ollama.llama.svc:11434"
```

To compare multiple `NUM_PARALLEL` values (e.g., 2, 4, 6), use the sweep wrapper. It patches the Ollama Deployment, waits for rollout, runs the benchmark, and restores the original value:

```bash
NP_VALUES="2 4 6" BENCHMARK_ARGS="--max-concurrency 6 --mixed" llama/tools/sweep-num-parallel.sh
```

Results match the methodology in INFO-047 and feed into INFO-055.

## Quick test

```bash
kubectl run -it --rm curl --image=curlimages/curl:8.12.1 --restart=Never -- \
  curl -sS http://ollama.llama.svc:11434/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"gemma4:12b-it-qat","messages":[{"role":"user","content":"Write one sentence about homelabs."}],"max_tokens":64}'
```
