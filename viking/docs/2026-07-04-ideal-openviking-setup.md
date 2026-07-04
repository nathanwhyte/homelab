# Ideal OpenViking setup

Date: 2026-07-04
Repo scope: `viking/` in the homelab repository
Cluster scope: `viking` namespace in the K3s cluster

## Executive summary

The ideal OpenViking setup is a small, reproducible, single-writer knowledge-base stack with clearly separated responsibilities:

- OpenViking serves the API, MCP endpoint, WebDAV endpoint, queue orchestration, and AGFS metadata operations.
- Garage S3 stores the authoritative AGFS file tree.
- A dedicated HTTP vector database stores embeddings outside the OpenViking API process.
- A dedicated CUDA embedder serves Qwen3-Embedding-4B embeddings.
- A dedicated CUDA llama.cpp VLM serves L0/L1 generation.
- Local model services handle deterministic, low-latency OpenViking internals.
- Ollama cloud-model routing remains available for higher-quality or bursty agent/model work that should not be tied to OpenViking's indexing GPU budget.
- Hermes uses OpenViking for curated knowledge retrieval, not volatile agent memory.
- Mem0 remains the preferred Hermes memory provider for row-level transactional memory writes.

The deployment should optimize for predictability and recovery over peak indexing throughput. OpenViking should be easy to wipe/recreate at the Kubernetes workload layer without losing canonical content in Garage or the vector database PVC, and easy to fully rebuild when a clean reindex is desired.

## Design principles

1. **Single canonical deploy path**
   - `viking/deploy-openviking.sh` and `viking/manifests/kustomization.yaml` should define the same active resource set.
   - Experimental worker/coordinator/console manifests may remain in the repo, but must not be part of the default deploy path.

2. **Single OpenViking writer**
   - Keep `Deployment/openviking` at one replica with `strategy: Recreate`.
   - Avoid parallel OpenViking API pods writing to the same AGFS tree.
   - Use queue/concurrency settings to tune throughput inside the one process, not multiple writer pods.

3. **Storage is explicit and durable**
   - AGFS: Garage S3 bucket `openviking-agfs`.
   - Vector DB: dedicated `ov-vectordb` deployment and PVC.
   - Runtime scratch/state: `openviking-data` PVC.
   - Model caches: separate PVCs for the embedder and VLM model files.

4. **Model services are stable dependencies**
   - The embedder and VLM should be independently deployable and health-checkable.
   - OpenViking config should point at stable Services, not pod IPs or node-local URLs.
   - Service names should abstract implementation details where useful: `embedder-qwen`, `llamacpp-vlm`.

5. **No secrets in git**
   - Commit only `*.secret.yaml.example` templates.
   - Real secret files stay untracked.
   - Deploy scripts should fail early when required secrets are missing.

6. **Public access is least-surprise**
   - `/mcp` should be reachable by MCP clients with OpenViking bearer/API-key auth.
   - All public paths rely on OpenViking's own API-key auth (enforced on every tier); no Traefik BasicAuth layer (decision A, 2026-07-04 — see "Ingress and middleware").
   - Internal cluster access should use ClusterIP Services.
   - LAN/debug access should use a clearly named NodePort Service.

7. **Observability is part of the stack**
   - Metrics exporter sidecar, ServiceMonitor, PrometheusRule, and dashboard ConfigMap should be in the default canonical manifest set.
   - Alert on API reachability, stale lock accumulation, and queue non-drain.

## Target architecture

```text
Public / MCP clients
  |
  |  https://context.nathanwhyte.dev
  v
Traefik / Cloudflare tunnel
  |
  |-- /mcp -------------------------------> openviking:1933  (OV bearer/API-key auth)
  |-- /, /api/v1/*, /webdav/* ------------> openviking:1933  (OV API-key auth)

OpenViking API pod on timmy
  |-- app container: ghcr.io/volcengine/openviking:v0.4.4
  |-- ov-exporter sidecar
  |
  |-- AGFS file tree ---------------------> Garage S3 bucket openviking-agfs
  |-- vector operations ------------------> ov-vectordb:5000
  |-- embeddings -------------------------> embedder-qwen:8080/v1
  |-- L0/L1 generation -------------------> llamacpp-vlm:80/v1

Model services
  |-- embedder-qwen-cuda on wemby GTX 1060
  |-- llamacpp-cuda-ov on manu GTX 1080
  |-- ollama / chat-ollama-proxy in llama namespace for local chat models and cloud model routing

Monitoring
  |-- openviking Service metrics port 9210
  |-- ServiceMonitor/openviking
  |-- PrometheusRule/openviking-alerts
  |-- Grafana dashboard ConfigMap
```

## Canonical Kubernetes resources

The default deploy path should apply these resources and only these resources, plus untracked real secrets.

### Namespace

- `Namespace/viking`

### Required secrets

Created from local untracked files copied from examples:

- `Secret/openviking-api-key`
  - `api-key`: root key injected into `ov.conf`.
  - `user-api-key`: key used by helper tools such as the metrics exporter.
- `Secret/openviking-s3-credentials`
  - `AWS_ACCESS_KEY_ID`
  - `AWS_SECRET_ACCESS_KEY`

### ConfigMaps

- `ConfigMap/openviking-config`
  - Worker/legacy config only if still needed by dormant experimental manifests.
- `ConfigMap/openviking-standalone-config`
  - Canonical app config for the active OpenViking deployment.
- `ConfigMap/openviking-exporter`
  - Generated from `viking/exporters/ov-exporter.py`.
- `ConfigMap/openviking-stack-health-dashboard`
  - Generated from `viking/dashboards/openviking-stack-health.json`.

### Storage

- `PersistentVolumeClaim/openviking-data`
  - Runtime scratch, temp files, local process state.
- `PersistentVolumeClaim/ov-vectordb-data`
  - Persistent vector database files.
- `PersistentVolumeClaim/embedder-qwen-model-cache`
  - Qwen embedding model cache.
- `PersistentVolumeClaim/llama-cuda-model-cache`
  - VLM model cache.

### Core services and deployments

- `Deployment/ov-vectordb`
  - Node: `timmy`.
  - Image: `ghcr.io/volcengine/openviking:v0.4.4`.
  - Command: `python -m openviking.storage.vectordb.service.server_fastapi`.
  - Service: `Service/ov-vectordb` on port `5000`.

- `Deployment/embedder-qwen-cuda`
  - Node: `wemby`.
  - Runtime class: `nvidia`.
  - Model: Qwen3-Embedding-4B GGUF.
  - Dimensions: `2560`.
  - Recommended llama.cpp settings:
    - `--embedding`
    - `--pooling last`
    - `--ctx-size 8192`
    - `--parallel 1`
    - `--batch-size 512`
  - Service: `Service/embedder-qwen` on port `8080`.

- `Deployment/llamacpp-cuda-ov`
  - Node: `manu`.
  - Runtime class: `nvidia`.
  - Replicas: `1`, steady-state always on.
  - Model: Qwen3-8B GGUF, quantized to fit GTX 1080 VRAM.
  - Recommended llama.cpp settings:
    - `--ctx-size 32768`
    - `--parallel 4`
    - `--cache-type-k q4_0`
    - `--cache-type-v q4_0`
    - `--flash-attn on`
    - `--metrics`
  - Services:
    - `Service/llamacpp-vlm` as the stable generic VLM endpoint.
    - `Service/llamacpp-cuda-llm` as the direct CUDA endpoint if needed for debugging.

### Local and cloud model layer

OpenViking should depend on stable in-cluster model endpoints for its core indexing path, while the broader homelab/Hermes model layer can still use Ollama and cloud-routed models for tasks that are not part of OpenViking's critical path.

#### OpenViking-local models

These are the preferred dependencies for OpenViking itself:

- **Embeddings**: `embedder-qwen.viking.svc.cluster.local:8080/v1`
  - Local CUDA llama.cpp server.
  - Qwen3-Embedding-4B.
  - 2560 dimensions.
  - Dedicated to OpenViking/Mem0-style embedding traffic.
  - Should remain stable and not be shared with general chat workloads.

- **VLM / semantic generation**: `llamacpp-vlm.viking.svc:80/v1`
  - Local CUDA llama.cpp server.
  - Qwen3-8B GGUF on `manu`.
  - Generates L0 abstracts and L1 overviews.
  - Should stay dedicated to OpenViking indexing so queue latency is predictable.

This separation prevents routine agent chat traffic from evicting or saturating the models OpenViking needs to process semantic queue work.

#### Ollama local models

The `llama` namespace provides the broader local Ollama model service, currently centered on `ollama.llama.svc.cluster.local:11434` and fronted by the chat proxy where appropriate.

Recommended role:

- Hermes auxiliary reasoning and local chat workloads.
- Mem0 LLM extraction when configured to use local Ollama.
- Opportunistic local model calls where cold-start latency is acceptable.
- Not the default backend for OpenViking embeddings or L0/L1 generation.

Operational notes:

- Keep `OLLAMA_KEEP_ALIVE` short enough to free VRAM when idle.
- Expect first-request cold starts for infrequent jobs.
- Use the chat proxy for automation where reasoning/thinking suppression and counters matter.
- Do not point OpenViking at the general Ollama service unless intentionally testing an alternate model route.

#### Ollama cloud models / proxy-routed models

Cloud-routed models reachable through the Ollama-compatible proxy layer are useful for tasks that benefit from stronger reasoning or external provider capacity, but they should not be required for OpenViking's baseline availability.

Recommended role:

- Hermes primary/auxiliary chat models when local models are overloaded or lower quality.
- One-off high-quality summarization, planning, or code-review tasks.
- Burst capacity for agent workflows that should not compete with OpenViking indexing.
- Fallback during local GPU maintenance.

Not recommended:

- Making OpenViking's core indexing path depend on cloud models by default.
- Using cloud models for high-volume bulk reindex unless cost and rate limits are explicitly accepted.
- Mixing provider-specific model behavior into OpenViking's reproducibility assumptions.

Ideal routing policy:

| Workload | Preferred backend | Fallback | Rationale |
| --- | --- | --- | --- |
| OpenViking embeddings | `embedder-qwen` local CUDA | none, fail visibly | Embedding dimensions must remain stable at 2560. |
| OpenViking L0/L1 generation | `llamacpp-vlm` local CUDA | manually selected cloud model only for recovery | Keeps indexing reproducible and cost-free. |
| Hermes memory extraction | local Ollama via Mem0 config | cloud model through proxy | Memory extraction can tolerate provider changes better than vectors can. |
| Hermes agent chat/reasoning | configured provider/proxy model | local Ollama or cloud proxy | Quality/latency tradeoff depends on the session. |
| Bulk reindex | local VLM + local embedder | avoid cloud unless intentional | Prevents surprise spend/rate-limit failures. |

The important boundary is that OpenViking's vector schema and semantic artifacts should not silently change because the general agent model router changed.

- `Deployment/openviking`
  - Node: `timmy`.
  - Replicas: `1`.
  - Strategy: `Recreate`.
  - Containers:
    - `openviking`
    - `ov-exporter`
  - No lock-cleanup sidecar: removed 2026-07-04 (homelab commit `5d0e0ff`) after upstream GH #2015 was verified fixed on v0.4.4 via the reproducer `viking/tests/test_subtree_lock_fix.py` (two concurrent sibling writes, 1.4s each, 0.0s skew). The `OpenVikingLocksAccumulating` alert is the regression tripwire; manual cleanup is `find /app/data/viking -name '.path.ovlock' -delete`, and stale in-memory lock handles are cleared by a pod restart.
  - Init containers:
    - config rewrite from template + secrets.
    - temp/stale lock cleanup.
    - compatibility patch only while required by the pinned OpenViking version.
  - Services:
    - `Service/openviking` ClusterIP on `1933` and metrics `9210`.
    - `Service/openviking-lan` NodePort `31933` for LAN/debug access.

### Ingress and middleware

- `Middleware/openviking-https-redirect`
- `Ingress/openviking-redirect`
- `Ingress/openviking-ingress`
- `Ingress/openviking-mcp`

> **DECISION RESOLVED (2026-07-04): Option A — API-key-only.** The earlier BasicAuth prescription conflicted with the June-21 IMPR-1007 decision (API-key-only on `context.nathanwhyte.dev`) and with BUG-1031 (Traefik's `kubernetescrd` provider fails to resolve Middleware CRD references, 404ing any route that wires one in). Confirmed by the operator and executed in IMPR-1007 Phase 4 (homelab `606591e`): the never-deployed `openviking-basicauth` Middleware manifest and htpasswd secret example were deleted, the dangling ingress annotation stripped (live-verified `/health` 200, `/mcp` 401 without bearer), and `deploy-openviking.sh`'s auth-secret block removed. OV's `root_api_key` (trusted mode) is the auth boundary on every tier. Revisit only if a browser-facing UI ever needs an extra prompt — and prefer Cloudflare Access over Traefik BasicAuth in that case.

Recommended routing:

- `/mcp`: OpenViking bearer/API-key auth only.
- `/`, `/api/v1/*`, `/webdav/*`: OpenViking API-key auth (no Traefik auth layer).

### Observability

- `ServiceMonitor/openviking`
- `PrometheusRule/openviking-alerts`
- Grafana dashboard ConfigMap

Minimum useful metrics/alerts:

- `openviking_up == 0` for API/exporter failures.
- `.path.ovlock` count above threshold for stale lock accumulation.
- queue pending count not decreasing for queue stalls.

## Canonical `ov.conf` shape

The active config should use S3 AGFS, HTTP vectordb, Qwen embeddings, and the generic VLM service.

```json
{
  "default_account": "default",
  "default_user": "noot",
  "storage": {
    "workspace": "/app/data",
    "transaction": {
      "lock_timeout": 30.0,
      "lock_expire": 60.0
    },
    "vectordb": {
      "backend": "http",
      "name": "context",
      "dimension": 2560,
      "url": "http://ov-vectordb.viking.svc.cluster.local:5000"
    },
    "agfs": {
      "backend": "s3",
      "s3": {
        "bucket": "openviking-agfs",
        "endpoint": "http://garage.garage.svc.cluster.local:3900",
        "region": "garage",
        "use_path_style": true
      }
    }
  },
  "embedding": {
    "max_input_tokens": 8192,
    "dense": {
      "provider": "openai",
      "model": "qwen3-embedding-4b",
      "api_base": "http://embedder-qwen.viking.svc.cluster.local:8080/v1",
      "dimension": 2560,
      "batch_size": 256
    },
    "max_concurrent": 6
  },
  "vlm": {
    "provider": "litellm",
    "model": "openai/current.gguf",
    "api_base": "http://llamacpp-vlm.viking.svc/v1",
    "max_concurrent": 4,
    "timeout": 300
  },
  "server": {
    "host": "0.0.0.0",
    "port": 1933,
    "workers": 2,
    "auth_mode": "trusted"
  }
}
```

Secrets should be injected by an init container rather than committed in the ConfigMap.

## Deployment workflow

### Preflight

1. Confirm cluster context:
   - `kubectl --context=tailnet get nodes`
2. Confirm required untracked secret files exist:
   - `viking/manifests/openviking-api-key.secret.yaml`
   - `viking/manifests/openviking-s3-credentials.secret.yaml`
3. Render manifests:
   - `kubectl kustomize viking/manifests >/tmp/openviking-rendered.yaml`
4. Validate without mutation:
   - `kubectl --context=tailnet apply --dry-run=server -f /tmp/openviking-rendered.yaml`
5. Review live diff:
   - `kubectl --context=tailnet diff -f /tmp/openviking-rendered.yaml`

### Apply

Only after approval:

```bash
KUBECTL_CONTEXT=tailnet viking/deploy-openviking.sh
```

### Post-apply verification

1. Workloads:
   - `kubectl --context=tailnet -n viking get pods,deploy,svc,endpoints,pvc`
2. Rollouts:
   - `kubectl --context=tailnet -n viking rollout status deploy/ov-vectordb`
   - `kubectl --context=tailnet -n viking rollout status deploy/embedder-qwen-cuda`
   - `kubectl --context=tailnet -n viking rollout status deploy/llamacpp-cuda-ov`
   - `kubectl --context=tailnet -n viking rollout status deploy/openviking`
3. Health from inside the pod:
   - `GET http://127.0.0.1:1933/health`
   - `GET http://127.0.0.1:1933/ready`
4. OpenViking API smoke test with tenant headers.
5. MCP smoke test from Hermes:
   - `hermes mcp test openviking`
6. Search/read round trip against a known resource.
7. Observability:
   - Prometheus target up.
   - Grafana dashboard loads.
   - No queue/lock alerts firing.

## Clean rebuild workflow

A clean workload rebuild should not imply data loss.

1. Preserve data stores:
   - Do not delete Garage bucket data.
   - Do not delete `ov-vectordb-data` unless intentionally forcing a full re-embed.
   - Do not delete model cache PVCs unless intentionally forcing model re-download.
2. Recreate workloads in dependency order:
   - namespace/secrets/config
   - vectordb
   - embedder
   - VLM
   - OpenViking
   - ingress/observability
3. Verify readiness at each layer before moving to the next.
4. Run a narrow smoke test before submitting bulk sync/reindex work.

A full semantic rebuild is different: it intentionally wipes or recreates vector data, then reindexes AGFS content. That should be a separate, explicit operation with a rollback plan.

## Data strategy

OpenViking should store curated, durable knowledge artifacts rather than raw source-code mirrors or transient session logs.

Recommended content:

- architecture decisions
- non-obvious debugging discoveries
- operational runbooks
- stable service topology summaries
- high-signal troubleshooting notes

Avoid:

- bulk source-code imports
- command output dumps
- short-lived incident state
- duplicate notes from other memory systems
- raw session transcripts

## Known failure modes this setup avoids

- **Embedder mismatch**: the deploy path must use `embedder-qwen-cuda`, not retired `embedder-llamacpp`.
- **Missing model dependency**: the deploy path must apply and wait for `llamacpp-cuda-ov` before depending on VLM generation.
- **Missing exporter ConfigMap**: the OpenViking pod mounts `openviking-exporter`; the canonical path must apply it.
- **False optional S3 credentials**: S3 AGFS is canonical, so Garage credentials are required.
- **OpenViking before vectordb**: OpenViking readiness depends on the HTTP vectordb service.
- **Exporter auth failure**: `openviking-api-key` must include `user-api-key`.
- **Ingress middleware drift**: resolved 2026-07-04 (decision A, IMPR-1007 Phase 4, homelab `606591e`) — the BasicAuth Middleware and its ingress annotation were deleted; no ingress references a viking-namespace auth middleware anymore. See the **DECISION RESOLVED** callout in "Ingress and middleware".
- **Stale lock accumulation**: no longer mitigated by a lock-cleanup sidecar (removed 2026-07-04; upstream GH #2015 fixed in v0.4.4). The `OpenVikingLocksAccumulating` alert is the regression tripwire; manual cleanup is `find /app/data/viking -name '.path.ovlock' -delete`, and a pod restart clears stale in-memory lock handles.
- **Unbounded experimental rollout**: dormant worker/coordinator/console manifests must not be default-applied.

## Future cleanup recommendations

1. Move retired manifests into an explicit `viking/manifests/retired/` or `viking/manifests/experimental/` folder.
2. Update stale references in skills/docs that still describe the 768-dim nomic embedder as current.
3. Add a CI check that `kubectl kustomize viking/manifests` succeeds.
4. Add a script check that every manifest referenced by `deploy-openviking.sh` exists and every required mounted ConfigMap is applied.
5. Decide whether `openviking-configmap.yaml` is still needed or should be retired in favor of only `openviking-standalone-configmap.yaml` for the active path.
6. Document the exact procedure for a full semantic rebuild, including when to preserve vs wipe `ov-vectordb-data`.
