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
| External consumers | **Active pulls (verified 2026-06-10) — 6 namespaces, 7 images, all from `~/code/dotfiles/hermes/k8s/manifests.yaml`:**<br>• `build` → `registry.nathanwhyte.dev/build-hook/api:latest` — source: [`nathanwhyte/build-hook`](https://github.com/nathanwhyte/build-hook) (Rust, public); **not checked out locally**<br>• `coach` → `registry.nathanwhyte.dev/coach/coach:latest` + `coach/scrub:release` — source: [`nathanwhyte/credit-coach`](https://github.com/nathanwhyte/credit-coach) (private; Bun frontend + Python FastAPI scrubber); **not checked out locally**<br>• `equal-risk` → `registry.nathanwhyte.dev/equal-risk/{math,rails}:test` — source: [`nathanwhyte/equal-risk-portfolio`](https://github.com/nathanwhyte/equal-risk-portfolio) (Ruby, public); **checked out at `~/code/equal-risk-portfolio/`** (own `build-docker.sh` + `k8s/`)<br>• `glossary` → `registry.nathanwhyte.dev/glossary/glossary:latest` — source: [`nathanwhyte/glossary`](https://github.com/nathanwhyte/glossary) (Elixir, public); **not checked out locally**<br>• `portfolio` → `registry.nathanwhyte.dev/portfolio/portfolio:latest` — **no GitHub source found** (checked `nathanwhyte` and `aclamant` orgs); Phoenix/Elixir per image config (`/app/bin/server`, `MIX_ENV=prod`); would need to be extracted from the image if rebuilt. **Not checked out locally.**<br><br>**Active pushes (in this repo):**<br>• `viking/deploy-openviking-parallel.sh` → `registry.nathanwhyte.dev/homelab` (OV worker/coordinator/merge images, **no live consumer** — worker/coordinator/merge all scaled to 0)<br>• `backlog/nanochat/build.sh` → `registry.nathanwhyte.dev/library` (nanochat training images, **no live consumer** — `backlog/nanochat/train-rocm-job.yaml` references `nanochat:rocm-v4` but the job itself is in `backlog/`, not deployed)<br><br>**Full per-image audit** (Cmd, Env, creation timestamps, recovery notes) in `HARBOR-CLI.md` § "Audit: who else pushes to this Harbor?".<br><br>**Notably absent:** no pulls from `harbor`, `viking`, `llama`, `grafana`, `hermes`, or any system namespace. They all use upstream registries (docker.io, ghcr.io, quay.io, nvcr.io, rancher). |

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
