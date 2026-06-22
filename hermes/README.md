# Hermes Agent on K3s

Hermes runs in the `hermes` namespace as two Deployments:

- `hermes-agent` — Hermes Agent runtime, currently running `hermes gateway run`
- `hermes-jump` — constrained SSH terminal backend used by Hermes for shell/tool execution

### Persistent storage

| PVC | Mount | Pod | Size | Purpose |
|---|---|---|---|---|
| `hermes-home` | `/opt/data` | `hermes-agent` | 10Gi | Agent config, sessions, kanban DB |

> **Note:** The `hermes-jump-home` PVC and the enhanced entrypoint (uv, Python, gh, git config, GITHUB_TOKEN) described in earlier versions of this README exist in the repo manifests (`hermes-jump-pvc.yaml`, `hermes-jump.yaml`) but have **not been applied** to the live cluster. The current jump pod is ephemeral — no persistent home directory, no pre-installed tooling, and no GITHUB_TOKEN mount. See `hermes-jump.yaml` and `hermes-jump-pvc.yaml` for the desired-state manifests.

To create the secret in the hermes namespace (copy from the existing build namespace secret):

```bash
kubectl get secret github-access-token -n build -o json \
  | python3 -c "
import sys, json
d = json.load(sys.stdin)
d['metadata'].update({'name':'github-access-token','namespace':'hermes','labels':{'app':'hermes-jump'}})
for k in ('uid','resourceVersion','creationTimestamp','managedFields'):
    d['metadata'].pop(k, None)
d.get('metadata',{}).pop('annotations', None)
json.dump(d, sys.stdout)" \
  | kubectl apply -f -
```

To reset the jump home (e.g. after corruption):

```bash
kubectl delete pvc hermes-jump-home -n hermes
kubectl rollout restart deployment/hermes-jump -n hermes
# The entrypoint will recreate everything on the fresh PVC
```

The agent API is **cluster-internal only**. Do not expose it through Cloudflare or any public ingress until the PROJ-006 safety review is complete.

## Operator access model

The supported operator path is:

1. `kubectl port-forward` to the internal `hermes-agent` Service.
2. Bearer-authenticated calls to the built-in Hermes API server on port `8642`.
3. `kubectl logs` / `kubectl exec` only for admin/debug actions such as logs, session listing, config inspection, rollout restart, or checking the jump backend.

This avoids adding an SSH daemon, LoadBalancer, Ingress, or Cloudflare route for operator access.

## Helper script

Use `hermes/operator.sh` from this repository:

```bash
# health check through a temporary port-forward
hermes/operator.sh health

# list API models
hermes/operator.sh models

# one-shot OpenAI-compatible chat completion
hermes/operator.sh ask "Reply exactly: ok"

# use a non-default model for a single operator command
HERMES_MODEL=glm-5.1:cloud hermes/operator.sh ask "Reply exactly: ok"

# send a large prompt from a file without hitting shell ARG_MAX
hermes/operator.sh ask-file /tmp/prompt.txt

# async run + Server-Sent Events stream
hermes/operator.sh run "Reply exactly: ok"
hermes/operator.sh run-file /tmp/prompt.txt

# foreground port-forward for manual curl/OpenAI clients
hermes/operator.sh port-forward
```

The script reads the API key from the live Kubernetes Secret `hermes-api-server-key` unless `HERMES_API_KEY` is already set.

## Manual API usage

If you do not want to use the helper script:

```bash
kubectl -n hermes port-forward svc/hermes-agent 8642:8642
```

In another shell:

```bash
HERMES_API_KEY=$(kubectl get secret hermes-api-server-key -n hermes \
  -o jsonpath='{.data.api-key}' | base64 --decode)

curl -fsS http://127.0.0.1:8642/health

curl -fsS http://127.0.0.1:8642/v1/chat/completions \
  -H "Authorization: Bearer ${HERMES_API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "glm-5.1:cloud",
    "messages": [{"role": "user", "content": "Reply exactly: ok"}],
    "stream": false
  }'
```

## HTTP API reference

A full ground-truthed reference (live-verified against the running API server) lives in [`hermes/docs/http-api-reference.md`](docs/http-api-reference.md). Highlights:

- **API server (port 8642)** — OpenAI-compatible `chat.completions` only; stateless proxy, no tool execution; advertised model `glm-5.1:cloud` but any name accepted
- **Dashboard (port 9119)** — Web UI + REST API; **cluster-internal only**; **currently insecure** (`HERMES_DASHBOARD_INSECURE=1` is set and `/api/status` reports `auth_required: false`, even though `HERMES_DASHBOARD_SESSION_TOKEN` is wired in — token is unused until the env var is removed)
- **External exposure** — API server has no Ingress; dashboard is exposed at `hermes.nathanwhyte.dev` via Cloudflare tunnel (TASK-030 pending)

## Tuned baseline

Current tuning baseline (synced 2026-06-12):

| Setting | Value | Rationale |
|---|---|---|
| Default model | `glm-5.1:cloud` | Cloud-first default for quality; local `gemma4:12b-it-qat` available as fallback. |
| Ollama endpoint | `http://chat-ollama.llama.svc:11434/v1` | Hermes uses the internal compatibility proxy. For local models, the proxy bridges OpenAI chat-completions requests to Ollama native `/api/chat` with `think: false`, maps the response back to OpenAI/SSE shape, and does not enforce the old cloud budget gate. |
| Per-call model override | `HERMES_MODEL=<model>` with `operator.sh ask/run` | Allows explicit cloud or alternate local model tests without changing the deployment default. |
| API retries | `agent.api_max_retries: 3` | Allow retries for transient cloud API failures. |
| Delegation | `max_concurrent_children: 5`, `max_spawn_depth: 2`, `child_timeout_seconds: 900` | Parallel subagent workloads on 9070 XT (tuned per PR #9 VRAM analysis). |
|| Compression | enabled, `threshold: 0.75`, `target_ratio: 0.2` | Lower threshold triggers compression more aggressively. |
| Memory | `memory_char_limit: 20000`, `user_char_limit: 12000` via mem0 provider | `memory.provider: mem0` with mem0-adapter sidecar on localhost:18080 (translates Platform API → OSS API, backed by Mem0 server in `mem0` namespace with PostgreSQL/pgvector); OpenViking knowledge-base tools (`viking_*`) remain active via `OPENVIKING_ENDPOINT` |
| Toolsets | `hermes-cli` | Minimal toolset for current smoke tests and operator workflows. |
| External exposure | Cloudflare tunnel at `hermes.nathanwhyte.dev` (dashboard); LAN Ingress also active | Dashboard exposed via Cloudflare tunnel; API server cluster-internal. |

## Direct Hermes CLI access

If you want the native Hermes CLI instead of the HTTP API, use `kubectl exec` into the running `hermes-agent` Deployment. This is the supported direct-CLI path; there is intentionally no SSH daemon in the agent pod.

```bash
# Interactive shell in the agent container
kubectl exec -it -n hermes deploy/hermes-agent -- bash

# Native Hermes chat UI from your terminal
kubectl exec -it -n hermes deploy/hermes-agent -- hermes chat

# One-shot native CLI prompt
kubectl exec -it -n hermes deploy/hermes-agent -- hermes -z "Reply exactly: ok"

# Native session/config inspection
kubectl exec -it -n hermes deploy/hermes-agent -- hermes sessions list
kubectl exec -it -n hermes deploy/hermes-agent -- hermes config show
```

This starts a separate CLI process that shares the same `/root/.hermes` PVC, config, and state as the gateway. It does not attach to the already-running gateway process/session. Use `hermes/operator.sh` for normal API-driven access and `kubectl exec ... hermes chat` when you specifically want the native CLI.

Direct SSH into `hermes-agent` is deliberately not configured. Adding it would require an SSH daemon, Service/key management, and safety review before any external route.

## Admin/debug commands

```bash
# Gateway logs
hermes/operator.sh logs
hermes/operator.sh logs --follow --tail=50

# Hermes session store
hermes/operator.sh sessions list
hermes/operator.sh sessions stats

# Runtime status and config
hermes/operator.sh status
hermes/operator.sh config

# Rollout/restart
hermes/operator.sh rollout-status
hermes/operator.sh restart
```

`hermes/operator.sh config` runs `hermes config show` inside the pod. Treat its output as operationally sensitive and do not paste secrets into committed files.

## Terminal backend check

Hermes should run terminal commands through the `hermes-jump` pod, not directly in the agent container.

```bash
hermes/operator.sh jump-check
```

The expected command output should show the jump pod hostname and `/home/hermes` working directory. If the agent asks for approval, approve only benign read-only commands while TASK-030 is incomplete.

The Kubernetes RBAC boundary can be checked directly:

```bash
kubectl exec -n hermes deploy/hermes-jump -- kubectl auth can-i get pods -n llama
kubectl exec -n hermes deploy/hermes-jump -- kubectl auth can-i delete pods -n llama
```

Expected: `yes` for read-only pod access, `no` for destructive pod deletion.

## Rollback / disable

This access path has no external exposure. To disable local operator access, stop the port-forward process.

To disable the API server entirely, remove or set `API_SERVER_ENABLED=false` in `hermes/hermes-deployment.yaml`, remove the API port/probes or switch probes back to an exec/readiness marker, then apply the deployment. That is a manifest rollback and should be committed separately.

To rotate the API key:

```bash
kubectl create secret generic hermes-api-server-key \
  -n hermes \
  --from-literal=api-key="$(openssl rand -hex 32)" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl rollout restart deployment/hermes-agent -n hermes
kubectl rollout status deployment/hermes-agent -n hermes
```

Do not commit the generated key.

## OpenViking memory provider

Hermes uses the bundled OpenViking plugin for persistent, tiered semantic memory across sessions. The plugin is pre-installed in the Docker image at `/opt/hermes/plugins/memory/openviking/` — no `pip install openviking` needed. It uses `httpx` (already in the venv) for direct HTTP calls to the OV REST API.

### Configuration

The provider is activated by `memory.provider: openviking` in the ConfigMap. Four env vars control the connection:

| Env Var | Value | Purpose |
|---|---|---|
| `OPENVIKING_ENDPOINT` | `http://openviking.viking.svc.cluster.local:1933` | In-cluster OV service (avoids BasicAuth and WAF issues) |
| `OPENVIKING_API_KEY` | *(from `openviking-api-key` Secret)* | Authenticates to OV's `root_api_key` |
| `OPENVIKING_ACCOUNT` | `default` | Tenant account (matches OV's `default_account`) |
| `OPENVIKING_USER` | `noot` | Tenant user (must match OV's `default_user` — **not** the plugin default `default`) |
| `OPENVIKING_AGENT` | `hermes` | Agent tag for multi-agent identification |

`OPENVIKING_USER=noot` is critical — the homelab's OV instance uses `default_user: "noot"`. Sending the plugin default `default` would create a separate invisible user namespace (see BUG-006).

### Cross-namespace Secret

The `openviking-api-key` Secret lives in the `viking` namespace but the Hermes pod runs in `hermes`. Since K8s doesn't support cross-namespace `secretKeyRef`, the Secret must be duplicated:

```bash
# Copy the API key from viking to hermes namespace
kubectl get secret openviking-api-key -n viking -o jsonpath='{.data.api-key}' \
  | kubectl create secret generic openviking-api-key \
      --namespace=hermes \
      --from-file=api-key=/dev/stdin \
      --dry-run=client -o yaml \
  | kubectl apply -f -
```

Or create with a fresh key that matches both namespaces:

```bash
NEW_KEY=$(openssl rand -base64 32)
kubectl create secret generic openviking-api-key \
    --namespace=hermes \
    --from-literal=api-key="$NEW_KEY" \
    --dry-run=client -o yaml | kubectl apply -f -
kubectl create secret generic openviking-api-key \
    --namespace=viking \
    --from-literal=api-key="$NEW_KEY" \
    --dry-run=client -o yaml | kubectl apply -f -
```

After creating/updating the Secret, restart Hermes:

```bash
kubectl rollout restart deployment/hermes-agent -n hermes
kubectl rollout status deployment/hermes-agent -n hermes
```

### What the plugin does

When active, the OpenViking provider:

1. **Injects context** into the system prompt (OV tools and `viking://` URI scheme description)
2. **Prefetches relevant memories** before each turn (background semantic search)
3. **Syncs each turn** to OV (non-blocking)
4. **Mirrors built-in writes** — `MEMORY.md` → `viking://resources/patterns/`, `USER.md` → `viking://resources/preferences/`
5. **Extracts memories on session end** into 6 categories: profile, preferences, entities, events, cases, patterns
6. **Registers 5 tools**: `viking_search`, `viking_read`, `viking_browse`, `viking_remember`, `viking_add_resource`

### Namespace isolation

Hermes writes to namespaces that don't overlap with compendium-sync:

| Writer | Namespace |
|---|---|
| Hermes built-in mirror | `viking://resources/patterns/`, `viking://resources/preferences/` |
| Hermes session extraction | `viking://user/memories/`, `viking://agent/memories/` |
| Work compendium-sync | `viking://resources/compendium/` |
| Personal compendium-sync | `viking://resources/personal/` |
| Homelab index scripts | `viking://resources/projects/homelab/` |

No coordination mechanism is needed — idempotent upsert semantics mean concurrent writes converge.

### Disable / fallback

To disable the OpenViking provider and fall back to built-in memory only:

1. Remove `provider: openviking` from the `memory` section in the ConfigMap (or set `provider: ""`)
2. Remove the `OPENVIKING_*` env vars from the deployment
3. `kubectl rollout restart deployment/hermes-agent -n hermes`

If `OPENVIKING_ENDPOINT` is set but OV is unreachable, the plugin's `initialize()` health check fails and Hermes falls back to built-in memory automatically.
