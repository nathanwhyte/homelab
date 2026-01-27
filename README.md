# 🐋 Kubernetes Homelab

> It's like AWS, if AWS was hosted entirely in my office, next to my cat's food bowl,
> running on of old laptops and my gaming PC from high school.   - Me, circa Today

## Apps

### Equal Risk Portfolio Calculator - [equalriskportfolio.com](https://equalriskportfolio.com)

[View on GitHub](https://github.com/nathanwhyte/equal-risk-portfolio)

### Portfolio Website - [nathanwhyte.dev](https://www.nathanwhyte.dev)

[View on GitHub](https://github.com/nathanwhyte/nathanwhyte.dev)

- Basically my extended resume, with colors and better formatting.

### Build Hook

[View on GitHub](https://github.com/nathanwhyte/build-hook)

- Fills the role of GitHub actions on pushes to main, but for free.
- Triggers image builds and resource deployments for configured projects on request.
- Requests secured via bearer tokens passed as secrets to GitHub Actions workflows.
- Built using Rust's [axum](https://docs.rs/axum/latest/axum/index.html) framework.

### Personal Glossary

[View on GitHub](https://github.com/nathanwhyte/glossary)

- A knowledge base / second brain / notes store / wiki for things I always forget.
- Integrated search function across different notes, projects, tags, and topics.
- AI-powered notes review and topic synthesis.

## Services

### Kubernetes Dashboard

Kubernetes' own dashboard for cluster management.

- Deployed using [the official guide](https://kubernetes.io/docs/tasks/access-application-cluster/web-ui-dashboard/)

### Longhorn

Persistent storage solution for the entire cluster.

- Deployed using [Longhorn's helm install guide](https://longhorn.io/docs/1.10.1/deploy/install/install-with-helm/)
- Backups and redundancy on HDD, databases and caches on SSD/NVMe
- Successfully prevented me from accidentally erasing an entire hard drive full of family videos

### Grafana Suite

Mimicking [Grafana Cloud's Kubernetes Monitoring](https://grafana.com/docs/grafana-cloud/monitor-infrastructure/kubernetes-monitoring/intro-kubernetes-monitoring/) without actually using Grafana Cloud.

- Prometheus metrics collection and Grafana frontend via [kube-prometheus](https://github.com/prometheus-operator/kube-prometheus)
- Alloy for log collection via [k8s-monitoring-helm](https://github.com/grafana/k8s-monitoring-helm)
- Loki for log aggregation via [Loki's official helm chart](https://github.com/grafana/loki/tree/main/production/helm/loki)

### Garage

S3-compatible object storage engine.

- Deployed based on [Garage's official guide](https://garagehq.deuxfleurs.fr/documentation/cookbook/kubernetes/)

## Other Technologies

### Cloudflare Tunnel

### Cloudflare DNS
