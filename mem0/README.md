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

| Component | Manifest | Resources | Storage |
|-----------|----------|-----------|---------|
| Mem0 API server | `mem0-server-deployment.yaml` | 0.5-2 CPU, 1-2 Gi RAM | None |
| PostgreSQL + pgvector | `mem0-postgres-statefulset.yaml` | 0.25-1 CPU, 0.5-2 Gi RAM | 5Gi PVC (Longhorn) |
| Secrets | `mem0-secrets.yaml` | — | — |

**Total footprint**: ~3 vCPU, ~4.5 Gi RAM (fits on timmy alongside Ollama + OV vectordb).

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
  provider: mem0  # was: openviking
  mem0:
    api_key: "${MEM0_API_KEY}"
    base_url: "http://mem0-server.mem0.svc.cluster.local:8080"
```

Update `hermes/hermes-deployment.yaml` env vars (add alongside OPENVIKING_* vars):
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

| Call type | Model | Endpoint | GPU |
|-----------|-------|----------|-----|
| LLM extraction | gemma4:12b-it-qat | ollama.llama:11434 | timmy RX 9070 XT |
| Embedding | nomic-embed-text-v1.5 | embedder-llamacpp.viking:8080 | wemby GTX 1060 |

Note: LLM extraction calls to Ollama share the same `OLLAMA_MAX_LOADED_MODELS=1` constraint as Hermes auxiliary tasks. Mem0 extraction calls may evict the currently loaded model; OLLAMA_KEEP_ALIVE=3m means models unload after 3 min idle. Cold start ~10-15s for gemma4:12b-it-qat.

## Rollback

To revert to OV-only memory:
1. Change `memory.provider: openviking` in configmap
2. Remove MEM0_* env vars from deployment
3. `kubectl rollout restart deployment/hermes-agent -n hermes`
4. (Optional) Scale down mem0 namespace: `kubectl scale deployment/mem0-server --replicas=0 -n mem0`

## Related

- IDEA-029: Original proposal
- BUG-017: OV SUBTREE lock contention (motivation)
- IMPR-005: Route OV models through shared Ollama (related GPU optimization)