# 🐋 Kubernetes Homelab

Self-hosted [K3s](https://docs.k3s.io/) Kubernetes cluster running on 4
[Ubuntu Linux](https://ubuntu.com/server) nodes.

This cluster hosts web applications, databases, data backups, an image
registry, and much more.

> It's like AWS, if AWS was hosted entirely in my office, next to my cat's food bowl,
> running on of old laptops and my gaming PC from high school.
>
> ~ Me, circa Today

## Apps

### Equal Risk Portfolio Calculator - [equalriskportfolio.com](https://equalriskportfolio.com)

- Used by financial advisors to build portfolios that balance risk across asset classes.

[View on GitHub](https://github.com/nathanwhyte/equal-risk-portfolio)

### Portfolio Website - [nathanwhyte.dev](https://www.nathanwhyte.dev)

[View on GitHub](https://github.com/nathanwhyte/nathanwhyte.dev)

- An extension of my resume, with colors and better formatting.
- Built using the [Phoenix Framework](https://www.phoenixframework.org/) for Elixir and [TailwindCSS](https://tailwindcss.com/).

### Build Hook

[View on GitHub](https://github.com/nathanwhyte/build-hook)

- Fills the role of GitHub actions on pushes to main, but for free.
- Triggers image builds and resource deployments for configured projects on request.
- Requests secured via bearer tokens passed as secrets to GitHub Actions workflows.
- Built using Rust's [axum](https://docs.rs/axum/latest/axum/index.html) web framework with image builds running in a [Docker BuildKit](https://docs.docker.com/build/buildkit/configure/) container.

### Glossary

[View on GitHub](https://github.com/nathanwhyte/glossary)

- A knowledge base / second brain / notes store / personal wiki for things I always forget.
- Integrated search function across different notes, projects, tags, and topics.
- AI-powered notes review and topic synthesis.
- Built using Phoenix's [LiveView](https://hexdocs.pm/phoenix_live_view/welcome.html) for reactivity and [TailwindCSS](https://tailwindcss.com/) for styling.

## Services

### Kubernetes Dashboard

Kubernetes' own dashboard for cluster management.

- Deployed using [the official guide](https://kubernetes.io/docs/tasks/access-application-cluster/web-ui-dashboard/).

### Longhorn

Persistent storage solution for the entire cluster.

- Deployed using [Longhorn's kubectl install guide](https://longhorn.io/docs/1.10.1/deploy/install/install-with-kubectl/).
- Backups and redundancy on HDD, databases and caches on SSD/NVMe.
- Successfully prevented me from accidentally erasing an entire hard drive
  full of family videos.

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

### Garage

S3-compatible object storage engine.

- Deployed based on [Garage's official guide](https://garagehq.deuxfleurs.fr/documentation/cookbook/kubernetes/).
- Custom manager container for bucket management with the AWS S3 CLI.

## Other Technologies

### Databases

Postgres containers, for apps that need them, with Longhorn SSD storage
for replicated persistent storage.

### Pi-hole

Network-wide ad and tracker blocking with nice built-ins and easy configuration.

### Unbound

For in-house DNS resolution for speed and privacy

### Cloudflare Tunnel

Secure public-facing access to services without exposing my home IP address or
opening ports on my router _(for most services)_.

### Cloudflare DNS

For services that don't use Cloudflare Tunnel, Cloudflare's DNS
_(plus router port-forwarding)_ allows for secure access via my domain,
with SSL as an additional layer.
