# 🐋 Kubernetes Homelab

Self-hosted [K3s](https://docs.k3s.io/) Kubernetes cluster running on 3
[Ubuntu Linux](https://ubuntu.com/server) nodes.

This cluster hosts web applications, databases, data backups, an image
registry, and much more.

> It's like AWS, if AWS was hosted entirely in my office, next to my cat's food bowl,
> running on old laptops and my gaming PC from high school.
>
> ~ Me, circa Today

## Services

### Kubernetes Dashboard

Kubernetes' own dashboard for cluster management.

- Deployed using [the official guide](https://kubernetes.io/docs/tasks/access-application-cluster/web-ui-dashboard/).

### Headlamp

A modern, extensible Kubernetes UI alternative to the official dashboard.

- Deployed via Helm with a Cloudflare Tunnel for secure access.

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
- Read more in the Harbor [README](./harbor/HARBOR.md).

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

- **Ollama** (timmy, RX 9070 XT): `ollama.llama.svc:11434` — primary inference for Claude Code, Hermes, and OpenWebUI; hosts gemma4:12b-it-qat (local) and glm-5.1:cloud (remote)
- **OV VLM** (manu, GTX 1080): `llamacpp-cuda-ov` in viking namespace — OpenViking vision/L0 generation; always on (steady-state replicas=1)
- **Embedder** (wemby, GTX 1060, CUDA): `embedder-llamacpp.viking.svc:8080` — nomic-embed-text f16 for vector embeddings
- Read more in [`llama/README.md`](./llama/README.md) and [`viking/`](./viking/).

### Hermes

AI agent with mem0 persistent memory (OpenViking knowledge-base tools), SSH terminal backend, and Cloudflare-exposed dashboard.

- Agent API on port 8642 (cluster-internal), dashboard on 9119 (exposed at `hermes.nathanwhyte.dev` via Cloudflare tunnel)
- Uses mem0 as memory provider (mem0-adapter sidecar translates Platform API → OSS API); OpenViking provides knowledge-base tools (`viking_*`)
- Read more in [`hermes/README.md`](./hermes/README.md).

### OpenViking

Hierarchical RAG engine with auto-generated L0/L1/L2 semantic indices over a filesystem-shaped knowledge tree.

- S3-backed file storage (Garage), HTTP vector DB, embedder on wemby (CUDA), VLM on manu (CUDA)
- API at `context.nathanwhyte.dev`, console at `viking.nathanwhyte.dev`
- Read more in [`viking/OPENVIKING.md`](./viking/OPENVIKING.md).

### Tailscale

WireGuard mesh for private admin/network access from off-LAN. All 3 nodes run Tailscale; manu and wemby are HA subnet routers advertising `192.168.1.0/24`.

- Read more in [`tailscale/README.md`](./tailscale/README.md).

## Other Technologies

### Databases

Postgres containers, for apps that need them, with Longhorn SSD storage
for replicated persistent storage.

### Pi-hole

> **Not currently deployed.** Network-wide ad and tracker blocking — planned for future deployment.

### Unbound

> **Not currently deployed.** In-house DNS resolution for speed and privacy — planned for future deployment.

### Cloudflare Tunnel

Secure public-facing access to services without exposing my home IP address or opening ports on my router _(for most services)_.

### Cloudflare DNS

For services that don't use Cloudflare Tunnel, Cloudflare's DNS _(plus router port-forwarding)_ allows for secure access via my domain.
