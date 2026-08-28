# FIM contention probe — timmy RX 9070 XT, Ollama 0.33.1 (2026-08-28)

Re-baseline of the IDEA-1090 contention probe after upgrading timmy from 0.32.13 → 0.33.1 the same day (image digest `075246f7…` = `latest` = `0.33.1`; Vulkan confirmed from the pod log: `library=Vulkan … AMD Radeon RX 9070 XT (RADV GFX1201)`; `deepseek-coder-v2:fim` resident 13.7 GB, `n_seq_max = 2`, `n_ctx_seq = 16384`).

Tool: `benchmarks/ollama/tools/fim-contention-probe.py` (8 reps per condition, salted 2 KiB Lua prefix, `max_tokens 64`, `temperature 0`; background load loops `/api/generate` on the same tag). Server env unchanged from production: `NUM_PARALLEL=2`, `MAX_LOADED_MODELS=1`, `KEEP_ALIVE=-1`, `CONTEXT_LENGTH=131072`, `KV_CACHE_TYPE=q8_0`, `FLASH_ATTENTION=1`.

## Files

| File | What |
| --- | --- |
| `probe.log`, `summary.json` | **The baseline.** Clean run, GPU idle apart from the pinned FIM runner, both slots free. |
| `probe-run1-CONTAMINATED-warmhook-slot1.log`, `summary-run1-CONTAMINATED.json` | First attempt, started 32 s after the pod restart. Invalid — see below. Kept as the evidence for the warm-hook defect. |

## Results (clean run) vs 0.32.13

0.32.13 numbers are from the 2026-08-28 morning run archived in the compendium (`_sources/2026-08/2026-08-28_fim-chat-contention-probe-timmy.md`, IDEA-1090).

| Condition | FIM TTFT p50 0.32.13 → **0.33.1** | FIM TTFT p95 0.32.13 → **0.33.1** | Background stream (0.33.1) |
| --- | --- | --- | --- |
| A idle | 0.35 s → **0.25 s** | 0.36 s → 0.34 s | — |
| B 1 bg decode (42-tok prompt, 400 out) | 0.36 s → **0.26 s** | 0.37 s → 0.26 s | 153 tok/s, unaffected |
| C 2 bg decode | 2.39 s → **2.31 s** | 3.19 s → 2.96 s | 140–143 tok/s each |
| D 1 bg ~9.8k-token prefill, 200 out | 0.38 s → **0.27 s** | 3.30 s → 3.26 s | prefill 2.66 s, decode 103 tok/s |
| E 2 bg ~9.8k-token prefill | 5.60 s → **4.91 s** | 8.70 s → 6.53 s | decode 41–64 tok/s |

Reading:

- The mechanics are unchanged by the upgrade: one background stream leaves FIM untouched (free second slot), any FIM request that lands inside a ~2.7 s prefill waits for it (D p95), and two background streams saturate both slots so FIM queues (C, E).
- Idle / uncontended TTFT dropped ~0.10 s (0.35 → 0.25 s p50, −29 %). That is the 0.32.15 model-metadata cache; the same order as pop's measured −30 % (INFO-1148) rather than upstream's headline −47 %.
- Full-64-token completion p50 idle: 0.68 s → 0.57 s.

## The contaminated first run (warm-hook defect)

The first 0.33.1 run reported B = 3.19 s p50 (vs 0.26 s clean) and C = 7.86 s — every request during B launched on runner **slot 0** while slot 1 never appeared until three requests were in flight. Cause, from the GIN log: the deployment's post-restart warm-up, `timeout 180s ollama run deepseek-coder-v2:fim "// warmup"`, ran as a real generation on the **base** FIM tag (no chat template, no natural stop) and held slot 1 for **2m39s** (`127.0.0.1 POST /api/generate … 2m39s`, 16:55:18 → 16:57:57). The probe started at 16:55:49.

Consequences: every post-restart measurement in the first ~3 minutes runs with one slot; and every pod restart burns ~2.5 min of GPU generating garbage. The fix (drafted in `llama/ollama-configmap.yaml`, not yet applied) replaces the `ollama run` with a load-only `/api/generate` POST with an empty prompt over bash `/dev/tcp` (the image has no curl/wget/python) — verified in the live pod to return `done_reason: "load"` immediately. Until it is applied: **do not benchmark within 3 minutes of an ollama pod restart on timmy**, and confirm with `kubectl logs … | grep '127.0.0.1 | POST "/api/generate"'` that the warm request has completed.

Three targeted repeats between the two full runs (one 400-token background generation + three FIM probes each) gave FIM TTFT 0.17–0.23 s with slots alternating 1/0 — confirming the serialization was the warm-up, not 0.33.1.
