# Round 1 — Config G (Qwen3-8B OPTIMIZED on manu/1080)
**Settings:** ctx=4096, q4_0 KV, parallel=8, flash-attn, batch=2048
**Time:** 15s | **Tokens out:** 142 | **Gen tok/s:** 33.5

## Output
```markdown
# Abstract: GPU Overview Dashboard

This dashboard provides a unified view of GPU metrics across multiple nodes using Grafana, integrating data from both NVIDIA DCGM and AMD amdgpu_exporter. The `gpu-overview-dashboard.yaml` defines the ConfigMap containing the dashboard JSON, with panels for temperature, utilization, memory, and power usage. It dynamically queries Prometheus via variable `$datasource`, supporting different GPU models across nodes: RX 9070 XT, GTX 1080, and GTX 1060. The dashboard is auto-discovered by Grafana sidecar and resolves duplicate DCGM dashboards previously deployed via Helm.
```
