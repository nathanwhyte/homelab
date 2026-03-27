# Ollama Kubernetes Deployment Design

**Date:** 2026-03-27
**Status:** Approved
**Namespace:** `llama`
**Node:** timmy (RX 9070 XT, 16GB VRAM)

## Overview

Deploy Ollama as a Kubernetes Deployment on timmy's RX 9070 XT, replacing the current bare-metal systemd setup. Includes a Prometheus exporter sidecar, ConfigMap-driven Modelfile management, and external access via Traefik ingress with API key authentication on `robots.nathanwhyte.dev`.

## Architecture

### Deployment

- **Name:** `ollama`
- **Namespace:** `llama`
- **Replicas:** 1
- **Strategy:** `Recreate` (GPU exclusive — cannot run two instances)
- **revisionHistoryLimit:** 1

**Node selection:**
- `kubernetes.io/hostname: timmy`
- `gpu.vendor: amd`

**GPU resources:**
- `amd.com/gpu: "1"` in both requests and limits

**Security:**
- `privileged: true` (required for ROCm GPU access, matches existing llamacpp pattern)

**Shared memory:**
- `/dev/shm` emptyDir, `medium: Memory`, `sizeLimit: 4Gi`

**Model storage:**
- Existing PVC `llama-model-cache` (20Gi, longhorn-ssd) mounted at `/root/.ollama`

### Containers

| Container | Image | Port | Purpose |
|-----------|-------|------|---------|
| `ollama` | `ollama/ollama:rocm` | 11434 | Ollama server |
| `ollama-exporter` | `python:3.12-slim` (runs `ollama-exporter.py`) | 9111 | Prometheus metrics sidecar |

### Environment Variables (ollama container)

| Variable | Value | Rationale |
|----------|-------|-----------|
| `OLLAMA_HOST` | `0.0.0.0` | Listen on all interfaces for K8s networking |
| `OLLAMA_FLASH_ATTENTION` | `1` | Required for KV cache quantization |
| `OLLAMA_KV_CACHE_TYPE` | `q8_0` | Cuts KV memory ~50% with negligible quality loss |
| `OLLAMA_KEEP_ALIVE` | `-1` | Keep model loaded indefinitely (dedicated GPU) |
| `OLLAMA_NUM_PARALLEL` | `2` | Two concurrent requests |
| `OLLAMA_MAX_LOADED_MODELS` | `1` | 16GB VRAM can only fit one model |
| `HIP_VISIBLE_DEVICES` | `0` | Target the discrete GPU |

### Probes

- **Startup probe:** TCP on port 11434, `periodSeconds: 10`, `failureThreshold: 120` (generous — model loading can take time)
- **Readiness probe:** TCP on port 11434, `initialDelaySeconds: 5`, `periodSeconds: 10`, `failureThreshold: 12`

## Model Loading

### Mechanism

The Ollama container entrypoint is overridden to:
1. Start `ollama serve` in the background
2. Run `/scripts/startup.sh` which waits for Ollama to be healthy, pulls stock models, and creates custom models from Modelfiles
3. Wait on the Ollama process

### Startup Script (ConfigMap: `ollama-startup`)

Mounted at `/scripts/startup.sh`. Logic:
1. Poll `localhost:11434` until healthy
2. For each Modelfile in `/modelfiles/`, check if the model already exists via `ollama list`
3. If the base model isn't pulled yet, run `ollama pull <base>`
4. Run `ollama create <name> -f /modelfiles/<name>` for custom models
5. Models persist on the PVC, so subsequent restarts skip downloads

### Modelfiles (ConfigMap: `ollama-modelfiles`)

Mounted at `/modelfiles/`. Each key is a model name, value is the Modelfile content. Initial model:

```
qwen35-claude: |
  FROM qwen3.5:9b-q4_K_M
  PARAMETER num_ctx 65536
  SYSTEM "/no_think"
```

Additional Modelfiles can be added to the ConfigMap and applied by restarting the pod.

## Networking

### Service

**Name:** `ollama`, **Type:** ClusterIP

| Port | TargetPort | Name | Purpose |
|------|------------|------|---------|
| 80 | 11434 | http | Ollama API (internal cluster access) |
| 9111 | 9111 | metrics | Prometheus exporter |

### Ingress

Traefik IngressRoute on `robots.nathanwhyte.dev`:
- **Route:** ``Host(`robots.nathanwhyte.dev`)`` with path prefix `/`
- **TLS:** Let's Encrypt (via existing Traefik cert resolver)
- **Auth:** Middleware checking `Authorization: Bearer <key>` header against a value from K8s Secret `ollama-api-key`

### Prometheus Integration

Update `grafana/helm/kube-prometheus-stack-values.yaml`:
- Replace bare-metal scrape target `192.168.1.19:9111` with K8s service `ollama.llama.svc:9111`

## File Layout

| File | Contents |
|------|----------|
| `llama/ollama-deployment.yaml` | Deployment (ollama + exporter sidecar), Service |
| `llama/ollama-configmap.yaml` | Startup script ConfigMap + Modelfiles ConfigMap |
| `llama/ollama-ingress.yaml` | Traefik IngressRoute + API key middleware + Secret |

**Reused existing resources:**
- `llama/namespace.yaml` — `llama` namespace
- `llama/pvc.yaml` — 20Gi `llama-model-cache` PVC on longhorn-ssd

**Updated resources:**
- `grafana/helm/kube-prometheus-stack-values.yaml` — Prometheus scrape target update

## Exporter Sidecar

The `ollama-exporter` container runs the existing `llama/ollama-exporter.py` script. It:
- Connects to Ollama at `localhost:11434` (pod-local, no service discovery needed)
- Exposes Prometheus metrics on port 9111
- Tracks: model status, VRAM usage, tokens/second, request counts

The exporter script is mounted from the `ollama-startup` ConfigMap (alongside the startup script) into the sidecar container running `python:3.12-slim`. This keeps it version-controlled and easy to update without building a custom image.

## Migration Notes

After deploying to K8s:
1. Stop the systemd Ollama service on timmy: `sudo systemctl disable --now ollama ollama-exporter`
2. The K8s deployment takes over GPU ownership
3. Update any scripts referencing `192.168.1.19:11434` to use `ollama.llama.svc:80` (cluster-internal) or `robots.nathanwhyte.dev` (external)
