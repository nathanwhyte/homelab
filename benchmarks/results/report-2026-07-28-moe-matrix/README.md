# Pop small-MoE benchmark matrix (2026-07-28) — IN PROGRESS

Re-run of the PROJ-1003 pop matrix on Ollama 0.32.5, with `laguna-xs-2.1`
restored (its 2026-07-13 macOS/Metal empty-output blocker was fixed upstream by
`#17291` / `#17237`) and a gemma4:12b MLX quant sweep added.

**This report is a stub — the Results section is unwritten.** The Open
Questions below are resolved (OQ-1), narrowed pending one test (OQ-2), or
standing scope boundaries to restate in the conclusions (OQ-3).

- **Harness**: `benchmarks/ollama/tools/concurrency-bench.py` (agentic workload,
  `num_ctx=32768`, `num_predict=16384`, `C=1..3`, 3 requests/level, 3 repeats)
- **Runner**: `benchmarks/ollama/tools/run-pop-moe-matrix.sh` — `OLLAMA_NUM_PARALLEL=3`,
  one `ollama stop` + 30s cooldown between models
- **Env**: ollama 0.32.5 (brew), `OLLAMA_FLASH_ATTENTION=1`,
  `OLLAMA_KV_CACHE_TYPE=q8_0`, `OLLAMA_MAX_LOADED_MODELS=1`,
  `OLLAMA_CONTEXT_LENGTH=131072`
- **Sampling**: per-model recommended settings (model card first, shipped
  Modelfile second, harness default last)

## Differences from the 2026-07-13 run

Both are deliberate and both break direct comparability with that table:

1. **Runtime**: 0.31.1 then, 0.32.5 now.
2. **Output budget**: `num_predict` 2048 → 16384. A first 0.32.5 pass at 2048
   showed the cap, not the model, was setting the numbers —
   `qwen3.6:35b-mlx` truncated 7-8 of every 9 requests and its
   time-to-first-answer (36.9s) matched `2048 / 55.3 tok/s` (37.0s) almost
   exactly. See the `73ff5d9` commit message.

## Results

<!-- TODO: fill from the 14 result dirs once the matrix completes. -->

## Open Questions

### OQ-1 — Which models actually benefit from concurrency, and why? (RESOLVED)

**Status: resolved 2026-07-28 from Ollama v0.32.5 source plus prior local
measurement. Two distinct mechanisms, not one.**

**1. MLX rows serialize by implementation.** Ollama's MLX runner has a single
request-consumer loop that calls `runRequest()` synchronously before accepting
the next request ([v0.32.5 `x/mlxrunner/runner.go` L207-243](https://github.com/ollama/ollama/blob/v0.32.5/x/mlxrunner/runner.go#L207-L243)).
`OLLAMA_NUM_PARALLEL` governs scheduler admission only; it does not create
continuous batching on the MLX path. This was already measured locally on
2026-07-16 and written up in
[[INFO-1105]](~/code/compendium/info/ollama-serving/INFO-1105-homelab-ollama-concurrency-on-pop-mlx-engine-serializes-llama-cpp-ba.md):
`qwen3.6:35b-mlx` TTFT stacked 0.37 → 1.41 → 3.61s at 1/2/4 clients with
aggregate flat, while GGUF `qwen2.5-coder:fim-1.5b` scaled 322 → 388 tok/s.

**2. `nemotron3:33b` is forced to one slot by policy, not architecture.** The
scheduler overrides `numParallel` to `1` for `nemotron_h`, `nemotron_h_moe` and
`nemotron_h_omni` — alongside `mllama`, `qwen3vl`, `qwen35`, `qwen3next`,
`lfm2` and others — logging "model architecture does not currently support
parallel requests" ([v0.32.5 `server/sched.go` L497-519](https://github.com/ollama/ollama/blob/v0.32.5/server/sched.go#L497-L519),
verified by direct fetch). Its flat GGUF result is therefore **not** evidence
that llama.cpp fails to batch hybrid Mamba models. Ordinary GGUF transformers
remain eligible for batching.

So the 07-13 summary ("MLX lacks batching") was right about MLX and wrong to
imply a single cause: nemotron3 was flat for an unrelated, explicit reason.

`laguna-xs-2.1:latest` (GGUF) vs its two MLX tags now **quantifies** how much
llama.cpp batching is worth on this workload, rather than being needed to
identify the mechanism — so the coverage caveat below is no longer blocking.

**Two claims in the original draft of this section were wrong** and are
corrected here:

- KV is *not* eagerly pre-allocated for three MLX slots. MLX cache growth is
  lazy; the eager-preallocation finding in IDEA-1063 concerns the llama.cpp
  path and should not have been asserted for MLX. The conclusion is unaffected
  — `server_prefill_s` being flat rules out allocation cost regardless of
  strategy.
- Seeing `OLLAMA_NUM_PARALLEL=3` in `ps eww` does **not** prove three effective
  slots. MLX serializes regardless, and nemotron3 is internally overridden to
  one.

**Original evidence, retained** — it is what the source explanation has to
account for, and it does:

The 2026-07-13 report attributed flat concurrency scaling to MLX
("MLX continuous-batching limitation; GGUF batching advantage"). The 07-13
numbers themselves are more nuanced than that summary:

| Model (07-13) | Backend | Decode C=1 | Agg C=3 | Scaling |
|---|---|---|---|---|
| `hermes3:8b` | GGUF Q4_0, dense | 77.6 | 99.6 | **1.28×** |
| `qwen3-coder:30b` | GGUF Q4_K_M, MoE | 67.3 | 84.8 | **1.26×** |
| `north-mini-code-1.0` | MLX nvfp4, MoE | 73.2 | 77.2 | 1.05× |
| `nemotron3:33b` | **GGUF** Q4_K_M, hybrid Mamba MoE | 79.1 | 78.9 | **1.00×** |
| `qwen3.6:35b-mlx` | MLX nvfp4, MoE | 41.4 | 41.5 | 1.00× |

`nemotron3:33b` is GGUF and did **not** scale, so "MLX vs GGUF" does not
explain the 07-13 data on its own. The 2026-07-28 run reproduces this: at
`num_predict=16384`, nemotron3 scales 1.02× and north-mini 1.01×.

Measurements on 2026-07-28 that constrain the answer — the mechanism is
**request serialization**, not KV allocation:

- `server_prefill_s` (server-reported `load_duration + prompt_eval_duration`,
  which is where KV allocation cost would appear) stays flat across
  concurrency: north-mini 0.059 → 0.092 → 0.119s, nemotron3 0.414 → 0.466 →
  0.458s — while **client-observed TTFT jumps to ~21s**.
- TTFT at C>1 lands on one request's solo decode time (north-mini
  1512 tok / 87.1 tps = 17.4s vs ttft 21.1s), i.e. a request's first token
  arrives as the previous one finishes — the signature of serialization.

**Coverage note (no longer blocking).** `hermes3:8b`, `qwen3-coder:30b` and the
plain-GGUF `gemma4:12b` rows were dropped from this matrix, leaving
`laguna-xs-2.1:latest` as the only plain-GGUF transformer row. That mattered
while the mechanism was unknown; now it only limits how much batching headroom
this table can quantify.

### OQ-2 — Is per-request `num_ctx` honored on 0.32.5? (PARTIALLY RESOLVED)

**Do not remove the `qwen36-claude.Modelfile` workaround on the strength of
this run.** An earlier draft of this section proposed exactly that; it was
wrong.

`dotfiles/ollama/qwen36-claude.Modelfile` records that per-request
`options.num_ctx` was **ignored** on 0.31.1 (verified 2026-07-08), which is why
that tag bakes `num_ctx` in. This run shows models resident at **32768** while
the server default is `OLLAMA_CONTEXT_LENGTH=131072`, which looked like
evidence of a fix. It is not sufficient:

- **There is no relevant change between the releases.** Ollama's option-merging
  tests are byte-identical in 0.31.1 and 0.32.5 — verified by fetching both
  copies of `server/routes_options_test.go` and comparing SHA-256
  (`bc0ce7b28fceecb1`, 6998 bytes, both). They already assert that an explicit
  request `num_ctx` overrides the Modelfile and the server default.
- **What this run actually demonstrates is cold-load behaviour.** The requested
  context becomes a soft context limit when the MLX runner is *first loaded*,
  and an already-loaded MLX runner deliberately ignores runner-option
  differences when deciding whether to reload
  ([v0.32.5 `server/sched.go` L1388-1444](https://github.com/ollama/ollama/blob/v0.32.5/server/sched.go#L1388-L1444)).
  The matrix runs `ollama stop` before every row, so every measurement here is
  a cold load. It proves the cold-load path selects 32K; it says nothing about
  changing context on a resident runner.

**Conclusion.** Direct `options.num_ctx` is honored when selecting the MLX
runner's cold-load soft context. 0.32.5 does **not** establish reliable dynamic
resizing of an already-resident MLX runner. Keep the dedicated Modelfile tag
until a warm-run test says otherwise.

**Decisive test** (post-matrix — running it now would perturb the benchmark):
32K cold load → request 64K *without* `ollama stop` → `ollama stop` and request
64K again, checking `/api/ps` after each step.

### OQ-3 — This matrix measures decode, not prefill (SCOPE BOUNDARY)

**Status: not a question needing new measurement — a limit on what these
results may be used to claim. Must be stated in the conclusions.**

The agentic workload uses ~180-204 token prompts, so essentially all of each
request is decode:

| Row | prompt tok | output tok | prefill share of wall | decode share |
|---|---|---|---|---|
| `north-mini-code-1.0:mlx-nvfp4` | 180 | 1774 | 0.3% | **99.7%** |
| `nemotron3:33b` | 204 | 1676 | 2.1% | **97.9%** |

Prefill and decode load the GPU in opposite ways. Prefill consumes the whole
prompt in one compute-bound matrix-**matrix** pass with high arithmetic
intensity, and saturates the device. Decode emits one token at a time as a
memory-bound matrix-**vector** pass with a serial dependency chain, and at
batch size 1 cannot fill the ALUs. Observed 2026-07-28: the GPU sits ~55%
under this benchmark, against 90%+ when the same model serves Claude Code.

The daily-driver workload is the opposite shape, and this is **measured, not
estimated** — from `claude-session-pop-qwen36-35b-20260707-184943.json` in this
same results directory (`qwen3.6:35b-mlx`, `full-warm`, n=3):

| Quantity | Value |
|---|---|
| Input tokens per turn | **77,466** (p50) |
| Time to first assistant token | 30.5s min / **36.1s p50** / 98.7s max |
| Implied prompt-processing rate | 2,544 / **2,146** / 785 tok/s |

Against ~1500 output tokens at the ~55 tok/s decode measured here, a full turn
is roughly **53-79% prefill** (p50 ≈ 57%) — dominated by the phase this matrix
barely exercises.

**Consequence.** These results rank models on decode throughput during
agentic generation, which is the right metric for "how fast do tokens come
out". They do **not** rank models for Claude-Code-shaped work, where prefill
dominates. Two rows could tie here and differ materially in daily use. Any
conclusion of the form "model X is the best daily driver on pop" is
unsupported by this data alone, and needs the prefill/TTFT work still active
in PROJ-1003: **TASK-1015** and **TASK-1111**. (TASK-1107 is cancelled and is
not follow-up work.)

The 2026-07-13 report noted the short prompts as a footnote explaining GPU
utilization. Stating it as a scope boundary on the conclusions is the sharper
form, and the one that stops the table being over-read.

### OQ-4 — Does MLX serialization make MLX *preferable* single-stream? (OPEN)

**Status: open. The laguna rows answer it. Recorded because OQ-1's result
invites a conclusion it does not support.**

OQ-1 establishes that Ollama's MLX runner serializes. It is tempting to read
that as "MLX is optimized for single-stream work". It is not. The two readings
must be kept apart:

- **Established — MLX is only *suitable* for single-stream work.** The absence
  of continuous batching is an implementation limit, not a design trade. A
  second concurrent request waits out the first. MLX is therefore acceptable
  where concurrency is never needed and strictly worse where it is.
- **Not established — that MLX is *faster per stream* than llama.cpp.** That is
  the claim "better suited" usually implies, and nothing measured so far tests
  it. The fastest MLX row here (`north-mini` 86.6 tok/s C=1) and fastest GGUF
  row (`nemotron3` 83.7 tok/s) are different models, so the comparison is void.
  INFO-1105's 113-122 tok/s for `qwen3.6:35b-mlx` was taken at
  `num_ctx=8192 / num_predict=256`; the same model runs ~55 tok/s here at 32K,
  so context length dominates that gap.

**What answers it.** `laguna-xs-2.1` as `:latest` (GGUF Q4_K_M) vs `:nvfp4` and
`:mxfp8` (MLX) — same weights, same prompts, same budget. Compare p50 `gen_tps`
at C=1. This is the only clean backend comparison in the matrix.

**Two things that temper the stakes either way:**

1. Batching's payoff on this hardware is modest. INFO-1105 measured GGUF
   scaling 322 → 388 tok/s (~20%), not the multiples a datacenter GPU shows,
   because decode here is memory-bandwidth-bound. [[IDEA-1062]] (dropped)
   reached the same conclusion from the other side: removing queue-wait
   entirely produced no perceptible speedup, so the simpler single-threaded
   server was kept.
2. Single interactive use is unaffected. One Claude Code session issues one
   request at a time and loses nothing to serialization. It bites only when
   fanning out parallel agents at a local model — already the operational rule
   in [[IMPR-1066]] (shipped), which INFO-1105 cross-references.

**Expect the quant to matter more than the backend.** A 2026-07-28 pre-flight
probe put `laguna:mxfp8` at ~2 tok/s against `:nvfp4` at ~75 on the same model.
If that survives proper measurement, "MLX" is not one thing and any
backend-level conclusion needs stating per quantization.
