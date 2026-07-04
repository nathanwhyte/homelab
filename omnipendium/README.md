# Omnipendium — homelab deployment (PROJ-028 stage 1)

FastAPI + Postgres/pgvector knowledge base API ([TASK-1105](https://linear.app/nathanwhyte-code/issue/PRO-238/stage-1-deploy-omnipendium-service-to-homelab-k3s-task-1105)). Service code and Dockerfile live in `~/code/omnipendium`; this directory holds the cluster spec only.

## Layout

| File                                | Purpose                                                       |
| ----------------------------------- | ------------------------------------------------------------- |
| `namespace.yaml`                    | `omnipendium` namespace                                       |
| `postgres-pvc.yaml`                 | Longhorn 5Gi volume for Postgres                              |
| `postgres-deployment.yaml`          | Dedicated `pgvector/pgvector:pg16` (per-app postgres pattern) |
| `postgres-service.yaml`             | ClusterIP `omnipendium-db:5432`                               |
| `omnipendium-deployment.yaml`       | API pod; alembic migration initContainer runs before start    |
| `omnipendium-service.yaml`          | ClusterIP `omnipendium:8000`                                  |
| `omnipendium-nodeport-service.yaml` | LAN/Tailscale NodePort **31940** (OV 31933 pattern)           |
| `*.secret.yaml.example`             | Templates for the two required Secrets                        |

## Deploy

```bash
cd ~/code/homelab/omnipendium

# 1. Secrets (one-time) — copy examples, fill values, apply
cp omnipendium-db.secret.yaml.example omnipendium-db.secret.yaml
cp omnipendium-api-key.secret.yaml.example omnipendium-api-key.secret.yaml
# edit both files (openssl rand -hex 32 for the API key), then:
kubectl apply -f namespace.yaml
kubectl apply -f omnipendium-db.secret.yaml -f omnipendium-api-key.secret.yaml

# 2. Everything else
kubectl apply -f postgres-pvc.yaml -f postgres-deployment.yaml -f postgres-service.yaml
kubectl apply -f omnipendium-deployment.yaml -f omnipendium-service.yaml -f omnipendium-nodeport-service.yaml
```

## Image updates

```bash
cd ~/code/omnipendium
docker buildx build --platform linux/amd64 \
  -t registry.nathanwhyte.dev/omnipendium/omnipendium:latest \
  -t registry.nathanwhyte.dev/omnipendium/omnipendium:$(git rev-parse --short HEAD) --push .
# manifests track :latest, so a restart picks up the new image
kubectl rollout restart deployment/omnipendium -n omnipendium
```

## Embeddings

The API embeds entry text and search queries via ollama (`nomic-embed-text`, 768d — must match the corpus already loaded in the DB). The Deployment pins the full env set (`OMNIPENDIUM_EMBEDDING_PROVIDER/MODEL/DIMENSIONS`, `OMNIPENDIUM_OLLAMA_BASE_URL`) because the app's config validator refuses to boot on mismatched provider/model pairs.

The endpoint is the cluster ollama in the `llama` namespace, which does **not** ship embedding models by default — pull once before (or after) deploying:

```bash
kubectl exec -n llama deploy/ollama -- ollama pull nomic-embed-text
```

Failure modes while the model is missing: entry writes still succeed but log an `embedding_skipped` audit row (no vector stored), and `POST /v1/entries/search` returns 503. Once the model is pulled, backfill any gap from the service repo:

```bash
cd ~/code/omnipendium
uv run python _scripts/backfill_embeddings.py --dry-run   # list missing
uv run python _scripts/backfill_embeddings.py             # re-embed
```

Note: the in-cluster llama.cpp embedders (`embedder-qwen`, retired `embedder-llamacpp` in the `viking` namespace) speak the OpenAI `/v1` API and are not usable here — the service only implements ollama's `/api/embed`.

## Harbor pull auth

The `omnipendium` Harbor project is **private** (unlike `homelab`), so the Deployment references a `harbor-pull` imagePullSecret backed by a pull-only project robot (`robot$omnipendium+omnipendium-pull`, no expiry). To recreate it:

```bash
# Harbor UI: omnipendium project → Robot Accounts → new pull-only robot, or API POST /api/v2.0/robots
kubectl create secret docker-registry harbor-pull -n omnipendium \
  --docker-server=registry.nathanwhyte.dev \
  --docker-username='robot$omnipendium+omnipendium-pull' \
  --docker-password='<robot secret>'
```

## Verify

```bash
# On-LAN (any node IP) or via Tailscale subnet route
curl http://192.168.1.x:31940/health          # -> {"status":"ok"} (no auth needed)
curl -H "Authorization: Bearer $KEY" http://192.168.1.x:31940/stats
# Off-LAN via timmy's tailnet IP
curl http://100.95.215.105:31940/health
```

No Cloudflare tunnel, Ingress, or TLS for stage 1 — the stage-2 Slack bot uses socket mode (TASK-1106), which needs no public endpoint.

## Monitoring

The app exposes `prometheus-fastapi-instrumentator` metrics at `/metrics` (PRO-246). The path is on the API-key middleware's public allowlist so in-cluster Prometheus can scrape it unauthenticated — note this also makes `/metrics` reachable on NodePort 31940 (accepted for stage 1; LAN/Tailscale only).

| File                              | Purpose                                                                   |
| --------------------------------- | ------------------------------------------------------------------------- |
| `omnipendium-servicemonitor.yaml` | Scrapes the `omnipendium` ClusterIP Service, port `http`, `/metrics`, 30s |
| `omnipendium-alerts.yaml`         | `OmnipendiumDown`, `OmnipendiumPodRestarting`, `OmnipendiumHigh5xx`       |

The kube-prometheus-stack Prometheus runs with an empty `serviceMonitorSelector`, so the ServiceMonitor is picked up from the `omnipendium` namespace automatically (same pattern as `viking/manifests/openviking-servicemonitor.yaml`). The instrumentator groups status codes, so the 5xx alert matches `status="5xx"`.

```bash
kubectl apply -f omnipendium-servicemonitor.yaml -f omnipendium-alerts.yaml
# verify the target appears (Prometheus UI → Status → Targets) or:
curl http://192.168.1.x:31940/metrics | head
```
