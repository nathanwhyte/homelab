# Model Showdown Report: Mistral-Small-24B vs Claude Haiku

**Date:** 2026-03-20
**Infrastructure:** RX 9070 XT (16GB VRAM) on timmy, llama.cpp server-rocm
**Models:** Mistral-Small-24B-Instruct Q4_K_M (local) vs Claude Haiku 4.5 (via CLI)

---

## Results At a Glance

| Round | Difficulty | Topic | Mistral | Claude | Quality Winner |
|-------|-----------|-------|---------|--------|----------------|
| 1 | Easy | Mutex vs Semaphore | 13s (7/10) | 7s (8/10) | Claude |
| 2 | Moderate | Go concurrency bugs | 11s (7/10) | 8s (9/10) | Claude |
| 3 | Moderate | K8s OOMKilled debugging | 21s (5/10) | 15s (9/10) | Claude |
| 4 | Hard | Bash find/xargs safety | 14s (7/10) | 9s (9/10) | Claude |
| 5 | Hard | FastAPI multi-worker cache | 20s (6/10) | 14s (9/10) | Claude |
| 6 | Expert | Rust/tokio CPU starvation | 21s (5/10) | 13s (9/10) | Claude |
| 7 | Expert | PostgreSQL streak query | 22s (5/10) | 38s (8/10) | Claude |

**Average quality:** Mistral 6.0 / Claude 8.7
**Total time:** Mistral 122s / Claude 104s

---

## Mistral-Small-24B Strengths

- **Full working code examples** — consistently provides runnable code with imports, main functions, and complete context. Good for copy-paste usage.
- **Well-structured responses** — clean headers, numbered lists, step-by-step breakdowns. Easy to scan.
- **Solid on fundamentals** — rounds 1, 2, and 4 scored 7/10, demonstrating reliable baseline knowledge of concurrency, Go, and shell scripting.
- **Speed on complex generation** — beat Claude on the hardest round (22s vs 38s for the PostgreSQL query), likely because local inference doesn't have network overhead for long outputs.
- **Breadth of coverage** — tends to list multiple possible causes and approaches, useful when you're not sure what you're looking for.

## Mistral-Small-24B Weaknesses

- **Misses the "why" on harder questions** — on rounds 3 (K8s OOMKilled) and 6 (tokio starvation), Mistral gave generic debugging checklists instead of identifying the specific root cause. This is the biggest gap.
- **Incorrect algorithms on expert tasks** — the PostgreSQL streak calculation (round 7) used a running SUM instead of the gaps-and-islands pattern, producing wrong results for the core requirement.
- **Verbose without depth** — responses are long but sometimes pad with generic advice rather than pinpointing the issue. More words, less signal.
- **Weak async/framework knowledge** — round 5's "simple" and "robust" solutions were both Redis-based (not genuinely different), and used synchronous Redis in async handlers.
- **Truncation on longer answers** — rounds 3 and 6 were cut off due to token limits, losing potentially important content.

## Claude Haiku Strengths

- **Precise diagnosis** — consistently identifies the exact root cause first, then expands. On round 3, immediately names RSS-vs-cgroup accounting; on round 6, immediately says "unbounded task spawning."
- **Multiple genuinely different solutions** — when asked for two approaches, provides two architecturally distinct options (e.g., in-process startup cache vs Redis with decorator pattern).
- **Deep ecosystem knowledge** — knows tokio-console, cgroup memory.stat, FastAPI lifespan context managers, find -exec vs xargs trade-offs. Uses idiomatic patterns for each ecosystem.
- **Concise and scannable** — shorter responses that pack more useful information per line.
- **Correct algorithms** — the PostgreSQL gaps-and-islands pattern was correct where Mistral's was not.

## Claude Haiku Weaknesses

- **Less complete code examples** — tends toward pseudocode or partial snippets rather than fully runnable programs.
- **Slower on long-output tasks** — the PostgreSQL round took 38s (vs Mistral's 22s), likely due to API overhead on longer generations.
- **Occasionally misses edge cases** — round 6 could have mentioned `spawn_blocking` for CPU-bound work; round 1 could have covered binary vs counting semaphores.

---

## Verdict

**For quick knowledge lookups and boilerplate code:** Mistral-Small-24B is perfectly capable and runs for free on your own hardware. Rounds 1, 2, and 4 show it handles standard programming questions well.

**For debugging production issues or complex algorithmic tasks:** Claude Haiku has a significant edge. The gap widens as difficulty increases — Mistral averaged 7/10 on easy/moderate rounds but dropped to 5/10 on expert rounds, while Claude stayed at 8-9/10 throughout.

**The sweet spot for the local model:** Use Mistral-Small-24B for tasks where you can verify the output — code generation, explaining concepts, formatting/refactoring. Escalate to Claude when you need precise diagnosis of subtle bugs or correct-first-time algorithmic solutions.

---

## Hardware Notes

- Mistral-Small-24B Q4_K_M uses ~15.7 GB of the 9070 XT's 16 GB VRAM
- Running with `--parallel 1`, `--ctx-size 8192`, q8_0 KV cache
- Average response time: 17.4s per question (acceptable for interactive use)
- Qwen3.5-27B Q3_K_M is available as a fallback on the same PVC
