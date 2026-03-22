# Round 1 — Config A
**Time:** 6s | **Tokens out:** 196

## Output
### .abstract.md

**Purpose:** The `gpu/dashboards/` directory contains configuration files for monitoring GPU metrics using Grafana dashboards. It provides a unified view of temperature, utilization, memory, and power metrics for both NVIDIA and AMD GPUs.

**Key Files:**
  - **`gpu-overview-dashboard.yaml`**: A ConfigMap file that defines the Grafana dashboard JSON, including panels for various GPU metrics. It queries Prometheus metrics from DCGM (NVIDIA) and amdgpu_exporter (AMD).
  - **`README.md`**: Provides deployment instructions using `kubectl apply -f ...`.

**Component Relationships:** The dashboards are auto-discovered by the Grafana sidecar and display metrics from GPU nodes named timmy, manu, and wemby. This setup replaces duplicate DCGM dashboards previously managed by Helm, resolving issue 12239.
