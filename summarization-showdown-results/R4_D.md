# Round 4 — Config D
**Time:** 20s | **Tokens out:** 0

## Output
| Component | Location | Status |
|-----------|----------|--------|
| **AMD GPU Exporter** | kube-system (timmy node) | Running (1/1) |
| **NVIDIA DCGM Exporter** | kube-system | Service exists on port 9400 |
| **AMD GPU Dashboard** | Grafana | amdgpu-dashboard ConfigMap |
| **NVIDIA DCGM Dashboard** | Grafana | nvidia-dcgm-dashboard ConfigMap |
| **Combined GPU Dashboard** | Grafana | gpu-overview-dashboard ConfigMap |

`★ Insight ─────────────────────────────────────`
The setup includes three layers: (1) **exporters** (amdgpu_exporter on timmy's AMD GPU, DCGM for NVIDIA GPUs on manu/wemby), (2) **Prometheus scrape targets** discovering these via Kubernetes service discovery, and (3) **Grafana dashboards** consuming the metrics. The combined gpu-overview-dashboard merges both vendor metrics into a single view for easy comparison across your three GPU nodes.
`─────────────────────────────────────────────────`
