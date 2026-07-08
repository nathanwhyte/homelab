# Mem0 Memory Provider

> **⚠️ Torn down 2026-07-02** — the `mem0` namespace was deleted and the tunnel routes removed;
> see [TORN-DOWN.md](TORN-DOWN.md). This README describes the stack as it ran and is retained
> as the restore reference (re-apply manifests + load the archived `pg_dumpall`).

Self-hosted Mem0 deployment for Hermes agent memory, replacing OpenViking as the `memory` provider while keeping OV for knowledge base operations.

## Architecture

```text
Hermes ──memory──▶ mem0-adapter:18080 ──▶ mem0-server:8080 ──▶ mem0-postgres:5432 (pgvector)
                           │
                           └── translates Platform v1/v3 API ──▶ OSS REST API

mem0-server:
  ├── LLM extraction ──▶ ollama.llama (gemma4:12b-it-qat)
  └── Embedding      ──▶ embedder-llamacpp.viking (nomic-embed-text-v1.5)
                         ⚠️ BROKEN — embedder-llamacpp retired (replicas=0) 2026-06-30.
                            See "Embedding status" below.

Hermes ──knowledge──▶ openviking.viking:1933 (unchanged)
```

**Split-provider design**: Mem0 handles agent memory reads/writes (the `memory` tool, `mem0_search`, `mem0_profile`, `mem0_conclude`). OpenViking handles knowledge base operations (`viking_search`, `viking_read`, `viking_browse`, `viking_add_resource`). No data loss — both systems coexist.

The **mem0-adapter** sidecar exists because the upstream `nousresearch/hermes-agent` image uses the Mem0 Platform `MemoryClient` (routes `/v1/ping/`, `/v3/memories/add/`, `/v3/memories/search/`, `Authorization: Token <key>`), while the self-hosted OSS server speaks OSS REST routes (`/memories`, `/search`, `X-API-Key`). The adapter translates between the two until upstream Hermes PR [#15624](https://github.com/NousResearch/hermes-agent/pull/15624) lands.

## Components

| Component             | Manifest / Image                                                                               | Resources                 | Storage                   |
| --------------------- | ---------------------------------------------------------------------------------------------- | ------------------------- | ------------------------- |
| Mem0 API server       | `mem0-server-deployment.yaml`                                                                  | 0.5-2 CPU, 1-2 Gi RAM     | emptyDir (SQLite history) |
| Mem0 API server (LAN) | `mem0-server-lan` NodePort service (8080:30080)                                                | —                         | —                         |
| Mem0 Dashboard        | `mem0-dashboard-deployment.yaml`                                                               | 50m-1 CPU, 128Mi-1 Gi RAM | none                      |
| PostgreSQL + pgvector | `mem0-postgres-statefulset.yaml`                                                               | 0.25-1 CPU, 0.5-2 Gi RAM  | 5Gi PVC (Longhorn)        |
| mem0-adapter sidecar  | `hermes/mem0-adapter/` → `registry.nathanwhyte.dev/homelab/mem0-adapter`                       | 50m-1 CPU, 64Mi-512Mi     | none                      |
| Custom Hermes image   | `hermes/mem0-adapter/Dockerfile.hermes` → `registry.nathanwhyte.dev/homelab/hermes-agent-mem0` | —                         | —                         |
| Secrets               | `mem0-secrets.yaml` + `hermes/hermes-deployment.yaml`                                          | —                         | —                         |

**Total footprint**: ~3.5 vCPU, ~5 Gi RAM (fits on timmy alongside Ollama + OV vectordb; Hermes itself runs on manu CPU).

## Building the API server image (amd64)

The upstream `mem0/mem0-api-server` image on Docker Hub is **arm64-only**; the cluster
nodes are all amd64, so the image is built from source and pushed to Harbor
(`registry.nathanwhyte.dev/homelab/mem0-api-server`).

```bash
git clone --depth 1 https://github.com/mem0ai/mem0.git
cd mem0/server
# Apply the homelab source patch (routes embeddings off OPENAI_API_BASE)
git apply /path/to/homelab/mem0/build/embedder-base-url.patch
# Copy the homelab Dockerfile (adds libpq5, drops dev --reload)
cp /path/to/homelab/mem0/build/Dockerfile ./Dockerfile.homelab
kubectl get secret harbor-core -n harbor -o jsonpath='{.data.HARBOR_ADMIN_PASSWORD}' \
  | base64 -d | docker login registry.nathanwhyte.dev -u admin --password-stdin
docker buildx build --platform linux/amd64 -f Dockerfile.homelab \
  -t registry.nathanwhyte.dev/homelab/mem0-api-server:<sha> --push .
```

Three deviations from the stock `server/Dockerfile` were required for a working deploy:

| Fix                                                   | Where                                | Why                                                                                                                                                                                          |
| ----------------------------------------------------- | ------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `apt-get install libpq5`                              | `mem0/build/Dockerfile`              | Upstream pins plain `psycopg` (not `psycopg[binary]`); the slim base ships no libpq, so the server can't import.                                                                             |
| `alembic upgrade head` before uvicorn                 | Deployment `command:`                | Stock CMD starts uvicorn directly and never migrates — app tables (`request_logs`, api keys, settings) are missing without it.                                                               |
| `HISTORY_DB_PATH=/data/history/history.db` + emptyDir | Deployment env + volume              | Default `/app/history/history.db` dir only exists via the dev compose volume; sqlite can't create it otherwise.                                                                              |
| `openai_base_url` on the embedder                     | `mem0/build/embedder-base-url.patch` | Stock `DEFAULT_CONFIG` sends both LLM and embeddings to the single `OPENAI_API_BASE`; the patch makes it honor `MEM0_EMBEDDER_API_BASE` so embeddings go to `embedder-llamacpp`, not Ollama. |

**Auth & health quirks:** the `MEM0_API_KEY` must be sent as the **`X-API-Key`** header
(the `Authorization: Bearer` path only decodes JWTs). There is **no `/health` route** —
use `/` (unauthenticated 307 redirect) for k8s probes.

## Deployment

### 1. Generate secrets

```bash
# Generate random secrets
POSTGRES_PASSWORD=$(openssl rand -base64 24 | tr -d '/+=' | head -c 32)
MEM0_API_KEY=$(openssl rand -base64 24 | tr -d '/+=' | head -c 32)
JWT_SECRET=$(openssl rand -base64 24 | tr -d '/+=' | head -c 32)

# Create the secret (edit mem0-secrets.yaml with the generated values first)
kubectl apply -f mem0/manifests/namespace.yaml
kubectl apply -f mem0/manifests/mem0-secrets.yaml
```

### 2. Deploy infrastructure

```bash
kubectl apply -f mem0/manifests/mem0-postgres-statefulset.yaml
kubectl apply -f mem0/manifests/mem0-server-deployment.yaml
```

### 3. Wait for health

```bash
kubectl rollout status deployment/mem0-server -n mem0
kubectl rollout status statefulset/mem0-postgres -n mem0
```

### 4. Verify Mem0 API

```bash
# Health check (there is no /health route; / returns a 307 redirect to Swagger UI)
curl -sf http://mem0-server.mem0.svc.cluster.local:8080/

# Create a test memory
# Auth: use X-API-Key (Bearer is rejected by the OSS server)
# Path: /memories/ (no /v1/ prefix on the OSS server)
curl -X POST http://mem0-server.mem0.svc.cluster.local:8080/memories/ \
  -H "X-API-Key: $MEM0_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "I prefer dark mode for coding"}], "user_id": "test-user"}'
```

### 5. Build and push the adapter + custom Hermes image

The adapter lives in `hermes/mem0-adapter/`. It exposes the Platform v1/v3 API
on port 18080 and forwards to the OSS server.

```bash
cd hermes/mem0-adapter
TAG=$(git rev-parse --short HEAD)

# Build the adapter sidecar
kubectl get secret harbor-core -n harbor -o jsonpath='{.data.HARBOR_ADMIN_PASSWORD}' \
  | base64 -d | docker login registry.nathanwhyte.dev -u admin --password-stdin
docker buildx build --platform linux/amd64 -t registry.nathanwhyte.dev/homelab/mem0-adapter:${TAG} --push .

# Build the custom Hermes image (adds mem0ai==2.0.6 to upstream image)
docker buildx build --platform linux/amd64 -f Dockerfile.hermes \
  -t registry.nathanwhyte.dev/homelab/hermes-agent-mem0:${TAG} --push .
```

Both images are tiny operational patches:

- **mem0-adapter** translates Platform requests to OSS routes and auth.
- **hermes-agent-mem0** installs `mem0ai==2.0.6` into the upstream Hermes venv
  (`/opt/hermes/.venv/bin/python`) because the upstream image does not ship it.

### 6. Wire Hermes

Update `hermes/hermes-configmap.yaml`:

```yaml
memory:
  memory_enabled: true
  user_profile_enabled: true
  memory_char_limit: 20000
  max_chars: 35000
  user_char_limit: 12000
  provider: mem0 # was: openviking
  mem0:
    api_key: "${MEM0_API_KEY}"
    base_url: "http://localhost:18080" # points at the in-pod adapter sidecar
```

Update `hermes/hermes-deployment.yaml`:

- Set the Hermes image to `registry.nathanwhyte.dev/homelab/hermes-agent-mem0:${TAG}`.
- Add the `mem0-adapter` sidecar container (see the live manifest for the full
  snippet; it mounts no volumes and uses `MEM0_URL` + `MEM0_API_KEY` env vars).
- Add the `mem0-plugin-copy` init container that copies `patched-plugin.py`
  from the adapter image to an `emptyDir`.
- Mount the patched plugin over `/opt/hermes/plugins/memory/mem0/__init__.py`
  via `subPath`.
- Add Hermes env vars:

```yaml
# Mem0 memory provider (IDEA-029)
- name: MEM0_API_KEY
  valueFrom:
    secretKeyRef:
      name: mem0-api-key # lives in the hermes namespace
      key: MEM0_API_KEY
- name: MEM0_BASE_URL
  value: "http://localhost:18080"
```

Then:

```bash
kubectl apply -f hermes/hermes-configmap.yaml
kubectl apply -f hermes/hermes-deployment.yaml
kubectl rollout restart deployment/hermes-agent -n hermes
```

## Dashboard

The mem0 server source ships a Next.js dashboard (`/app/dashboard`) that provides a
web UI for memories, API keys, entities, configuration, and request history. It is
built and deployed as a separate container image and exposed at
**`https://mem0.nathanwhyte.dev`**.

```text
Browser ──▶ https://mem0.nathanwhyte.dev ──▶ mem0-dashboard:3000
                 │
                 └── API path prefixes ──▶ mem0-server:8080
```

### Build the dashboard image

The dashboard source lives inside the mem0-api-server image at `/app/dashboard`,
so the dashboard image is built from that source with a multi-stage Node/pnpm
build. Keep the tag in sync with the API server image to avoid version drift.

```bash
cd mem0/build
TAG=b55c51e-hl4  # match the mem0-api-server tag you are running

kubectl get secret harbor-core -n harbor -o jsonpath='{.data.HARBOR_ADMIN_PASSWORD}' \
  | base64 -d | docker login registry.nathanwhyte.dev -u admin --password-stdin

docker buildx build --platform linux/amd64 -f Dockerfile.dashboard \
  -t registry.nathanwhyte.dev/homelab/mem0-dashboard:${TAG} --push .
```

### Deploy

```bash
kubectl apply -f mem0/manifests/mem0-dashboard-deployment.yaml
kubectl rollout status deployment/mem0-dashboard -n mem0
```

The dashboard container receives three important URLs:

| Env var               | Value                                            | Purpose                                                             |
| --------------------- | ------------------------------------------------ | ------------------------------------------------------------------- |
| `NEXT_PUBLIC_API_URL` | `https://api-mem0.nathanwhyte.dev`               | Public base URL used by the browser for API calls                   |
| `API_INTERNAL_URL`    | `http://mem0-server.mem0.svc.cluster.local:8080` | In-cluster URL used by Next.js server-side routes/API handlers      |
| `DASHBOARD_URL`       | `https://mem0.nathanwhyte.dev`                   | Used for CORS origin in mem0-server and for secure-cookie detection |

`entrypoint.sh` swaps the `NEXT_PUBLIC_*` placeholders baked into the Next.js
standalone output at pod startup; `API_INTERNAL_URL` and `DASHBOARD_URL` are read
at runtime and do not need placeholder substitution.

### Expose via Cloudflare tunnel

The dashboard and the mem0 API get their own hostnames. This keeps the
routing simple: `mem0.nathanwhyte.dev` serves only the Next.js dashboard, and
`api-mem0.nathanwhyte.dev` serves the mem0-server REST API. The dashboard's
browser-side client uses `NEXT_PUBLIC_API_URL=https://api-mem0.nathanwhyte.dev`,
and its server-side routes/API handlers proxy through `API_INTERNAL_URL`.

The tunnel rules in `cloudflare/main-tunnel/cloudflared-configmap.yaml`:

```yaml
- hostname: mem0.nathanwhyte.dev
  service: http://mem0-dashboard.mem0.svc.cluster.local:3000
- hostname: api-mem0.nathanwhyte.dev
  service: http://mem0-server.mem0.svc.cluster.local:8080
```

Create the DNS records with `cloudflared`:

```bash
kubectl get secret cloudflared-credentials -n cloudflare-system \
  -o jsonpath='{.data.credentials\.json}' | base64 -d > /tmp/cloudflared-creds.json
cloudflared tunnel --credentials-file /tmp/cloudflared-creds.json route dns \
  936478c5-b4a9-400c-8490-053451dda497 mem0.nathanwhyte.dev
cloudflared tunnel --credentials-file /tmp/cloudflared-creds.json route dns \
  936478c5-b4a9-400c-8490-053451dda497 api-mem0.nathanwhyte.dev
```

Then restart the tunnel:

```bash
kubectl apply -f cloudflare/main-tunnel/cloudflared-configmap.yaml
kubectl rollout restart deployment/cloudflared -n cloudflare-system
```

### First-admin setup

On first visit, the dashboard redirects to `/setup` and lets you create the admin
account. Once a user exists, registration closes. If you ever need to reset it,
delete the row from the `mem0_app` database:

```bash
kubectl exec -n mem0 sts/mem0-postgres -- psql -U postgres -d mem0_app \
  -c "DELETE FROM users WHERE email='<your-email>';"
```

### Verify

```bash
# Dashboard serves a 307 redirect to /setup when no admin exists
curl -sI https://mem0.nathanwhyte.dev/
# API path is routed to mem0-server
curl -s https://mem0.nathanwhyte.dev/auth/setup-status
```

## Prometheus memory-inventory exporter

Mem0 OSS does not expose Prometheus metrics for stored memories (`/metrics` and
`/api/metrics` return 404). The homelab deploy adds a tiny aggregate-only
exporter that queries `public.memories.payload` in Postgres and intentionally
does **not** expose memory text.

| Metric                                    | Meaning                                           |
| ----------------------------------------- | ------------------------------------------------- |
| `mem0_exporter_up`                        | Exporter can query Postgres (`1`) or failed (`0`) |
| `mem0_memories_total`                     | Total stored memories                             |
| `mem0_memories_by_user{user_id=...}`      | Stored memories grouped by `user_id`              |
| `mem0_memories_by_agent{agent_id=...}`    | Stored memories grouped by `agent_id`             |
| `mem0_memories_by_role{role=...}`         | Stored memories grouped by payload role           |
| `mem0_memory_oldest_timestamp_seconds`    | Oldest memory creation timestamp                  |
| `mem0_memory_newest_timestamp_seconds`    | Newest memory creation timestamp                  |
| `mem0_memory_metadata_key_total{key=...}` | Count of memories containing each metadata key    |

### Build the exporter image

Local Docker may not be available on the MacBook; the cluster has a BuildKit pod
in the `build` namespace that can build and push to Harbor.

```bash
cd ~/code/homelab
TAG=2026-06-30-r2
POD=$(kubectl --context=tailnet -n build get pod -l app=buildkitd -o jsonpath='{.items[0].metadata.name}')
TMP=$(mktemp -d)
cp mem0/exporter/Dockerfile mem0/exporter/mem0_exporter.py "$TMP/"
kubectl --context=tailnet -n build cp "$TMP" "$POD":/tmp/mem0-exporter-build

HARBOR_PASSWORD=$(kubectl --context=tailnet -n harbor get secret harbor-core \
  -o jsonpath='{.data.HARBOR_ADMIN_PASSWORD}' | base64 -d)

kubectl --context=tailnet -n build exec -i "$POD" -- sh -s <<'EOS' "$HARBOR_PASSWORD" "$TAG"
set -eu
PASS="$1"
TAG="$2"
DOCKER_CONFIG=/tmp/docker-config
export DOCKER_CONFIG
mkdir -p "$DOCKER_CONFIG"
AUTH=$(printf 'admin:%s' "$PASS" | base64 | tr -d '\n')
cat > "$DOCKER_CONFIG/config.json" <<EOF
{"auths":{"registry.nathanwhyte.dev":{"auth":"$AUTH"}}}
EOF
buildctl build \
  --frontend dockerfile.v0 \
  --local context=/tmp/mem0-exporter-build \
  --local dockerfile=/tmp/mem0-exporter-build \
  --opt platform=linux/amd64 \
  --output type=image,name="registry.nathanwhyte.dev/homelab/mem0-exporter:${TAG}",push=true
rm -rf "$DOCKER_CONFIG"
EOS
```

### Deploy and verify

```bash
kubectl --context=tailnet apply -f mem0/manifests/mem0-exporter.yaml
kubectl --context=tailnet -n mem0 rollout status deployment/mem0-exporter
kubectl --context=tailnet -n mem0 port-forward svc/mem0-exporter 19091:9090
curl -fsS http://127.0.0.1:19091/healthz
curl -fsS http://127.0.0.1:19091/metrics | grep '^mem0_memories_total '
```

## Embedding status

**embedder-llamacpp (nomic-embed-text-v1.5, 768-dim) was retired 2026-06-30 (replicas=0).**
The live mem0-server is still configured with `MEM0_EMBEDDER_API_BASE=http://embedder-llamacpp.viking.svc.cluster.local:8080/v1`
and `MEM0_EMBEDDING_DIMS=768`, so mem0's embedding path is currently **broken**.

The active cluster embedder is `embedder-qwen` (Qwen3-Embedding-4B Q8_0, 2560-dim) — on timmy's
RX 9070 XT (ROCm) since 2026-07-06 — but mem0 was never re-pointed at it. Fixing this (on any
future restore) requires:

1. Change `MEM0_EMBEDDER_API_BASE` to `http://embedder-qwen.viking.svc.cluster.local:8080/v1`
2. Change `MEM0_EMBEDDING_DIMS` to `2560`
3. Drop and recreate the `memories` table in Postgres (pgvector column width is baked at table creation)
4. Re-embed all existing memories

This is tracked as a known gap — the embedder migration (timmy ROCm → wemby CUDA) happened on 2026-06-29
and the mem0 config was not updated to follow.

## Model routing

| Call type      | Model                           | Endpoint                               | GPU              | Status                  |
| -------------- | ------------------------------- | -------------------------------------- | ---------------- | ----------------------- |
| LLM extraction | **gemma4:12b-it-qat** (local)   | chat-ollama-proxy.llama:11434 → ollama | timmy RX 9070 XT | ✅                      |
| Embedding      | nomic-embed-text-v1.5 (768-dim) | embedder-llamacpp.viking:8080          | wemby GTX 1060   | ❌ retired (replicas=0) |

**Context window is critical (not model size).** mem0 v2.0.6 uses a single-call `ADDITIVE_EXTRACTION_PROMPT` (~9-10K tokens) that returns a `{"memory":[...]}` operations object. With the live Ollama at `OLLAMA_CONTEXT_LENGTH=8192`, that prompt was **truncated**, so local models saw mangled instructions and emitted `{"` then stopped → nothing stored (this looked like a model-capability failure but wasn't — even glm-5.1:cloud only worked because cloud models ignore the local `num_ctx`). Raising `OLLAMA_CONTEXT_LENGTH` to **16384** (`llama/ollama-deployment.yaml`) fixes it: the local `gemma4:12b-it-qat` extracts correctly, so **no cloud model is needed**. Extraction is routed through `chat-ollama-proxy` (`INJECT_REASONING_NONE=true`) so reasoning tokens don't contaminate the JSON.

**Embedding dimensions:** `nomic-embed-text-v1.5` emits 768-dim vectors. mem0 defaults to 1536 (OpenAI) and creates the pgvector column at that width, so `MEM0_EMBEDDING_DIMS=768` is required — and changing it means **dropping and recreating the `memories` table**.

## Rollback

To revert to OV-only memory:

1. Change `memory.provider: openviking` in configmap
2. Remove the mem0-adapter sidecar, the `mem0-plugin-copy` init container, and the patched-plugin volume/volumeMount
3. Switch the Hermes image back to `nousresearch/hermes-agent:latest`
4. Remove MEM0\_\* env vars from deployment
5. `kubectl rollout restart deployment/hermes-agent -n hermes`
6. (Optional) Scale down mem0 namespace: `kubectl scale deployment/mem0-server --replicas=0 -n mem0`

## Hermes integration status

✅ **Fixed by a local adapter sidecar + patched plugin + custom Hermes image.**

The upstream `nousresearch/hermes-agent:latest` image uses the Mem0 Platform
`MemoryClient` without a `host=` parameter, so it cannot talk to the self-hosted
OSS server directly. The homelab deploy adds three small bridging pieces:

1. **`hermes/mem0-adapter/main.py`** — a FastAPI sidecar that exposes the
   Platform v1/v3 endpoints Hermes expects and forwards them to the OSS server:

   | Platform request (Hermes)   | OSS translation  |
   | --------------------------- | ---------------- |
   | `GET /v1/ping/`             | static pong      |
   | `POST /v3/memories/add/`    | `POST /memories` |
   | `POST /v3/memories/search/` | `POST /search`   |
   | `POST /v3/memories/`        | `GET /memories`  |

   The adapter also rewrites auth (`Authorization: Token <key>` → `X-API-Key: <key>`)
   and drops headers that break forwarding (`authorization`, `mem0-user-id`,
   `content-length`).

2. **`hermes/mem0-adapter/patched-plugin.py`** — a copy of the upstream Hermes
   mem0 plugin with two minimal changes:
   - Reads `base_url` from `MEM0_BASE_URL` / `MEM0_HOST` / `mem0.json`.
   - Passes `host=base_url` to `MemoryClient(...)` so the client points at
     `http://localhost:18080` (the adapter sidecar) instead of `https://api.mem0.ai`.

3. **`hermes/mem0-adapter/Dockerfile.hermes`** — layers `mem0ai==2.0.6` onto the
   upstream Hermes image with `uv pip install --python /opt/hermes/.venv/bin/python`,
   because the upstream image does not include the `mem0ai` package.

The manifest wires it together: an init container copies the patched plugin into
a shared `emptyDir`, the Hermes container mounts it over
`/opt/hermes/plugins/memory/mem0/__init__.py`, and the `mem0-adapter` sidecar runs
in the same pod. The upstream OSS/self-hosted PR [#15624](https://github.com/NousResearch/hermes-agent/pull/15624)
is still the preferred long-term fix; the adapter can be retired once it merges.

### End-to-end verification

Tested on the live cluster after applying the updated manifest:

| Step           | Command / check                                                                                   | Result                                                                                                |
| -------------- | ------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| Hermes health  | `./hermes/operator.sh health`                                                                     | `{"status": "ok", ...}` ✅                                                                            |
| Store a memory | `ask "Remember that my favorite test animal is the capybara, for mem0 integration test INFO-055"` | Hermes replied and stored the fact ✅                                                                 |
| Adapter proxy  | `kubectl logs -c mem0-adapter deploy/hermes-agent`                                                | `GET /v1/ping/`, `POST /v3/memories/add/`, `POST /v3/memories/search/` all returned 200 ✅            |
| Database row   | `SELECT ... FROM memories` in `mem0-postgres`                                                     | Row found with payload `Favorite test animal is the capybara (for mem0 integration test INFO-055)` ✅ |
| Memory recall  | `ask "What is my favorite test animal, from our recent mem0 integration test?"`                   | Hermes answered `capybara` ✅                                                                         |

### OSS REST endpoint reference

From the official
[Mem0 OSS REST API docs](https://docs.mem0.ai/open-source/features/rest-api):

| Method   | Path                                  | Description                                                |
| -------- | ------------------------------------- | ---------------------------------------------------------- |
| `POST`   | `/configure`                          | Set memory configuration                                   |
| `GET`    | `/configure`                          | Get current memory configuration                           |
| `GET`    | `/configure/providers`                | List bundled LLM/embedder providers                        |
| `POST`   | `/memories`                           | Create memories                                            |
| `GET`    | `/memories`                           | Get all memories (filter by `user_id`/`agent_id`/`run_id`) |
| `GET`    | `/memories/{memory_id}`               | Get a specific memory                                      |
| `PUT`    | `/memories/{memory_id}`               | Update a memory                                            |
| `DELETE` | `/memories/{memory_id}`               | Delete a specific memory                                   |
| `DELETE` | `/memories`                           | Delete all memories for an identifier                      |
| `GET`    | `/memories/{memory_id}/history`       | Get memory history                                         |
| `POST`   | `/search`                             | Search memories                                            |
| `POST`   | `/reset`                              | Reset all memories                                         |
| `GET`    | `/entities`                           | Distinct `user_id`/`agent_id`/`run_id` values with counts  |
| `DELETE` | `/entities/{entity_type}/{entity_id}` | Cascade-delete all memories for an entity                  |

Note: **none of these use the `/v1/` prefix**. The
[Mem0 API Reference](https://docs.mem0.ai/api-reference) documents the hosted
Platform API, not the OSS server.

### Auth modes

The OSS server supports three auth modes (per the Mem0 docs):

| Mode                  | Header                          | Use case                                            |
| --------------------- | ------------------------------- | --------------------------------------------------- |
| Per-user API key      | `X-API-Key: m0sk_...`           | Programmatic access scoped to a dashboard user      |
| Legacy `MEM0_API_KEY` | `X-API-Key: <env value>`        | Back-compat for deployments that set `MEM0_API_KEY` |
| Bearer JWT            | `Authorization: Bearer <token>` | Dashboard sessions from `POST /auth/login`          |

The current homelab deployment sets `MEM0_API_KEY` and uses it via the
`X-API-Key` header.

### Verified OSS API behavior

Tested by port-forwarding `mem0-server` to `localhost:18080` from this MacBook.

| Call                             | Route                  | Auth                    | Result                               |
| -------------------------------- | ---------------------- | ----------------------- | ------------------------------------ |
| Health                           | `GET /`                | none                    | 307 → Swagger UI ✅                  |
| Create memory                    | `POST /memories/`      | `X-API-Key`             | ✅                                   |
| Create memory                    | `POST /v1/memories/`   | `X-API-Key`             | 404 ❌                               |
| Create memory                    | `POST /memories/`      | `Authorization: Bearer` | 401 ❌                               |
| Search                           | `POST /search`         | `X-API-Key`             | ✅                                   |
| Concurrent distinct writes       | `POST /memories/` (5×) | `X-API-Key`             | ✅                                   |
| Concurrent near-duplicate writes | `POST /memories/` (5×) | `X-API-Key`             | Empty results (server-side dedup) ⚠️ |

### Deduplication pitfall

Mem0's server-side LLM extraction collapses semantically identical messages into
a single fact and returns `{"results": []}` for duplicates. This is by design,
but it can look like a silent failure when several Hermes sub-agents report the
same finding. Use distinct, specific phrasing for test messages.

## Related

- IDEA-029: Original proposal
- BUG-017: OV SUBTREE lock contention (motivation)
- BUG-018: Hermes self-hosted mem0 blocker (resolved via adapter)
- IMPR-005: Route OV models through shared Ollama (related GPU optimization)
- INFO-055: mem0 + Hermes GPU cohabitation benchmark (`OLLAMA_NUM_PARALLEL=6` validated)
