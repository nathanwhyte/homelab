# FIM + VLM dual-use at 4 slots × 8192 — 2026-09-16

IDEA-1105, second run. Same probe and VLM workload as `fim-vlm-dual-use-20260916-1636` (5,229-token markdown document into a 256-token abstract over `/api/chat`, 8 FIM probes per condition), after moving timmy to `OLLAMA_NUM_PARALLEL=4` with both deepseek-coder-v2 tags at `num_ctx 8192` (`-c 32768 -np 4`, 4,590 MiB q8_0 KV, 28/28 layers on Vulkan0). Budget under test: 3 summarize requests + 1 FIM. Ollama 0.34.1, run from pop over LAN with `NUM_BATCH_VARIANTS="1024 2048" run-fim-vlm-dual-use.sh 8`.

## Results — FIM latency

| Tag / `num_batch` | Condition | FIM TTFT p50 | FIM TTFT p95 | FIM 64-tok p50 |
| --- | --- | --- | --- | --- |
| `fim` / 512 | A idle | 0.23 s | 0.23 s | 0.62 s |
| `instruct` / 512 | A idle | 0.23 s | 0.24 s | 0.62 s |
| `instruct` / 512 | B 1 decode loop | 0.24 s | 0.28 s | 0.76 s |
| `instruct` / 512 | F 1 VLM loop | 0.49 s | 1.46 s | 2.15 s |
| `instruct` / 512 | G 2 VLM loops | 0.28 s | 1.35 s | 3.70 s |
| `instruct` / 512 | H 3 VLM loops | 0.32 s | 1.54 s | 3.74 s |
| `instruct` / 1024 | A idle | 0.20 s | 0.21 s | 0.60 s |
| `instruct` / 1024 | F 1 VLM loop | 0.80 s | 1.46 s | 1.86 s |
| `instruct` / 1024 | H 3 VLM loops | 0.24 s | 1.18 s | 3.63 s |
| `instruct` / 2048 | A idle | 0.21 s | 0.21 s | 0.60 s |
| `instruct` / 2048 | F 1 VLM loop | 0.55 s | 1.39 s | 1.78 s |
| `instruct` / 2048 | H 3 VLM loops | 0.23 s | 1.27 s | 3.99 s |

Condition D (~9.8k-token prefill loop) did not run: its prompt exceeds the 8192-token slot, and Ollama rejected it with HTTP 400 in 36 ms rather than truncating it.

## Results — VLM throughput

| `num_batch` | Condition | Prefill p50 | Decode p50 per request | Wall per abstract p50 | Abstracts/s (all loops) |
| --- | --- | --- | --- | --- | --- |
| 512 | F 1 loop | 1.15 s | 114 tok/s | 2.5 s | ~0.40 |
| 512 | G 2 loops | 1.19-1.25 s | 41-42 tok/s | 4.6-4.7 s | ~0.43 |
| 512 | H 3 loops | 1.22-1.26 s | 23-29 tok/s | 6.3-7.0 s | ~0.46 |
| 1024 | F 1 loop | 1.03 s | 124 tok/s | 2.3 s | ~0.43 |
| 1024 | H 3 loops | 0.99-1.11 s | 26-28 tok/s | 5.9-6.2 s | ~0.49 |
| 2048 | F 1 loop | 0.98 s | 124 tok/s | 2.3 s | ~0.43 |
| 2048 | H 3 loops | 0.86-0.99 s | 29-38 tok/s | 5.5-5.7 s | ~0.54 |

## Quality

- FIM completion was byte-identical to the `fim` tag baseline in every condition and `num_batch` variant (temperature 0; the probe prefix is repetitive, so run `fim-smoke.py` before switching serving).
- Every sampled abstract was on-topic and on-format.
- `NRestarts=0` and no `oom-kill` across the run; the `fim` tag was re-pinned at 8192 afterwards.

## Reading

- **Slot queuing is fixed.** With 2 slots, 2 VLM loops pushed FIM TTFT p50 to 2.15 s (previous run). With 4 slots, FIM TTFT p50 stays at 0.23-0.32 s even with 3 VLM loops running, because a slot is always free.
- **Prefill contention is not.** FIM TTFT p95 is 1.2-1.5 s under any VLM load: a FIM request that lands during a ~1 s summarize prefill waits for it. That is still well outside the INFO-1090 inline budget (p95 ≤ 0.30 s).
- **Decode is shared.** Once running, FIM decodes alongside up to 3 abstracts, so a 64-token completion takes ~3.7 s under 3 loops versus 0.62 s idle.
- **More slots barely raise VLM throughput.** Three concurrent abstracts deliver ~0.46-0.54 abstracts/s against ~0.40-0.43 for one at a time (+15-25%). Batched MoE decode on this card splits the same compute rather than multiplying it.
- **`num_batch` 2048 is the best of the three**, by a small margin: prefill ~15-20% faster, the best VLM throughput, and FIM p95 slightly lower than 512. 1024 and 2048 are within noise of each other on FIM latency at 8 probes.
- **Verdict:** 4 × 8192 makes dual-use workable for background summarizing while FIM stays responsive at the median, but not while typing at full VLM concurrency. For a full OV resync, 3 concurrent VLM requests buy little throughput and cost FIM tail latency; 1-2 concurrent is nearly as fast.
