# Round 1 — Config A
**Time:** 7s | **Tokens out:** 274

## Output
# .abstract.md

## GPU Dashboards Documentation Summary

The `gpu/dashboards/` directory contains files for monitoring GPU metrics using Grafana dashboards. The primary purpose is to provide a visual overview of GPU temperature, utilization, memory, and power usage across different nodes.

### Key Files:
- **gpu-overview-dashboard.yaml**: A ConfigMap file (450 lines) that defines the Grafana dashboard JSON with panels for monitoring various GPU metrics. It queries Prometheus metrics from DCGM (NVIDIA) and amdgpu_exporter (AMD), using legends and variables to dynamically display data.
- **README.md**: A brief file (12 lines) outlining deployment instructions, specifically `kubectl apply -f ...`.

### Component Relationships:
The dashboard is designed to work with GPU nodes named timmy (RX 9070 XT), manu (GTX 1080), and wemby (GTX 1060). It auto-discovered by the Grafana sidecar, eliminating the need for manual configuration. The directory integrates with vendor-specific exporters located in `gpu/nvidia/` and `gpu/amd/`, resolving previous issues with duplicate DCGM dashboards by removing the Helm-based configuration (issue #12239).
