# Round 4 — Config F (Qwen3-8B OPTIMIZED on manu/1080)
**Settings:** ctx=4096, q4_0 KV, parallel=8, flash-attn, batch=2048
**Time:** 9s | **Tokens out:** 265 | **Gen tok/s:** 33.2

## Output
- **User preferences/corrections**:  
  - Use tables for structured data, not bullet lists.  
  - Avoid summarizing actions at the end of responses.  
  - Fixed duplicate DCGM dashboards.  

- **Decisions and rationale**:  
  - Combined NVIDIA DCGM and AMD amdgpu_exporter into a single dashboard.  
  - Removed old DCGM dashboard from Helm values and init container.  
  - Deployed new dashboard as ConfigMap with sidecar label.  
  - Restarted Grafana to ensure only one DCGM dashboard remains.  

- **Technical facts about the project**:  
  - GPU exporters: NVIDIA DCGM (manu, wemby), AMD amdgpu_exporter (timmy).  
  - Dashboard location: gpu/dashboards/gpu-overview-dashboard.yaml.  
  - Grafana is managed via kube-prometheus-stack-values.yaml and init containers.  
  - Dashboards are deployed as ConfigMaps with sidecar labels.  

- **Patterns for future sessions**:  
  - Use tables for structured data.  
  - Avoid redundant summaries in responses.  
  - Focus on actionable updates without extra explanation.  
  - Ensure consistency in dashboard management (e.g., removing duplicates).
