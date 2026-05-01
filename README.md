# 🐋 Kubernetes Homelab

Self-hosted [K3s](https://docs.k3s.io/) Kubernetes cluster running on 3
[Ubuntu Linux](https://ubuntu.com/server) nodes.

This cluster hosts web applications, databases, data backups, an image
registry, and much more.

> It's like AWS, if AWS was hosted entirely in my office, next to my cat's food bowl,
> running on of old laptops and my gaming PC from high school.
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

### Homepage

A clean, customizable personal dashboard for accessing internal services.

- Static configuration via ConfigMap with service bookmarks and status widgets.
- Exposed through a Cloudflare Tunnel.

### Excalidraw

Virtual whiteboard for sketching diagrams and brainstorming.

- Lightweight, collaborative drawing tool deployed in-cluster.

### IT-tools

Collection of handy developer utilities (formatters, converters, encoders, etc.).

- Single-container deployment for quick access to common tools.

### Stirling PDF

Web-based PDF manipulation tool (merge, split, rotate, compress, convert, etc.).

- Runs in a container with all PDF processing handled locally.

### Garage

S3-compatible object storage engine.

- Deployed based on [Garage's official guide](https://garagehq.deuxfleurs.fr/documentation/cookbook/kubernetes/).
- Custom manager container for bucket management with the AWS S3 CLI.

### Llama

Internal OpenAI-compatible LLM endpoints backed by Ollama and `llama.cpp`.

- **Ollama** (timmy, RX 9070 XT): `ollama.llama.svc:80` — primary inference for Claude Code and OpenWebUI; hosts models like Qwen, Gemma, Mistral
- **OV LLM** (manu, GTX 1080): `llamacpp-cuda-llm.viking.svc:80` — OpenViking VLM inference; ROCm hot standby on timmy scaled to 0
- **Embedder** (timmy, CPU): `embedder-llamacpp.viking.svc:8080` — nomic-embed-text f16 for vector embeddings
- Read more in [`llama/README.md`](./llama/README.md) and [`viking/`](./viking/).

## Other Technologies

### Databases

Postgres containers, for apps that need them, with Longhorn SSD storage
for replicated persistent storage.

### Pi-hole

Network-wide ad and tracker blocking with nice built-ins and easy configuration.

### Unbound

For in-house DNS resolution for speed and privacy

### Cloudflare Tunnel

Secure public-facing access to services without exposing my home IP address or opening ports on my router _(for most services)_.

### Cloudflare DNS

For services that don't use Cloudflare Tunnel, Cloudflare's DNS _(plus router port-forwarding)_ allows for secure access via my domain.
