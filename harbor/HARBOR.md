# Harbor Container Registry

Private container registry running on the K3s cluster. Hosts the hermes-agent, ov-vectordb, and other cluster-built images.

## At a glance

| Property | Value |
|---|---|
| Endpoint | `https://registry.nathanwhyte.dev` |
| Namespace | `harbor` |
| Helm release | `harbor` (revision 3, upgraded 2026-06-10) |
| Chart / App | `harbor-1.19.1` / Harbor **2.15.1** (app components); DB and Redis pinned at v2.14.3 in `harbor-values.yaml` |
| Components | core, portal, registry, jobservice, database, redis, trivy, exporter (single replica each) |
| Storage | RWO Longhorn volumes — `harbor-registry-rwo` (50Gi, longhorn-harbor), `harbor-jobservice-rwo` (1Gi, longhorn-harbor); database (5Gi, longhorn-hdd), redis (5Gi, longhorn-ssd), trivy (5Gi, longhorn-ssd) |
| TLS | cert-manager DNS-01 via Cloudflare (`letsencrypt-prod` ClusterIssuer, `harbor-tls` Secret) |
| Ingress | Traefik with `harbor-no-limit` middleware (unlimited image push body size) |
| Auth | Local DB. Default admin: `admin` / `<CHANGE_ME>` (change after first login) |
| Project creation | Open (`projectCreationRestriction: "everyone"`) — public pull is anonymous, push always requires auth |
| Metrics | Prometheus endpoints on every component, port 8001, path `/metrics` |
| External consumers | (TBD — see `harbor/HARBOR-CLI.md` "Audit" section for the grep to run) |

## Quick start

```bash
# Log in
docker login registry.nathanwhyte.dev

# Push (project name required; `library/` is the default public project)
docker tag myapp:latest registry.nathanwhyte.dev/library/myapp:latest
docker push registry.nathanwhyte.dev/library/myapp:latest

# Pull
docker pull registry.nathanwhyte.dev/library/myapp:latest
```

Web UI: <https://registry.nathanwhyte.dev> — projects, repos, vulnerability scans, retention, robot accounts.

## Operations

| Task | Where |
|---|---|
| Install / upgrade | `harbor/deploy-harbor.sh` (auto-applies probe-timeout patch; pass `--skip-probe-patch` to opt out, `--diff` for `helm diff` preflight) |
| Component health, log tailing, cert rotation, GC, RWO migration history, day-2 procedures | `harbor/HARBOR-RUNBOOK.md` |
| `harbor-cli` install + project / robot / vulnerability / retention / GC / audit workflows | `harbor/HARBOR-CLI.md` |
| Configuration values (image tags, RWO claims, resources, middleware) | `harbor/harbor-values.yaml` |
| Traefik push-size middleware | `harbor/harbor-middleware.yaml` |
| Longhorn RWO PVCs (registry, jobservice) | `harbor/rwo-pvcs.yaml` |
| May 2026 RWX→RWO migration artifacts (frozen paper trail) | `harbor/rwo-migrators.yaml` + `harbor/RWO-MIGRATION.md` |
| cert-manager ClusterIssuer | `harbor/letsencrypt-issuer.yaml` |

## See also

- [[2026-06-10-harbor-2-14-audit|2026-06-10 Harbor 2.14 audit]] — drift checklist, harbor-cli surface, upgrade-path decision matrix
- [[IDEA-021-homelab-harbor-registry-refresh|IDEA-021]] — refresh + 2.15.x upgrade plan
- [goharbor.io/docs](https://goharbor.io/docs/) — upstream docs
- [goharbor/harbor](https://github.com/goharbor/harbor) — source
