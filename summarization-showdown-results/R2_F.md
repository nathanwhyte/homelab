# Round 2 — Config F (Qwen3-8B OPTIMIZED on manu/1080)
**Settings:** ctx=4096, q4_0 KV, parallel=8, flash-attn, batch=2048
**Time:** 5s | **Tokens out:** 132 | **Gen tok/s:** 33.2

## Output
- **Key decisions**: Reduced KV cache precision to q4_0 to stay within memory limits; kept context size at 8192.  
- **Important facts**: Timmy has 32GB system RAM, 16GB VRAM; model loads into VRAM, KV cache spills to system RAM.  
- **Current state**: Deployment stable post-redeployment; pod uptime >45 minutes.  
- **Pending actions**: Monitor memory usage over time; consider future scaling.  
- **Errors/blockers**: OOM killer previously caused crashes due to memory limits; resolved by adjusting KV cache precision.
