# FIM + VLM dual-use on one runner — 2026-09-16

IDEA-1105. Can one resident `deepseek-coder-v2:instruct` runner (`num_ctx 16384`, 2 slots, q8_0 KV) serve inline FIM and OpenViking-shaped summarize requests at the same time on timmy's RX 9070 XT? Two 16B-lite tags cannot co-reside on the 16 GB card, so one runner serving both roles is the only way to run them concurrently on the GPU.

Ollama 0.34.1, Vulkan, 8 FIM probes per condition, run from pop over LAN with `run-fim-vlm-dual-use.sh 8`. VLM load: system prompt plus a 5,229-token markdown document, 256-token abstract, `/api/chat`, looped.

## Results

| Leg | Condition | FIM TTFT p50 | FIM TTFT p95 | FIM 64-tok p50 | Background |
| --- | --- | --- | --- | --- | --- |
| `fim` tag | A idle | 0.24 s | 0.29 s | 0.66 s | none |
| `instruct` | A idle | 0.24 s | 0.24 s | 0.67 s | none |
| `instruct` | B 1 decode loop | 0.25 s | 0.27 s | 0.74 s | 137 tok/s decode |
| `instruct` | D 1 ~9.8k prefill loop | 0.37 s | 2.98 s | 2.08 s | 2.2 s prefill, 95 tok/s |
| `instruct` | F 1 VLM loop | 0.93 s | 1.69 s | 2.05 s | 1.1 s prefill, 123 tok/s, 2.6 s/abstract |
| `instruct` | G 2 VLM loops | 2.15 s | 2.44 s | 4.18 s | 52-68 tok/s, 4.7 s/abstract each |

## Quality

- FIM: the instruct tag's completion matched the `fim` tag's byte for byte in every condition (temperature 0). The probe prefix is repetitive Lua, so this shows identical behavior, not broad FIM quality. Run `fim-smoke.py` against the instruct tag before switching serving.
- VLM: every sampled abstract was accurate and on-format (3-4 sentences naming settings, the num_ctx decision and the open follow-up). The base `fim` tag could not do this (TASK-1216).

## Reading

- Idle and under plain decode, the instruct tag is indistinguishable from the `fim` tag for FIM, so the weights swap costs nothing.
- Summarize load is the problem. Every abstract starts with about 1.1 s of 5k-token prefill, and FIM requests queue behind it. With one VLM loop, FIM TTFT p50 is 0.93 s (about 4× idle). With two loops both slots are busy and FIM p50 is 2.15 s, which is unusable for inline autocomplete (the INFO-1090 budget is TTFT p95 ≤ 0.30 s).
- Dual-use is viable only if summarize traffic is gated: one VLM request at a time at most, ideally off editing hours or paused while FIM requests arrive. An unthrottled OV ingest burst would take autocomplete down.
- `fim` tag restored afterwards (`keep_alive -1`).
