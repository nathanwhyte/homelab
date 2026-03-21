# Round 1 — Config F (Qwen3-8B OPTIMIZED on manu/1080)
**Settings:** ctx=4096, q4_0 KV, parallel=8, flash-attn, batch=2048
**Time:** 15s | **Tokens out:** 144 | **Gen tok/s:** 33.2

## Output
```markdown
# Abstract: GPU Overview Dashboard

This dashboard provides a unified view of GPU metrics across NVIDIA and AMD hardware using Grafana. The `gpu-overview-dashboard.yaml` file defines a ConfigMap containing the Grafana dashboard JSON, with panels for temperature, utilization, memory, and power usage. It queries Prometheus via DCGM (NVIDIA) and amdgpu_exporter (AMD), dynamically identifying instances through variables like `$datasource` and `{{exported_instance}}`. The README outlines deployment via `kubectl apply`, and the dashboard is auto-discovered by Grafana sidecar. Previously, duplicate dashboards caused conflicts, which were resolved by removing the Helm chart.
```
