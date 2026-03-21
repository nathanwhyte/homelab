# Round 1 — Config B
**Time:** 37s | **Tokens out:** 1023

## Output
# GPU Dashboards Abstract

This directory provides a unified Grafana dashboard for monitoring GPU metrics across heterogeneous hardware nodes. The primary configuration resides in `gpu-overview-dashboard.yaml`, which defines panels for temperature, utilization, memory, and power consumption. It queries Prometheus metrics from both NVIDIA DCGM and AMD amdgpu_exporter exporters located in the parent `gpu/` directory. Deployed via ConfigMap for auto-discovery by the Grafana sidecar, this setup supports mixed GPU nodes including timmy (AMD), manu, and wemby (NVIDIA). Duplicate dashboard configurations were resolved by removing the Helm chart in favor of this standalone ConfigMap approach.
