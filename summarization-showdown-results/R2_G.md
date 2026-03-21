# Round 2 — Config G (Qwen3-8B OPTIMIZED on manu/1080)
**Settings:** ctx=4096, q4_0 KV, parallel=8, flash-attn, batch=2048
**Time:** 5s | **Tokens out:** 131 | **Gen tok/s:** 33.2

## Output
- **Key decisions**: Reduced KV cache precision to q4_0 to avoid OOM, retained context size at 8192  
- **Important facts**: Timmy has 32GB RAM, 16GB VRAM; model loads into VRAM, KV cache uses system RAM; container memory limit is 10Gi  
- **Current state**: Pod stable after redeploy with q4_0 KV cache, ~45 minutes uptime  
- **Pending actions**: Documented system RAM/VRAM usage for future reference  
- **Errors/blockers**: None - issue resolved via KV precision adjustment
