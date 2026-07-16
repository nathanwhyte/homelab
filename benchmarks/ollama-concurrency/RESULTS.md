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

## Legs D + E — cohabitation partner sweep: MoE vs dense (2026-07-16, later same day)

Same multi-model protocol as Leg C with different big-model partners for
`qwen2.5-coder:fim-1.5b`:

| pairing                                | big solo → cohab (tok/s) | FIM solo → cohab 1+1 (tok/s) | FIM TTFT max, burst (s) |
| -------------------------------------- | ------------------------ | ---------------------------- | ----------------------- |
| Leg C: qwen3.6:35b-mlx (MoE 35B-A3B)   | 120 → 105 (−12%)         | 283 → ~210 (−26%)            | 1.85                    |
| Leg D: gemma4:26b-mlx                  | 119 → 105 (−12%)         | ~281 → ~197 (−30%)           | 1.82                    |
| Leg E: qwen3.5:9b-mlx (**dense** 9.4B) | 75.6 → ~67 (−12%)        | 281 → ~90–103 (**−65%**)     | 3.47                    |

Two findings:

1. **gemma4:26b-mlx behaves like an MoE.** It decodes at 119 tok/s solo —
   identical to the 35B-A3B MoE — while the unambiguously dense 9.4B nvfp4
   model manages only 75.6 tok/s. A dense 26B would be bandwidth-capped around
   ~35–45 tok/s on this machine.
2. **A dense partner starves the small model; MoE partners don't.** Dense
   decode reads the full weight set every step and saturates the memory
   controller continuously, so the FIM model loses ~65% of its decode rate and
   its burst TTFT tail doubles. MoE decode leaves bandwidth gaps the small
   model can slip into (−26–30%). The big model loses the same ~12% in every
   pairing. For the autocomplete-cohabitation use case, prefer an MoE chat
   partner.

## Leg F — two mid-size MLX models: gemma4:12b + qwen3.5:9b (both dense)

| level          | TTFT med/max (s) | gen/req (tok/s)        | aggregate (tok/s) |
| -------------- | ---------------- | ---------------------- | ----------------- |
| solo gemma 12b | 0.16 / 0.18      | 57.7                   | 56                |
| solo qwen 9b   | 0.15 / 0.16      | 74.1                   | 71                |
| 1+1 concurrent | 0.23 / 0.24      | gemma ~33–42, qwen ~51 | 68.7              |

**gemma4:12b-mlx is dense** (57.7 tok/s solo, slower than the smaller 9.4B
qwen) — so the gemma4 family splits: 26b decodes MoE-like at 119 tok/s, 12b is
a conventional dense model. **Dense+dense cohabitation splits bandwidth
almost evenly**: each model keeps ~57–69% of its solo rate and the aggregate
(68.7 tok/s) is barely more than one model alone (sum of solos: 129). TTFT
stays flat — the cost is pure bandwidth sharing, never queueing.

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
`results-legC-multi-np2.json`, `results-legD-gemma26b-dense-multi-np2.json`
(filename says dense; the data proved it MoE-like),
`results-legE-qwen9b-dense-multi-np2.json`. Harness: `concurrency_bench.py`.
