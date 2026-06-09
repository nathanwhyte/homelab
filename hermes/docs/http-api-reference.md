# Hermes HTTP API reference

**Live-verified** against `hermes-agent` running in the `hermes` namespace on 2026-06-09. Runtime: `version: 0.16.0`, `release_date: 2026.6.5`.

This document covers the two HTTP surfaces the Hermes container exposes inside the pod: the **API server** (port 8642, OpenAI-compatible chat completions) and the **Dashboard** (port 9119, web UI + REST API). The Hermes gateway itself (`gateway run`) is not HTTP-addressable; that path uses SSH to `hermes-jump` via `hermes/operator.sh`.

---

## Service endpoints (cluster-internal)

| Service | ClusterIP | Port | Purpose |
|---|---|---|---|
| `hermes-agent.hermes.svc.cluster.local` | (DNS) | `8642/TCP` | API server (OpenAI-compatible) |
| `hermes-agent.hermes.svc.cluster.local` | (DNS) | `9119/TCP` | Dashboard web UI + REST API |
| `hermes-jump.hermes.svc.cluster.local` | (DNS) | `22/TCP` | SSH jump (terminal backend — not HTTP) |

Both ports are exposed on the same `hermes-agent` Service. There is no separate Service per port. Probe them from inside the cluster:

```bash
curl http://hermes-agent.hermes.svc.cluster.local:8642/health
```

From a workstation, port-forward first:

```bash
kubectl -n hermes port-forward svc/hermes-agent 8642:8642 9119:9119
```

---

## API server (port 8642)

An OpenAI `chat.completions`-compatible endpoint. The server is a **stateless proxy** to the configured backend (`glm-5.1:cloud` via the `chat-ollama` shim at `http://chat-ollama.llama.svc:11434/v1`). It does not run the agent tool loop; tools in the request are accepted but not executed by the model.

### Authentication

All endpoints except `GET /health` require a Bearer token:

```bash
HERMES_API_KEY=$(kubectl get secret hermes-api-server-key -n hermes \
  -o jsonpath='{.data.api-key}' | base64 --decode)
```

- Missing or wrong key → `401 {"error": {"message": "Invalid API key", "type": "invalid_request_error", "code": "invalid_api_key"}}` (verified).
- Key rotates via `kubectl create secret` (see `hermes/README.md` "Rollback / disable").

### `GET /health`

No auth.

```bash
curl -s http://hermes-agent.hermes.svc.cluster.local:8642/health
```

Response (verified 2026-06-09):

```json
{"status": "ok", "platform": "hermes-agent", "version": "0.16.0"}
```

### `GET /v1/models`

Bearer auth.

```bash
curl -s -H "Authorization: Bearer $HERMES_API_KEY" \
  http://hermes-agent.hermes.svc.cluster.local:8642/v1/models
```

Response (verified):

```json
{
  "object": "list",
  "data": [{
    "id": "glm-5.1:cloud",
    "object": "model",
    "created": 1781042757,
    "owned_by": "hermes",
    "permission": [],
    "root": "glm-5.1:cloud",
    "parent": null
  }]
}
```

The server advertises one model but **accepts any model name** in `/v1/chat/completions` and routes every request to the configured backend regardless. The `model` field in the response echoes whatever the caller sent.

### `POST /v1/chat/completions`

Bearer auth. Full OpenAI chat-completions shape.

```bash
curl -s -X POST -H "Authorization: Bearer $HERMES_API_KEY" \
  -H "Content-Type: application/json" \
  http://hermes-agent.hermes.svc.cluster.local:8642/v1/chat/completions \
  -d '{
    "model": "glm-5.1:cloud",
    "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
    "max_tokens": 20,
    "temperature": 0
  }'
```

Verified non-streaming response:

```json
{
  "id": "chatcmpl-9d86123cadd5403c95831bf1a50b7",
  "object": "chat.completion",
  "created": 1781042758,
  "model": "glm-5.1:cloud",
  "choices": [{
    "index": 0,
    "message": {"role": "assistant", "content": "ok"},
    "finish_reason": "stop"
  }],
  "usage": {"prompt_tokens": 18015, "completion_tokens": 13, "total_tokens": 18028}
}
```

#### Streaming (`"stream": true`)

Returns Server-Sent Events per the OpenAI spec. Each chunk is one `data: {...}\n\n`, ending with `data: [DONE]`. The final chunk carries the `usage` block.

#### Supported parameters

| Parameter | Supported | Notes |
|---|---|---|
| `model` | yes | Any name accepted; always routes to the configured backend |
| `messages` | yes | Full OpenAI shape (system / user / assistant) |
| `max_tokens` | yes | |
| `temperature` | yes | |
| `top_p` | yes | |
| `stream` | yes | SSE per OpenAI spec |
| `tools` | accepted | Not executed — passed through; the underlying model may hallucinate tool calls in prose (verified: model replied with "echo said hi" when given a `tools=[{echo}]` payload) |
| `tool_choice` | accepted | Same as `tools` — no execution |

#### Things the API server does NOT do

- No `/v1/completions` (legacy text completions)
- No `/v1/embeddings`
- No agent tool loop. Use the dashboard or `hermes/operator.sh run` for the full Hermes experience
- No session persistence. The API server is a stateless proxy; sessions created via `/v1/chat/completions` are stored in the gateway's session DB (visible via `/api/sessions` on the dashboard) but the API server itself is stateless

---

## Dashboard (port 9119)

Web UI on `/`, REST API under `/api/`. The dashboard is exposed externally via Cloudflare tunnel at `https://hermes.nathanwhyte.dev` (token auth at the tunnel).

### Authentication

> ⚠️ **Currently insecure.** `HERMES_DASHBOARD_INSECURE=1` is set in the manifest. `/api/status` reports `auth_required: false` and `auth_providers: []`. The `HERMES_DASHBOARD_SESSION_TOKEN` env var is wired in (from the `hermes-dashboard-token` Secret) but is **not enforced** while INSECURE=1 is set.
>
> For cluster-internal callers, this is fine: the dashboard is reachable only via `kubectl port-forward` or the Cloudflare tunnel (which has its own auth). For direct cluster exposure (e.g. an Ingress), **unset INSECURE=1 first** so the token is checked.

Verified `GET /api/status` response (2026-06-09):

```json
{
  "version": "0.16.0",
  "release_date": "2026.6.5",
  "hermes_home": "/opt/data",
  "config_path": "/opt/data/config.yaml",
  "env_path": "/opt/data/.env",
  "config_version": 28,
  "latest_config_version": 28,
  "gateway_running": true,
  "gateway_pid": 143,
  "gateway_state": "running",
  "gateway_platforms": {
    "api_server": {"state": "connected", "error_message": null,
                   "updated_at": "2026-06-09T18:36:36.835206+00:00"}
  },
  "active_sessions": 3,
  "auth_required": false,
  "auth_providers": []
}
```

The session token is retrieved via:

```bash
HERMES_DASH_TOKEN=$(kubectl get secret hermes-dashboard-token -n hermes \
  -o jsonpath='{.data.token}' | base64 --decode)
```

It currently has no functional effect on `/api/*` while INSECURE=1 is set; it is the auth secret for the Cloudflare tunnel on the external side.

### Endpoints

| Endpoint | Method | Auth | Notes |
|---|---|---|---|
| `/` | GET | No | Web SPA (served from the gateway) |
| `/api/health` | GET | No | Returns 401 even with INSECURE=1 (gateway quirk — use `/health` on the API server instead) |
| `/api/status` | GET | No (INSECURE=1) | Gateway status, version, platform health |
| `/api/config` | GET | No (INSECURE=1) | Full Hermes config (model, providers, toolsets, etc.) — operationally sensitive |
| `/api/sessions` | GET | No (INSECURE=1) | List sessions with IDs, sources, models, token usage, timestamps |

Examples:

```bash
# Status
curl -s -H "Authorization: Bearer $HERMES_DASH_TOKEN" \
  http://hermes-agent.hermes.svc.cluster.local:9119/api/status

# Sessions (large — paginate or jq-select)
curl -s -H "Authorization: Bearer $HERMES_DASH_TOKEN" \
  http://hermes-agent.hermes.svc.cluster.local:9119/api/sessions | jq '.sessions[] | {id, source, model, created_at}'
```

---

## Configuration reference

These env vars on `hermes-agent` container control HTTP behavior. Source: `hermes/hermes-deployment.yaml` (live cluster state on 2026-06-09).

| Env var | Live value | Effect |
|---|---|---|
| `API_SERVER_ENABLED` | `true` | Enable the API server process |
| `API_SERVER_HOST` | `0.0.0.0` | Bind address |
| `API_SERVER_PORT` | `8642` | Listen port |
| `API_SERVER_MODEL_NAME` | `glm-5.1:cloud` | The model ID advertised by `/v1/models` and echoed in responses |
| `API_SERVER_KEY` | *(from `hermes-api-server-key` Secret)* | Bearer token for `/v1/*` |
| `HERMES_DASHBOARD` | `1` | Enable the dashboard |
| `HERMES_DASHBOARD_HOST` | `0.0.0.0` | Bind address |
| `HERMES_DASHBOARD_PORT` | `9119` | Listen port |
| `HERMES_DASHBOARD_INSECURE` | `1` | Skip dashboard token check (current posture) |
| `HERMES_DASHBOARD_SESSION_TOKEN` | *(from `hermes-dashboard-token` Secret)* | Token for Cloudflare tunnel (unused on `/api/*` while INSECURE=1) |

The actual LLM backend and model list are configured separately in the `hermes-config` ConfigMap (`model.base_url`, `providers.ollama-launch.models`, etc.), not via API server env vars.

---

## Integration examples

### OpenAI Python SDK

```python
from openai import OpenAI

client = OpenAI(
    api_key="<API_KEY>",
    base_url="http://hermes-agent.hermes.svc.cluster.local:8642/v1",
)

resp = client.chat.completions.create(
    model="glm-5.1:cloud",
    messages=[{"role": "user", "content": "Hello!"}],
    max_tokens=100,
)
print(resp.choices[0].message.content)
```

For cluster-external use, port-forward first or terminate at the Cloudflare tunnel (currently exposes the dashboard only, not the API server).

### curl from inside a pod

```bash
API_KEY=$(kubectl get secret hermes-api-server-key -n hermes \
  -o jsonpath='{.data.api-key}' | base64 -d)

kubectl run curl --rm -it --restart=Never --image=curlimages/curl -- \
  curl -s -X POST \
    -H "Authorization: Bearer $API_KEY" \
    -H "Content-Type: application/json" \
    http://hermes-agent.hermes.svc.cluster.local:8642/v1/chat/completions \
    -d '{"model":"glm-5.1:cloud","messages":[{"role":"user","content":"hi"}]}'
```

### SSE streaming from a shell

```bash
curl -N -X POST \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  http://hermes-agent.hermes.svc.cluster.local:8642/v1/chat/completions \
  -d '{"model":"glm-5.1:cloud","messages":[{"role":"user","content":"Count to 5"}],"max_tokens":50,"stream":true}'
```

---

## Discrepancies from earlier references (resolved)

| Earlier statement | Reality | Source |
|---|---|---|
| `Hermes Agent v2026.6.5` image tag | Image is `nousresearch/hermes-agent:latest` (now; was `nousresearch/hermes-agent`); runtime version is `0.16.0` / `release_date 2026.6.5` | Live `/health` + `/api/status` |
| "Dashboard is cluster-internal only" | Dashboard is cluster-internal **and** externally exposed at `hermes.nathanwhyte.dev` via Cloudflare tunnel. API server is cluster-internal only | `hermes-ingress.yaml` + Cloudflare config |
| `HERMES_DASHBOARD_INSECURE=1` with no other auth | Correct — `/api/status` confirms `auth_required: false`; the session token is unused for `/api/*` while INSECURE=1 is set | Live `/api/status` |
| Stateless proxy, no tool execution | Confirmed by live test: passing `tools=[{echo}]` produced a text response ("echo said hi") with no tool_call in the JSON | Live test 2026-06-09 |

---

## Related

- `hermes/README.md` — operator-focused quick start, including `hermes/operator.sh` wrappers
- `hermes/hermes-deployment.yaml` — live source of truth for env vars and probes
- `hermes/hermes-configmap.yaml` — model/provider/toolsets config
- `hermes/CLAUDE.md` and root `CLAUDE.md` — service routing table and cross-service context
