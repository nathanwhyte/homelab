# Three-Model Comparison: Mistral-Small-24B vs Qwen3.5-27B vs Claude Haiku

**Date:** 2026-03-20
**Test:** 7 rounds of coding/debugging questions, easy through expert difficulty

---

## The Models

| | Mistral-Small-24B Q4_K_M | Qwen3.5-27B Q3_K_M | Claude Haiku 4.5 |
|---|---|---|---|
| **Parameters** | 24B | 27B | Unknown (cloud) |
| **Quantization** | 4-bit (Q4_K_M) | 3-bit (Q3_K_M) | Full precision |
| **VRAM** | 14.3 GB | 13.5 GB | N/A |
| **Hardware** | RX 9070 XT (ROCm) | RX 9070 XT (ROCm) | Anthropic cloud |
| **Thinking mode** | No | Yes (~65% hidden tokens) | No |
| **Cost** | Free (local) | Free (local) | Pay per token |

---

## Head-to-Head Results

| Round | Difficulty | Topic | Mistral | Qwen | Claude | Best Quality |
|-------|-----------|-------|---------|------|--------|-------------|
| 1 | Easy | Mutex vs semaphore | 7/10 (13s) | 8/10 (41s) | 8/10 (7s) | Qwen/Claude |
| 2 | Moderate | Go concurrency bugs | 7/10 (11s) | 9/10 (65s) | 9/10 (8s) | Qwen/Claude |
| 3 | Moderate | K8s OOMKilled debugging | 5/10 (21s) | 9/10 (112s) | 9/10 (15s) | Qwen/Claude |
| 4 | Hard | Bash find/xargs safety | 7/10 (14s) | 9/10 (102s) | 9/10 (9s) | Qwen/Claude |
| 5 | Hard | FastAPI multi-worker cache | 6/10 (20s) | 8/10 (175s) | 9/10 (14s) | Claude |
| 6 | Expert | Rust/tokio starvation | 5/10 (21s) | 7/10 (209s) | 9/10 (13s) | Claude |
| 7 | Expert | PostgreSQL streak query | 5/10 (22s) | 8/10 (61s) | 8/10 (38s) | Qwen/Claude |

### Averages

| Metric | Mistral-Small-24B | Qwen3.5-27B | Claude Haiku |
|--------|-------------------|-------------|--------------|
| **Quality** | 6.0/10 | 8.3/10 | 8.7/10 |
| **Avg time** | 17.4s | 109s | 14.9s |
| **Correct answers** | 7/7 | 6/7 | 7/7 |
| **Total time** | 122s | 765s | 104s |

---

## Quality by Difficulty Tier

| Difficulty | Mistral | Qwen | Claude |
|-----------|---------|------|--------|
| Easy (R1) | 7.0 | 8.0 | 8.0 |
| Moderate (R2-3) | 6.0 | 9.0 | 9.0 |
| Hard (R4-5) | 6.5 | 8.5 | 9.0 |
| Expert (R6-7) | 5.0 | 7.5 | 8.5 |

**Key insight:** Mistral degrades sharply on expert questions (5.0 avg). Qwen and Claude both maintain quality, though Qwen dips slightly. Claude is the most consistent across all difficulty levels.

---

## Strengths and Weaknesses Summary

### Mistral-Small-24B Q4_K_M
**Best at:** Fast responses, simple/moderate questions, code generation boilerplate
| Strength | Weakness |
|----------|----------|
| Fast (17s avg) | Shallow on expert topics (5.0 avg) |
| Reliable — always produces output | Generic debugging checklists instead of root cause |
| Good code formatting | Incorrect SQL algorithm (R7) |
| No special config needed | Misses subtle issues other models catch |

### Qwen3.5-27B Q3_K_M
**Best at:** Deep analysis, thorough debugging walkthroughs, catching edge cases
| Strength | Weakness |
|----------|----------|
| Near-Claude quality (8.3 vs 8.7) | Slow (109s avg, 6x slower than others) |
| Catches subtle issues (flag injection R4) | Requires max_tokens >= 4096 |
| Correct algorithms (gap-and-island SQL) | 65% of tokens are hidden thinking |
| Thorough debugging with real commands | Verbose — pads with unrequested info |
| Free, runs locally | Missed classic tokio antipattern (R6) |

### Claude Haiku 4.5
**Best at:** Precise diagnosis, consistent quality, speed
| Strength | Weakness |
|----------|----------|
| Highest quality (8.7 avg) | Cloud dependency and cost |
| Fastest (15s avg) | Less complete code examples |
| Precise root-cause identification | Occasionally misses edge cases |
| Consistent across all difficulty levels | Can't run offline |
| Multiple genuinely different solutions | |

---

## Speed vs Quality Tradeoff

```
Quality
  9 |         C C C C     C = Claude Haiku
    |     Q Q       Q Q   Q = Qwen3.5-27B
  8 |   Q
    | M                   M = Mistral-Small-24B
  7 | M M M
    |
  6 |     M   M
    |             M M
  5 |
    +--+--+--+--+--+--+--> Time (s)
    0  20  40  60 100 150 200
```

- **Claude** clusters in the top-left (fast + high quality)
- **Qwen** clusters in the top-right (high quality + slow)
- **Mistral** clusters in the left-middle (fast + moderate quality)

---

## Recommendations

### For interactive use (chat, quick questions, code assist)
**Use Mistral-Small-24B.** 17s responses are acceptable for interactive work. Quality is good enough for moderate tasks and you can eyeball the output.

### For batch processing (summarization, analysis, complex reasoning)
**Use Qwen3.5-27B with thinking enabled.** The 60-200s latency doesn't matter for async workloads, and the quality approaches Claude. Set `max_tokens >= 8192`.

### For critical tasks (production debugging, architectural decisions)
**Use Claude Haiku.** It's the most reliable across all difficulty levels and the fastest. Worth the cloud cost for tasks where being wrong is expensive.

### Model switching strategy
Both local models are cached on the same PVC. Swap with:
```bash
# Edit primary model in rocm-llamacpp-deployment.yaml, then:
bash llama/deploy-llamacpp-rocm.sh
```

Or consider running Mistral as the default and switching to Qwen for specific complex tasks.

---

## Infrastructure Notes

- Both models run on the same RX 9070 XT (16GB VRAM) — only one at a time
- Exposed via Traefik IngressRoute at `http://llama.local/v1`
- Bearer token auth via llama-server's `--api-key` flag
- Models cached on `llama-rocm-model-cache` PVC — fast swaps (no re-download)
- Claude accessed via `claude --print --model haiku` CLI
