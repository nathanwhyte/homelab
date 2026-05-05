---
date: 2026-05-05
status: approved
topic: "OpenViking improvement open questions — resolved decisions"
---

# Design: OpenViking Improvement Open Questions

**Date**: 2026-05-05
**Source**: `thoughts/shared/research/2026-05-05-viking-improvements.md`

## Decisions

| Question | Decision | Rationale |
|----------|----------|-----------|
| Merge service | Enable | Already running, env vars just need uncommenting. 3x read latency reduction with automatic fan-out fallback when stale. |
| ROCm deployment | Remove entirely | Only the CUDA deployment on manu's GTX 1080 is used. ROCm deployment, service, and PVC are all orphaned. |
| Config templating | InitContainer envsubst | Consistent with existing worker pattern. Inject secrets via env vars → initContainer rewrites ov.conf JSON. No migration tooling required. |
| Auth Secret | Wire into Traefik | Add Middleware CRD + ingress annotation for defense-in-depth (OV API key + Traefik basicAuth). |

## 1. Merge Service Enablement

**What**: Uncomment `OV_MERGED_URL` and `OV_MERGE_STATUS_URL` in `ov-coordinator-deployment.yaml:57-60`.

**Files changed**:
- `viking/ov-coordinator-deployment.yaml` — uncomment env vars

**Validation**:
- Verify `ov-merged` Service selector matches active worker pods
- Rollout restart coordinator after apply
- Confirm coordinator logs show `_use_merged() = True`

**Risk**: Stale reads up to `STALE_THRESHOLD` (600s). Acceptable because OV data is indexed on a schedule, not real-time. Merge service automatically falls back to fan-out when stale or unhealthy.

## 2. Remove ROCm Deployment

**What**: Remove the ROCm llamacpp deployment, its misdirected service, and the orphaned model cache PVC. Only the CUDA deployment on manu's GTX 1080 is used for OV inference.

**Resources to delete from cluster**:
- `deployment/llamacpp-rocm` (already scaled to 0)
- `service/llamacpp-rocm-llm` (selector currently points to CUDA pod — confusing, not needed)
- `pvc/llama-rocm-model-cache` (orphaned since ROCm scaled to 0)

**Files to remove from viking/**:
- `viking/rocm-llamacpp-deployment.yaml`

**Files to update**:
- Remove ROCm-related references from deploy scripts (`deploy-openviking.sh`, `deploy-openviking-parallel.sh`)

**CLAUDE.md update**: Remove the "ROCm LLM" row from the service routing table. The failover note about rolling back to timmy's 9070 XT stays (as a manual recovery procedure), but it won't use the old ROCm manifests — it would be a fresh deployment.

## 3. Config Templating via InitContainer

**What**: Use the same initContainer pattern the worker StatefulSet already uses — env vars from `secretKeyRef` → initContainer rewrites the `ov.conf` JSON before the app starts.

### Step 1: Create API Key Secret

Create `openviking-api-key` Secret containing the API key value. This is a new Secret (the key currently lives only in the ConfigMap).

### Step 2: Wire existing S3 Secret into deployments

The `openviking-s3-credentials` Secret already exists but is never referenced. Add `secretKeyRef` env vars to:
- `ov-coordinator-deployment.yaml`
- `ov-merge-deployment.yaml`
- `ov-consolidate-job.yaml`

### Step 3: Add initContainers for ov.conf rewriting

For the coordinator and merge deployments, add an initContainer that:
1. Reads the base `ov.conf` from the ConfigMap mount
2. Reads `API_KEY`, `S3_ACCESS_KEY`, `S3_SECRET_KEY` from env vars (sourced from Secrets via `secretKeyRef`)
3. Rewrites the JSON with Python: `json.load` → set keys → `json.dump`
4. Writes to a shared emptyDir volume

The app container mounts the rewritten config from the shared emptyDir instead of the ConfigMap directly.

This mirrors the worker StatefulSet's existing `config-gen` initContainer pattern at `ov-worker-statefulset.yaml:35-44`.

### Step 4: Remove hardcoded defaults

- `viking/index-homelab.py:28` — remove hardcoded API key default, require env var
- `viking/mcp-server.py:318-319` — remove hardcoded S3 credential defaults, require env var
- `viking/ov-consolidate-job.yaml:234-235` — replace `value: "47067ca1..."` with `valueFrom.secretKeyRef`

### Step 5: Clean ConfigMap

Remove `api-key`, `access_key`, and `secret_key` from `openviking-configmap.yaml`. The `ov.conf` JSON in the ConfigMap becomes a template with placeholder values that the initContainer replaces.

**Files changed**:
- `viking/openviking-configmap.yaml` — remove plaintext credentials, use placeholders
- `viking/ov-coordinator-deployment.yaml` — add secretKeyRef env vars + config-rewrite initContainer
- `viking/ov-merge-deployment.yaml` — add secretKeyRef env vars + config-rewrite initContainer
- `viking/ov-consolidate-job.yaml` — replace hardcoded value with secretKeyRef
- `viking/ov-worker-statefulset.yaml` — add secretKeyRef env vars (initContainer already exists)
- `viking/index-homelab.py` — remove hardcoded API key default
- `viking/mcp-server.py` — remove hardcoded S3 credential defaults
- New: `viking/openviking-api-key.secret.yaml.example` — template for the API key Secret

**Risk**: The `ov.conf` JSON has the API key in two locations (standalone + embedded). The initContainer script must update both or they'll diverge. Test by checking the running pod's config after deployment.

## 4. Auth Secret → Traefik Middleware

**What**: Create a Traefik `Middleware` CRD referencing `openviking-auth-secret` and attach it to the OV ingress.

**Files changed**:
- New: `viking/openviking-basicauth-middleware.yaml` — Traefik Middleware CRD
- `viking/openviking-ingress.yaml` — add `middleware.traefik.io/name: openviking-basicauth` annotation

**Middleware spec**:
```yaml
apiVersion: traefik.io/v1alpha1
kind: Middleware
metadata:
  name: openviking-basicauth
  namespace: viking
spec:
  basicAuth:
    secret: openviking-auth-secret
```

**Ingress annotation**:
```yaml
annotations:
  traefik.ingress.kubernetes.io/router.middlewares: viking-openviking-basicauth@kubernetescrd
```

**Validation**: After apply, verify that accessing the OV ingress URL prompts for basic auth before reaching the OV API.

**Risk**: None. This is additive. OV's native API key auth still works as a second layer. If basicAuth causes issues, remove the annotation to instantly revert.

## Implementation Order

1. **Security first**: Config templating (Section 3) — removes plaintext credentials
2. **Quick wins**: Merge enablement (Section 1) — one-line uncomment + restart
3. **Safety net**: Auth middleware (Section 4) — new CRD, no existing behavior change
4. **Cleanup last**: ROCm removal (Section 2) — delete orphaned resources, remove manifests

## Out of Scope

These decisions are **not** part of this spec:
- Kustomize migration (4.1) — future work
- Docker image builds (4.2) — future work
- Pod anti-affinity, PDBs, probe fixes — separate improvements from the research doc