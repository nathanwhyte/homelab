# OpenViking clean rebuild runbook — 2026-07-04

## Goal

Rebuild the OpenViking deployment from a small canonical set instead of replaying the older experimental manifests. The current live cluster is healthy, but the repo deploy path had drifted enough that a scratch deploy would not reproduce it.

## Current canonical architecture

- `openviking` on `timmy`, single replica, `Recreate`, image `ghcr.io/volcengine/openviking:v0.4.7` (v0.4.5 fixed the trusted-mode Role serialization upstream, so the former `role-value-patch` init container is gone; see `viking/docs/2026-07-04-impr-1032-phase-a-precheck.md`).
- AGFS storage: Garage S3 bucket `openviking-agfs` via `openviking-s3-credentials`.
- Vector DB: `ov-vectordb` HTTP backend on `timmy`, 2560 dimensions.
- Embedder: `embedder-qwen-cuda` on `wemby` GTX 1060, Qwen3-Embedding-4B, service `embedder-qwen:8080`.
- VLM: `llamacpp-cuda-ov` on `manu` GTX 1080, service `llamacpp-vlm:80`, steady-state `replicas: 1`.
- Public access: `context.nathanwhyte.dev`, with all paths protected by OpenViking bearer/API-key auth. There is no Traefik BasicAuth layer in the canonical path.
- LAN access: `openviking-lan` NodePort `31933`.
- Observability: `ov-exporter` sidecar, `ServiceMonitor`, `PrometheusRule`, and Grafana dashboard ConfigMap.

## Problems found in the old scratch path

1. `deploy-openviking.sh` applied the retired `embedder-llamacpp` path instead of the live primary `embedder-qwen-cuda` path.
2. The script waited for `llamacpp-cuda-ov` but never applied its manifest.
3. The `openviking` pod mounted the `openviking-exporter` ConfigMap, but the script did not apply it.
4. `ov.conf` uses S3 AGFS, but the script described S3 credentials as optional and claimed local AGFS was canonical.
5. `ov.conf` depends on the HTTP vectordb, but the script applied OpenViking before applying `ov-vectordb`.
6. The API-key secret example did not include `user-api-key`, which the exporter requires.
7. Public ingress previously referenced a BasicAuth middleware, but IMPR-1007 Phase 4 resolved the posture to API-key-only and removed the middleware reference.
8. There was no `kustomization.yaml` that defined the canonical active manifest set.

## Files changed in this rebuild prep

- `viking/deploy-openviking.sh` now applies only the canonical active stack, in dependency order, and treats required secrets as required.
- `viking/manifests/kustomization.yaml` defines the active non-secret resource set.
- `viking/manifests/openviking-api-key.secret.yaml.example` now documents both `api-key` and `user-api-key`.

## Required untracked secrets

Create these from the corresponding `.example` files before a true rebuild:

- `viking/manifests/openviking-api-key.secret.yaml`
- `viking/manifests/openviking-s3-credentials.secret.yaml`

Do not commit the real secret files.

## Dry-run validation

From this repo:

```bash
bash -n viking/deploy-openviking.sh
kubectl kustomize viking/manifests >/tmp/openviking-rendered.yaml
kubectl --context=tailnet apply --dry-run=server -f /tmp/openviking-rendered.yaml
```

The server-side dry-run requires the cluster CRDs for Traefik and kube-prometheus-stack. It does not mutate the cluster.

## Rebuild sequence

Do not run this without an explicit go-ahead because it deletes/recreates live workloads.

1. Confirm recent backup/snapshot of OpenViking data and Garage bucket state.
2. Confirm live health and capture current resource versions:
   - `kubectl --context=tailnet -n viking get pods,deploy,svc,pvc,endpoints`
3. Apply secrets from local untracked files.
4. Apply the canonical stack:
   - `KUBECTL_CONTEXT=tailnet viking/deploy-openviking.sh`
5. Verify:
   - deployments ready: `ov-vectordb`, `embedder-qwen-cuda`, `llamacpp-cuda-ov`, `openviking`
   - services have endpoints: `ov-vectordb`, `embedder-qwen`, `llamacpp-vlm`, `openviking`
   - `/health` and `/ready` return success from inside the pod
   - `hermes mcp test openviking` succeeds after Hermes sees the endpoint
6. Only after verification, decide whether to remove retired rollback manifests/resources.

## Not included yet

This prep does not delete any live resource and does not remove dormant repo manifests such as old worker/coordinator/ROCm/retired embedder paths. Those should be handled in a follow-up cleanup PR after the canonical rebuild path is proven.
