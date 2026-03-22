# Round 1 — Config D
**Time:** 16s | **Tokens out:** 0

## Output
**File to create:** `/home/natew/code/homelab/gpu/dashboards/.abstract.md`

**Summary:**

```markdown
# GPU Dashboards

Unified GPU monitoring dashboard for the homelab Kubernetes cluster, combining NVIDIA and AMD metrics across three GPU nodes (timmy/RX 9070 XT, manu/GTX 1080, wemby/GTX 1060).

## Key Components

- **gpu-overview-dashboard.yaml**: Grafana ConfigMap dashboard (450 lines) with temperature, utilization, memory, and power panels. Queries both NVIDIA DCGM exporter (`gpu/nvidia/`) and AMD amdgpu_exporter (`gpu/amd/`). Legend format: `{{exported_instance}} - {{modelName}}`.
- **README.md**: Simple kubectl deployment instructions.

## Relationships

Auto-discovered by Grafana's ConfigMap sidecar. Replaces prior Helm-installed DCGM dashboard, eliminating duplicate dashboards. Datasource variable allows flexible scrape target configuration.
```

The abstract explains **purpose** (unified GPU monitoring), **key files** (the dashboard ConfigMap and README), and **relationships** (Grafana discovery, vendor exporters, multi-node scope, prior duplicate dashboard resolution).
