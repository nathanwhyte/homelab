# Model Benchmark Report: Qwen3.5-27B Q3_K_M

**Date:** 2026-03-20
**Infrastructure:** RX 9070 XT (16GB VRAM) on timmy, llama.cpp server-rocm
**Model:** Qwen3.5-27B Q3_K_M (13.5 GB) via `ghcr.io/ggml-org/llama.cpp:server-rocm`
**Config:** `--parallel 1`, `--ctx-size 8192`, `--cache-type-k q8_0`, `--cache-type-v q8_0`

---

## Results

| Round | Difficulty | Topic | Time | Tokens | Quality | Correct |
|-------|-----------|-------|------|--------|---------|---------|
| 1 | Easy | Mutex vs semaphore | 41s | 1,149 | 8/10 | Yes |
| 2 | Moderate | Go concurrency bugs | 65s | 1,839 | 9/10 | Yes |
| 3 | Moderate | K8s OOMKilled debugging | 112s | 3,155 | 9/10 | Yes |
| 4 | Hard | Bash find/xargs safety | 102s | 2,856 | 9/10 | Yes |
| 5 | Hard | FastAPI multi-worker cache | 175s | 4,882 | 8/10 | Yes |
| 6 | Expert | Rust/tokio CPU starvation | 209s | 5,792 | 7/10 | No |
| 7 | Expert | PostgreSQL streak query | 61s | 1,708 | 8/10 | Yes |
| **Avg** | | | **109s** | **3,054** | **8.3/10** | **6/7** |

**Total time:** 765s (~13 min) | **Total tokens:** 21,381

---

## Strengths

- **Deep, methodical analysis** — the thinking mode produces thorough walkthroughs that cover edge cases other models miss. The K8s debugging answer (R3) covered cgroup v1/v2, language-specific profiling, and VPA recommendations.
- **Catches subtle issues** — identified the flag injection risk in bash filenames (R4) that neither Mistral nor Claude flagged. Found all three Go concurrency bugs (R2) with an idiomatic channel-based alternative.
- **Correct SQL algorithms** — used the proper gap-and-island technique (R7) for consecutive streak detection, producing a correct query in just 61 seconds.
- **Practical debugging commands** — responses include specific kubectl, cgroup, pprof, and perf commands you can actually run, not just generic advice.

## Weaknesses

- **Slow** — averaging 109s per question. The thinking mode generates 2-5x more tokens than visible output. Round 5 took nearly 3 minutes for a "Hard" question.
- **Verbose** — answers are thorough but sometimes pad with information that wasn't asked for. The FastAPI answer (R5) was 4,882 tokens for a question that could be answered in 500.
- **Missed the classic tokio antipattern** — Round 6 diagnosed "recursive task spawning" instead of the more likely "blocking the async runtime." The `no errors` clue strongly suggests blocking, which the model underweighted.
- **"Robust" solutions lack architectural variety** — R5's two solutions were both Redis-based, differing only in error handling rather than fundamental approach.

---

## Thinking Mode Analysis

The model spent significant tokens on internal reasoning before producing visible output:

| Round | Total Tokens | Approx Visible | Approx Thinking | Think Ratio |
|-------|-------------|----------------|-----------------|-------------|
| 1 | 1,149 | ~400 | ~750 | 65% |
| 2 | 1,839 | ~600 | ~1,200 | 65% |
| 3 | 3,155 | ~1,200 | ~1,950 | 62% |
| 4 | 2,856 | ~800 | ~2,050 | 72% |
| 5 | 4,882 | ~1,500 | ~3,380 | 69% |
| 6 | 5,792 | ~1,800 | ~3,990 | 69% |
| 7 | 1,708 | ~700 | ~1,000 | 59% |

Roughly **60-70% of all generated tokens are thinking tokens** that don't appear in the response. This is the main driver of latency.

With `max_tokens=512-1024`, the thinking consumed the entire budget and produced empty responses (5/7 rounds failed in our initial test). **`max_tokens >= 4096` is required** for reliable output, and `8192` is recommended.

---

## Hardware Notes

- Model VRAM: 12,348 MiB (GPU) + 521 MiB (CPU-mapped)
- All 65/65 layers offloaded to ROCm GPU
- ~2.5 GB VRAM headroom (vs ~0.3 GB for Mistral-Small-24B)
- No OOM events across all test runs
- Accessed via Traefik IngressRoute with bearer token auth — no port-forward timeouts
