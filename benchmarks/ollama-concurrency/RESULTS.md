# Ollama concurrency benchmark — pop (M5 Max, 64 GB), 2026-07-16

Server: manual `ollama serve` 0.32.1 with `OLLAMA_NUM_PARALLEL=2`,
`OLLAMA_MAX_LOADED_MODELS=2`, `OLLAMA_FLASH_ATTENTION=1`, `OLLAMA_KEEP_ALIVE=10m`
(the desktop app hardcodes `NUM_PARALLEL=4` / `MAX_LOADED_MODELS=1` in the env it
gives its serve child and ignores `launchctl setenv` — see note below).
Requests: `/api/generate` streamed, `num_ctx=8192`, `num_predict=256`, temp 0.2,
unique prompt preambles to defeat prefix caching. TTFT measured client-side to
first streamed chunk (includes queue wait). 3 repeats per level.
MLX FIM services were stopped beforehand for isolation.

## Leg A — same model, qwen3.6:35b-mlx (MLX engine, MoE 35B-A3B nvfp4)

| clients     | TTFT med / max (s) | gen/req med (tok/s) | aggregate med (tok/s) | wall med / max (s) |
| ----------- | ------------------ | ------------------- | --------------------- | ------------------ |
| 1           | 0.37 / 0.40        | 113                 | 97                    | 2.64 / 2.67        |
| 2           | 1.41 / 2.73        | 116                 | 110                   | 3.62 / 4.99        |
| 4 (2 slots) | 3.61 / 6.93        | 122                 | 115                   | 5.75 / 9.00        |

**The MLX runner serializes concurrent requests.** Per-request decode speed is
untouched (~113–122 tok/s at every level) while TTFT and wall stack linearly
with queue depth; aggregate throughput barely moves (97 → 115 tok/s). There is
no continuous-batching win on the MLX path — `NUM_PARALLEL` slots only bound
admission, they don't batch decode. Resident size grew 23.4 → 25.6 GB across
levels: MLX KV appears allocated per-request/lazily, not eagerly at
`num_ctx × NUM_PARALLEL` (that eager math is a llama.cpp-engine property).

## Leg B — same model, qwen2.5-coder:fim-1.5b (GGUF, llama.cpp engine)

| clients     | TTFT med / max (s) | gen/req med (tok/s)      | aggregate med (tok/s) |
| ----------- | ------------------ | ------------------------ | --------------------- |
| 1           | ~0.1               | ~275 (short gens, noisy) | —                     |
| 2           | 0.14 / 0.19        | 248                      | 322                   |
| 4 (2 slots) | 0.18 / 1.29        | 237                      | 388                   |

**llama.cpp does batch.** Aggregate scales with concurrency (322 → 388 tok/s)
while per-request decode drops only ~5%. Caveat: base model EOS'd early on some
chat-style prompts (tokens/req 86–211), so walls aren't comparable across
levels; TTFT and tok/s rates are valid.

## Leg C — multi-model cohabitation (35b-mlx + fim-1.5b, both resident)

| level                | TTFT med / max (s) | gen/req min–max (tok/s) | aggregate med (tok/s) |
| -------------------- | ------------------ | ----------------------- | --------------------- |
| solo 35b             | 0.32 / 0.42        | ~120                    | 105                   |
| solo fim-1.5b        | 0.12 / 0.13        | ~283                    | 247                   |
| 1+1 concurrent       | 0.29 / 0.49        | 105 (35b) – 210 (fim)   | 161                   |
| burst: 1×35b + 3×fim | 0.21 / 1.85        | 88 (35b) – 186 (fim)    | 297                   |

**Cohabitation is cheap.** Each model runs in its own runner process, so there
is no cross-model queueing — concurrent TTFT is identical to solo. Bandwidth
sharing costs the 35b ~12% decode (120 → 105) with one small-model request and
~26% (→ 88) under a 3-wide small-model burst; the small model loses ~26–34%.
Memory: 25.6 + 1.5 GB resident, no pressure on 64 GB with 8k contexts.

## Operational takeaways

1. Chat model + autocomplete model side-by-side is viable on pop: the
   autocomplete model's TTFT stays ~0.2 s even while the 35b decodes.
2. Same-model concurrency on MLX models buys almost nothing — a second Claude
   session against qwen3.6:35b-mlx just waits (TTFT ≈ the first session's
   remaining decode time). Queueing is the cost, not slow decode.
3. `OLLAMA_MAX_LOADED_MODELS=1` (the IMPR-1057 OOM guard) is what forbids
   cohabitation today — and the guard is doubly moot because the desktop app
   ignores launchd env entirely and pins `MAX_LOADED_MODELS=1`,
   `NUM_PARALLEL=4`, `CONTEXT_LENGTH=131072` itself (verified via `ps eww`).
   Raising it requires either running serve manually or an app-level change.
4. The 131072 app context default × lazy MLX KV means memory grows with actual
   use, not at load — the eager-KV OOM fear applies to GGUF models only.

Raw data: `results-legA-qwen35b-np2.json`, `results-legB-fim15b-np2.json`,
`results-legC-multi-np2.json`. Harness: `concurrency_bench.py`.
