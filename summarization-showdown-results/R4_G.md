# Round 4 — Config G (Qwen3-8B OPTIMIZED on manu/1080)
**Settings:** ctx=4096, q4_0 KV, parallel=8, flash-attn, batch=2048
**Time:** 9s | **Tokens out:** 278 | **Gen tok/s:** 33.3

## Output
- **User preferences/corrections**:  
  - Use tables for structured data, not bullet lists.  
  - Avoid summarizing actions at the end of responses; provide only the result.  
  - Fixed duplicate DCGM dashboards by removing old Helm download and using a ConfigMap with sidecar label.  

- **Decisions and rationale**:  
  - Combined NVIDIA DCGM and AMD amdgpu_exporter data into one dashboard for unified monitoring.  
  - Deployed GPU inventory as a table for clarity and structured presentation.  
  - Removed outdated DCGM dashboard via Helm values and init container to prevent duplication.  

- **Technical facts about the project**:  
  - NVIDIA DCGM exporter is running on nodes "manu" and "wemby".  
  - AMD amdgpu_exporter is running on node "timmy".  
  - A combined GPU dashboard was created in `gpu/dashboards/gpu-overview-dashboard.yaml`.  
  - The old DCGM dashboard was sourced from Helm downloading a public Grafana dashboard.  

- **Patterns for future sessions**:  
  - Use tables for structured data (e.g., inventory, metrics).  
  - Avoid redundant summaries; focus on delivering the result directly.  
  - Address user feedback immediately to align with their workflow and preferences.
