# Round 2 — Config E (Qwen3-8B on manu/1080)
**Time:** 6s | **Tokens out:** 194 | **Gen tok/s:** 33.4

## Output
- **Key decisions**:  
  - Chose q4_0 KV cache to stay within 10Gi memory limit instead of increasing container limits.  
  - Reduced RSS from ~11.2Gi to ~9.1Gi.  

- **Important facts**:  
  - Timmy has 32GB RAM, 16GB VRAM (9070 XT).  
  - Model loads into VRAM; KV cache spills to system RAM.  
  - 10Gi container limit restricts system RAM usage (KV + overhead), not VRAM.  

- **Current state**:  
  - Deployment stable after redeploy with q4_0 KV.  
  - 45+ minutes uptime, no OOM errors.  

- **Pending actions**:  
  - Note updated in deployment annotation for future reference.  

- **Errors/blockers**:  
  - None; issue resolved.
