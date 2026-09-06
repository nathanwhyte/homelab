# 🐋 Kubernetes Homelab

Self-hosted [K3s](https://docs.k3s.io/) Kubernetes cluster running on 3
[Ubuntu Linux](https://ubuntu.com/server) nodes.

This cluster hosts web applications, databases, data backups, an image
registry, and much more.

> It's like AWS, if AWS was hosted entirely in my office, next to my cat's food bowl,
> running on old laptops and my gaming PC from high school.
>
> ~ Me, circa Today

## Deployment scripts

Grafana, Harbor, OpenWebUI, Dashboard, Headlamp and Garage reuse each release's
deployed Helm chart version when applying values. Use `--dry-run` to simulate
their Helm operations before deploying. See [version reuse and verification](scripts/helm-deploy.md)
for first-install behavior, Garage's local chart requirement and test evidence.

## Services

### Kubernetes Dashboard

Kubernetes' own dashboard for cluster management.

- Deployed using [the official guide](https://kubernetes.io/docs/tasks/access-application-cluster/web-ui-dashboard/).

### Headlamp

> **Torn down 2026-07-02.** A modern, extensible Kubernetes UI alternative to the official
> dashboard. Manifests retained for reference — see [`headlamp/TORN-DOWN.md`](./headlamp/TORN-DOWN.md).

### Longhorn

Persistent storage solution for the entire cluster.

- Deployed using [Longhorn's kubectl install guide](https://longhorn.io/docs/1.10.1/deploy/install/install-with-kubectl/).
- Backups and redundancy on HDD, databases and caches on SSD/NVMe.
- Successfully prevented me from accidentally erasing an entire hard drive full of family videos.

### Grafana Suite

Mimicking [Grafana Cloud's Kubernetes Monitoring](https://grafana.com/docs/grafana-cloud/monitor-infrastructure/kubernetes-monitoring/intro-kubernetes-monitoring/) without actually using Grafana Cloud.

- Prometheus metrics collection and Grafana frontend via [kube-prometheus](https://github.com/prometheus-operator/kube-prometheus).
- Alloy for log collection via [k8s-monitoring-helm](https://github.com/grafana/k8s-monitoring-helm).
- Loki for log aggregation via [Loki's official helm chart](https://github.com/grafana/loki/tree/main/production/helm/loki).

### Harbor

Container image registry with a nice web interface.

- Deployed using [Harbor's official helm chart](https://github.com/goharbor/harbor-helm)
- Public image repositories for images used in the cluster.
- Read more in the Harbor [README](./harbor/harbor.md).

### OpenWebUI

Web interface for interacting with local LLMs via Ollama.

- Deployed with Helm in the `openwebui` namespace.
- Serves as the primary chat interface for the local Ollama instance.

### SearXNG

Privacy-respecting metasearch engine, used as the web search backend for OpenWebUI.

- Lightweight deployment with custom settings via ConfigMap.
- Aggregates results from multiple search engines without tracking.

### Garage

S3-compatible object storage engine.

- Deployed based on [Garage's official guide](https://garagehq.deuxfleurs.fr/documentation/cookbook/kubernetes/).
- Custom manager container for bucket management with the AWS S3 CLI.

### Llama

Internal OpenAI-compatible LLM endpoints backed by Ollama and `llama.cpp`.

- **Ollama** (timmy, RX 9070 XT): `ollama.llama.svc:11434` — primary inference for Claude Code and OpenWebUI; hosts gemma4:12b-it-qat (local) and glm-5.1:cloud (remote)
- **OV VLM** (manu, GTX 1080): `llamacpp-cuda-ov` in viking namespace — **failover only** since the 2026-07-05 cloud cutover (OV's primary VLM is cloud via chat-ollama — `gemma4:31b-cloud`, non-thinking, settled 2026-07-06); replicas=1 during the soak window, retirement candidate
- **Embedder** (timmy, RX 9070 XT, ROCm): `embedder-qwen.viking.svc:8080` — Qwen3-Embedding-4B Q8_0 (2560-dim) for vector embeddings (Deployment `embedder-qwen`, migrated back to timmy 2026-07-06 after wemby's failing charging cable/port kept hard-dropping the node; co-resides with Ollama on the 9070 XT, ~5 GB; `embedder-qwen-cuda` on wemby retained as `replicas=0` rollback); legacy `embedder-llamacpp` on wemby deleted 2026-07-04 (768-dim rollback path gone); the mem0 stack that pointed at it was torn down 2026-07-02 (see `mem0/TORN-DOWN.md`)
- Read more in [`llama/README.md`](./llama/README.md) and [`viking/`](./viking/).

### Hermes

> **Retired 2026-07-16** — the `hermes` namespace (agent, jump terminal, secrets, PVCs, Cloudflare
> route) was deleted in full. Its mem0 memory backend was itself torn down 2026-07-02 (see
> [`mem0/TORN-DOWN.md`](./mem0/TORN-DOWN.md)); OpenViking's knowledge-base tooling has since covered
> the persistent-memory use case, so the project was retired rather than migrated to a new memory
> provider. Manifests retained for reference — see [`hermes/RETIRED.md`](./hermes/RETIRED.md).

AI agent that used mem0 for persistent memory plus OpenViking knowledge-base tools, an SSH terminal backend, and a Cloudflare-exposed dashboard.

- Read more in [`hermes/README.md`](./hermes/README.md).

### OpenViking

Hierarchical RAG engine with auto-generated L0/L1/L2 semantic indices over a filesystem-shaped knowledge tree.

- S3-backed file storage (Garage), HTTP vector DB, embedder on timmy (RX 9070 XT, ROCm, since 2026-07-06), cloud VLM primary via chat-ollama (manu CUDA as failover since 2026-07-05)
- API + web console at `context.nathanwhyte.dev` (console at `/studio/`, root-key login; the old `viking.nathanwhyte.dev` ov-console was removed)
- Endpoint tiers: in-cluster `openviking.viking.svc:1933` · LAN `192.168.1.19:31933` · Tailscale `100.95.215.105:31933` · public `context.nathanwhyte.dev` (Cloudflare tunnel)
- Read more in [`viking/openviking.md`](./viking/openviking.md).

### Tailscale

WireGuard mesh for private admin/network access from off-LAN. All 3 nodes run Tailscale; manu and wemby are HA subnet routers advertising `192.168.1.0/24`.

- Read more in [`tailscale/README.md`](./tailscale/README.md).

### Omnipendium

Knowledge-base API (FastAPI + Postgres/pgvector) with a Slack bot frontend (PROJ-028 stage 1).

- Read more in [`omnipendium/README.md`](./omnipendium/README.md).

### Copyparty

Self-hosted file server for media and general file sharing.

- Longhorn HDD-backed storage plus an NFS PV for the media library.

### Syncthing

> **Torn down 2026-07-25** — namespace deleted (both Services, the scaled-to-0 Deployment, and the
> 50 GiB `syncthing-data` PVC). The vault syncs through git; no backup was needed. Manifests
> retained for reference — see [`syncthing/TORN-DOWN.md`](./syncthing/TORN-DOWN.md).

Always-on Syncthing peer for Obsidian/compendium vault sync across MacBook(s), iPad, and phone.

### Mem0

> **Torn down 2026-07-02** — namespace deleted, final Postgres dump archived locally. Manifests
> retained for reference — see [`mem0/TORN-DOWN.md`](./mem0/TORN-DOWN.md).

Self-hosted Mem0 memory backend for Hermes (server + Platform→OSS adapter + Postgres/pgvector).

## Other Technologies

### Databases

Postgres containers, for apps that need them, with Longhorn SSD storage
for replicated persistent storage.

### Pi-hole

Network-wide ad and tracker blocking, **deployed on all three hosts** — wemby
(`192.168.1.9`), manu (`192.168.1.10`), timmy (`192.168.1.19`). Each is an
independent instance; clients receive all three, so **all three must answer
identically**.

Since 2026-08-24 the compose files and the internal DNS record set are
version-controlled here: [`network/pihole/`](network/pihole/README.md). Internal
names (`registry`, `k8s`, `longhorn.nathanwhyte.dev`) are served from
`FTLCONF_dns_hosts` in each host's compose file — **not** from public DNS, which
no longer carries them.

### Unbound

In-house recursive DNS, **deployed alongside each Pi-hole** on its own private
`dns-network` bridge. Pi-hole forwards to it as `unbound#53`. The three do not
share a recursive resolver, so unbound is not a hidden single point of failure.
Full recursion from the root hints with local DNSSEC validation — no public
upstream resolver is in the path.

See [`network/pihole/README.md`](network/pihole/README.md) for the operating
procedure and [`tailscale/README.md`](tailscale/README.md) for the measured DNS
failover behaviour.

### Cloudflare Tunnel

Secure public-facing access to services without exposing my home IP address or opening ports on my router _(for most services)_.

### Cloudflare DNS

For services that don't use Cloudflare Tunnel, Cloudflare's DNS _(plus router port-forwarding)_ allows for secure access via my domain.
