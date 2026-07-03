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
  -t registry.nathanwhyte.dev/homelab/omnipendium:$(git rev-parse --short HEAD) --push .
# bump the two image: tags in omnipendium-deployment.yaml, then
kubectl apply -f omnipendium-deployment.yaml
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
