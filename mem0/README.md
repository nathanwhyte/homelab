# Mem0 Memory Provider

Self-hosted Mem0 deployment for Hermes agent memory, replacing OpenViking as the `memory` provider while keeping OV for knowledge base operations.

## Architecture

```
Hermes ──memory──▶ mem0-server:8080 ──▶ mem0-postgres:5432 (pgvector)
                          │
                          ├── LLM extraction ──▶ ollama.llama (gemma4:12b-it-qat)
                          └── Embedding    ──▶ embedder-llamacpp.viking (nomic-embed-text-v1.5)

Hermes ──knowledge──▶ openviking.viking:1933 (unchanged)
```

**Split-provider design**: Mem0 handles agent memory reads/writes (the `memory` tool, `mem0_search`, `mem0_profile`, `mem0_conclude`). OpenViking handles knowledge base operations (`viking_search`, `viking_read`, `viking_browse`, `viking_add_resource`). No data loss — both systems coexist.

## Components

| Component             | Manifest                         | Resources                | Storage                   |
| --------------------- | -------------------------------- | ------------------------ | ------------------------- |
| Mem0 API server       | `mem0-server-deployment.yaml`    | 0.5-2 CPU, 1-2 Gi RAM    | emptyDir (SQLite history) |
| PostgreSQL + pgvector | `mem0-postgres-statefulset.yaml` | 0.25-1 CPU, 0.5-2 Gi RAM | 5Gi PVC (Longhorn)        |
| Secrets               | `mem0-secrets.yaml`              | —                        | —                         |

**Total footprint**: ~3 vCPU, ~4.5 Gi RAM (fits on timmy alongside Ollama + OV vectordb).

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

**Auth & health quirks:** the `ADMIN_API_KEY` must be sent as the **`X-API-Key`** header
(the `Authorization: Bearer` path only decodes JWTs). There is **no `/health` route** —
use `/` (unauthenticated 307 redirect) for k8s probes.

## Deployment

### 1. Generate secrets

```bash
# Generate random secrets
POSTGRES_PASSWORD=$(openssl rand -base64 24 | tr -d '/+=' | head -c 32)
ADMIN_API_KEY=$(openssl rand -base64 24 | tr -d '/+=' | head -c 32)
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
# Health check
curl -sf http://mem0-server.mem0.svc.cluster.local:8080/health

# Create a test memory
curl -X POST http://mem0-server.mem0.svc.cluster.local:8080/v1/memories/ \
  -H "Authorization: Bearer $ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "I prefer dark mode for coding"}], "user_id": "test-user"}'
```

### 5. Wire Hermes (after verifying Mem0 works)

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
    base_url: "http://mem0-server.mem0.svc.cluster.local:8080"
```

Update `hermes/hermes-deployment.yaml` env vars (add alongside OPENVIKING\_\* vars):

```yaml
# Mem0 memory provider (IDEA-029)
- name: MEM0_API_KEY
  valueFrom:
    secretKeyRef:
      name: mem0-secrets
      key: ADMIN_API_KEY
```

Then:

```bash
kubectl apply -f hermes/hermes-configmap.yaml
kubectl apply -f hermes/hermes-deployment.yaml
kubectl rollout restart deployment/hermes-agent -n hermes
```

## Model routing

| Call type      | Model                           | Endpoint                               | GPU              |
| -------------- | ------------------------------- | -------------------------------------- | ---------------- |
| LLM extraction | **gemma4:12b-it-qat** (local)   | chat-ollama-proxy.llama:11434 → ollama | timmy RX 9070 XT |
| Embedding      | nomic-embed-text-v1.5 (768-dim) | embedder-llamacpp.viking:8080          | wemby GTX 1060   |

**Context window is critical (not model size).** mem0 v2.0.6 uses a single-call `ADDITIVE_EXTRACTION_PROMPT` (~9-10K tokens) that returns a `{"memory":[...]}` operations object. With the live Ollama at `OLLAMA_CONTEXT_LENGTH=8192`, that prompt was **truncated**, so local models saw mangled instructions and emitted `{"` then stopped → nothing stored (this looked like a model-capability failure but wasn't — even glm-5.1:cloud only worked because cloud models ignore the local `num_ctx`). Raising `OLLAMA_CONTEXT_LENGTH` to **16384** (`llama/ollama-deployment.yaml`) fixes it: the local `gemma4:12b-it-qat` extracts correctly, so **no cloud model is needed**. Extraction is routed through `chat-ollama-proxy` (`INJECT_REASONING_NONE=true`) so reasoning tokens don't contaminate the JSON.

**Embedding dimensions:** `nomic-embed-text-v1.5` emits 768-dim vectors. mem0 defaults to 1536 (OpenAI) and creates the pgvector column at that width, so `MEM0_EMBEDDING_DIMS=768` is required — and changing it means **dropping and recreating the `memories` table**.

## Rollback

To revert to OV-only memory:

1. Change `memory.provider: openviking` in configmap
2. Remove MEM0\_\* env vars from deployment
3. `kubectl rollout restart deployment/hermes-agent -n hermes`
4. (Optional) Scale down mem0 namespace: `kubectl scale deployment/mem0-server --replicas=0 -n mem0`

## Related

- IDEA-029: Original proposal
- BUG-017: OV SUBTREE lock contention (motivation)
- IMPR-005: Route OV models through shared Ollama (related GPU optimization)
- INFO-055: mem0 + Hermes GPU cohabitation benchmark (`OLLAMA_NUM_PARALLEL=6` validated)
