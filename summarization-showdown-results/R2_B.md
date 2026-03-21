# Round 2 — Config B
**Time:** 39s | **Tokens out:** 1024

## Output
*   **Issue:** llama.cpp deployment on "timmy" crashing due to OOM after ~2h (RSS 11.2Gi > 10Gi limit).
*   **Root Cause:** q8_0 KV cache precision too high for Qwen3.5-27B + 8192 context within container memory limit.
*   **Resolution:** Switched KV cache to q4_0 (saves ~2GB, expected RSS ~9.1Gi). Context size maintained at 8192. Rejected raising memory limit.
*   **Hardware:** Timmy node: 32GB system
