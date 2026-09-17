# VLM-only `num_batch` 512 vs 2048 — 2026-09-17

IDEA-1105. Does `num_batch` change summarize (VLM) throughput at realistic OpenViking input sizes, with no FIM load? Ollama's default is 512 (`-b 512 -ub 512`); a 2026-09-16 sweep under FIM contention hinted at a ~15-20% prefill gain for 2048.

## Setup

- timmy, RX 9070 XT, Ollama 0.34.1 (Vulkan), `OLLAMA_NUM_PARALLEL=4`, KV q8_0.
- Temporary tags `deepseek-coder-v2:instruct-nb512` / `-nb2048` built from `deepseek-coder-v2:instruct` (`num_ctx 8192`), deleted afterwards; FIM re-pinned.
- Inputs: 9 real compendium pointer payloads (`compendium-sync.py` `payload_for()`), 3 per bucket, each rendered through OpenViking's own `semantic.document_summary` template in the running pod. Prompt tokens as reported by Ollama: **small 1,416** (pointer p50), **medium 3,198** (p99), **large 6,865** (the largest pointers). Inputs and responses are private vault content and are **not** committed here; `results.json` holds aggregates only.
- `/api/chat`, `num_predict 256`, temperature 0.2, salted prompts, 1 warm-up request per tag switch (not counted).
- Matrix: 3 buckets × 2 `num_batch` × concurrency 1/2/3. `num_batch` levels ran in ABBA order per bucket, 10 requests per pass, so every cell is 20 requests over two passes.
- Tool: `benchmarks/ollama/tools/vlm-batch-bench.py`.

## Results — abstracts per minute

| Bucket | Concurrency | nb512 | nb2048 | Δ |
| --- | --- | ---: | ---: | ---: |
| small (1.4k tok) | 1 | 29.2 | 31.1 | +6.5% |
| small | 2 | 34.3 | 36.2 | +5.7% |
| small | 3 | 39.9 | 44.4 | +11.5% |
| medium (3.2k tok) | 1 | 23.2 | 25.1 | +7.9% |
| medium | 2 | 26.3 | 27.8 | +5.8% |
| medium | 3 | 29.6 | 32.1 | +8.4% |
| large (6.9k tok) | 1 | 16.2 | 17.5 | +7.7% |
| large | 2 | 16.5 | 17.5 | +6.0% |
| large | 3 | 17.6 | 20.6 | +17.6% |

## Results — per request

| Bucket | Conc. | Prefill p50 nb512 → nb2048 | Decode p50 (tok/s) nb512 / nb2048 | Wall p50 nb512 → nb2048 | Wall p95 nb512 → nb2048 |
| --- | --- | --- | --- | --- | --- |
| small | 1 | 0.26 → 0.21 s | 156 / 163 | 2.00 → 1.92 s | 2.27 → 2.06 s |
| small | 3 | 0.39 → 0.39 s | 81 / 93 | 4.01 → 3.46 s | 5.58 → 5.13 s |
| medium | 1 | 0.63 → 0.47 s | 152 / 154 | 2.55 → 2.40 s | 2.80 → 2.45 s |
| medium | 3 | 0.80 → 0.65 s | 61 / 79 | 6.21 → 4.94 s | 8.20 → 6.15 s |
| large | 1 | 1.40 → 1.11 s | 135 / 136 | 3.74 → 3.50 s | 3.94 → 3.81 s |
| large | 3 | 1.67 → 1.37 s | 37 / 42 | 10.13 → 8.65 s | 13.02 → 11.26 s |

Concurrency gain (abstracts/min, concurrency 1 → 3): small +37% / +43%, medium +27% / +28%, large +8% / +18% (nb512 / nb2048).

## Quality and health

- 18/18 cells valid: 0 request errors, 0 empty outputs; median abstract length 136-179 words against the template's 60-180-word target.
- Runner VRAM: nb512 13.68 GB, nb2048 13.95 GB (**+252 MiB** compute buffer).
- `NRestarts=0`, no kernel OOM during the run; temporary tags deleted, `deepseek-coder-v2:fim` re-pinned at `num_ctx 8192`.

## Reading

- **`num_batch` 2048 is a small, consistent win**: +6-8% throughput at concurrency 1-2 in every bucket, +8-18% at concurrency 3. The gain comes from prefill (15-25% faster at medium/large) and, at concurrency 3, from less decode interference.
- **Against the IDEA-1105 decision rule** (bake 2048 only for ≥10% more abstracts/min at the p50 or p99 size, with no VRAM or quality cost): **not met.** Only small at concurrency 3 clears 10% (+11.5%); medium peaks at +8.4%; and 2048 costs +252 MiB of VRAM. Keep the 512 default unless the local VLM runs at concurrency 3.
- **Concurrency pays less as prompts grow**: 3 concurrent requests give +37-43% on small prompts but only +8-18% on the largest, where prefill dominates the shared GPU.
- Measured without FIM load. With FIM co-resident, larger batches also lengthen each non-preemptible prefill pass (see `fim-vlm-np4-ctx8k-20260916-1718`).
