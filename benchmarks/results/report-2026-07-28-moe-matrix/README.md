# Pop small-MoE benchmark matrix (2026-07-28)

Re-run of the PROJ-1003 pop matrix on Ollama 0.32.5, with `laguna-xs-2.1`
restored (its 2026-07-13 macOS/Metal empty-output blocker was fixed upstream by
`#17291` / `#17237`) and a gemma4:12b MLX quant sweep added.

**Results are complete (14 rows, 378/378 usable).** Of the open questions
below, **OQ-1, OQ-4 and OQ-5 are resolved**; OQ-2 is partially resolved (warm
resize answered, effective context limit still unknown); OQ-3 and OQ-6 are
standing scope boundaries that must be restated in any conclusions drawn from
this table.

**Read OQ-6 before drawing any MLX-versus-GGUF conclusion** — on the laguna
pair MLX prefills 4.6x faster while decoding 8% slower, so end-to-end ordering
depends on a workload's prefill/decode mix, and this table sits at one extreme
of it.

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

14 rows, **378/378 requests usable, 0 failed, 0 truncated**. Every request
ended `done_reason: stop`. Wall clock ~2h50m.

`gen` is p50 decode at C=1. `agg` is `aggregate_tok_s` at C=1/2/3. `ttft` is
time to first output of any kind; `ttfa` is time to first **answer** token —
they differ by the reasoning phase. `prefill` is server-reported
`load_duration + prompt_eval_duration`.

| Row | think | gen C=1 | agg C=1/2/3 | scale | ttft | ttfa | prefill | ITL | tokens |
|---|---|---|---|---|---|---|---|---|---|
| `nemotron3:33b` | native | **85.50** | 82.7 / 83.9 / 83.8 | 1.01x | 0.522 | 5.62 | 0.494 | 0.0117 | 1401 |
| `gemma4:26b-mxfp8` | native | 77.07 | 73.1 / 76.9 / 78.7 | 1.08x | 11.76 | 11.76 | 0.171 | 0.0130 | 1507 |
| `gemma4:12b-mlx` | off | 75.61 | 55.5 / 65.8 / 60.9 | 1.19x | 0.236 | 0.236 | 0.139 | 0.0132 | 547 |
| `laguna-xs-2.1:latest` | native | 73.15 | 72.2 / **104.9** / 87.6 | **1.45x** | 0.341 | 0.341 | 0.323 | 0.0137 | 1965 |
| `north-mini-code-1.0:mlx-nvfp4` | native | 72.06 | 73.3 / 69.7 / 68.7 | 1.00x | 0.115 | 4.84 | 0.080 | 0.0139 | 1515 |
| `gemma4:12b-mlx` | on | 70.19 | 63.7 / 58.3 / 62.4 | 1.00x | 0.489 | 11.88 | 0.115 | 0.0142 | 1430 |
| `laguna-xs-2.1:nvfp4` | native | 67.63 | 67.2 / 68.2 / 67.0 | 1.02x | **0.116** | **0.116** | **0.070** | 0.0148 | 1176 |
| `north-mini-code-1.0:mlx-nvfp4` *(repeat)* | native | 66.07 | 65.8 / 65.0 / 63.9 | 1.00x | 0.121 | 7.84 | 0.087 | 0.0151 | 1378 |
| `gemma4:12b-mxfp8` | off | 65.93 | 50.5 / 46.2 / 49.6 | 1.00x | 0.314 | 0.314 | 0.149 | 0.0152 | 550 |
| `qwen3.6:35b-mlx` | native | 48.73 | 48.4 / 48.2 / 48.4 | 1.00x | 0.174 | **40.97** | 0.116 | 0.0205 | 3557 |
| `gemma4:12b-mxfp8` | on | 47.99 | 48.6 / 45.8 / 47.8 | 1.00x | 0.490 | 14.71 | 0.186 | 0.0208 | 1462 |
| `qwen3.6:35b-mlx` | off | 46.67 | 46.3 / 48.6 / 45.9 | 1.05x | 0.173 | 0.173 | 0.115 | 0.0214 | 908 |
| `gemma4:12b-mlx-bf16` | on | 27.34 | 25.6 / 22.6 / 25.6 | 1.00x | 0.476 | 30.48 | 0.166 | 0.0366 | 1455 |
| `gemma4:12b-mlx-bf16` | off | 21.33 | 21.2 / 21.2 / 22.7 | 1.07x | 0.256 | 0.256 | 0.162 | 0.0469 | 561 |

### Read this before ranking anything

**Observed endpoint drift of 8.3% on an unchanged config.** The `north-mini`
config ran twice in this matrix — row 1 and row 14, byte-identical apart from
the output prefix — and measured **72.06 then 66.07 tok/s**, a **-8.3%
endpoint difference** across 2h50m of continuous GPU load.

Two caveats on how far that goes:

- **Two positions are not a trajectory.** This establishes that the endpoints
  differ by 8.3%; it does not establish a monotonic decline, a rate, or a
  general run-to-run variance figure. Intermediate positions were not sampled.
- **Thermal throttling is a hypothesis, not a measurement.** It is the obvious
  candidate for a laptop under sustained GPU load, but no thermal or clock data
  was captured, and nothing rules out an unrelated cause.

A third measurement of **86.6 tok/s** was taken during an earlier pass at this
config, but that run's result directory was deleted when the matrix was
restarted under the fixed harness, so it has **no committed artifact** and is
recorded here as context only — not as a data point.

Consequences:

- **Row position is a confound.** Early rows were measured on a cool machine,
  late rows on a hot one. `nemotron3` (row 2) and `gemma4:12b-mlx-bf16`
  (rows 12-13) are not on equal footing.
- **Differences under ~8% are not resolvable.** `laguna:latest` (73.15),
  `north-mini` (72.06) and `gemma4:12b-mlx` [on] (70.19) cannot be ordered by
  this data.
- Only the large gaps survive: the quantization ladder, the think-pair latency
  effect, laguna's concurrency scaling, and laguna MLX's prefill lead.

### What the data supports

**1. Quantization dominates decode, monotonically.** Same model, same thinking
mode, three quantizations — and these rows sit adjacent, so the drift above
affects them minimally:

| gemma4:12b [think:on] | size | gen C=1 | ITL |
|---|---|---|---|
| `-mlx` | 10 GB | 70.19 | 0.0142 |
| `-mxfp8` | 17 GB | 47.99 | 0.0208 |
| `-mlx-bf16` | 24 GB | 27.34 | 0.0366 |

A **2.6x decode spread** from a 2.4x size spread — consistent with
bandwidth-bound decode, where bytes moved per token sets the rate.

**2. Thinking has a consistent effect on latency and token count, and an
inconsistent one on throughput.** All **four** matched pairs:

| Model | ttfa on -> off | ratio | tokens on -> off | ratio | decode on -> off | Δ decode |
|---|---|---|---|---|---|---|
| `qwen3.6:35b-mlx` | 40.97 -> 0.173s | **237x** | 3557 -> 908 | 3.9x | 48.73 -> 46.67 | **-4.2%** |
| `gemma4:12b-mlx-bf16` | 30.48 -> 0.256s | 119x | 1455 -> 561 | 2.6x | 27.34 -> 21.33 | **-22.0%** |
| `gemma4:12b-mxfp8` | 14.71 -> 0.314s | 47x | 1462 -> 550 | 2.7x | 47.99 -> 65.93 | **+37.4%** |
| `gemma4:12b-mlx` | 11.88 -> 0.236s | 50x | 1430 -> 547 | 2.6x | 70.19 -> 75.61 | **+7.7%** |

**Consistent:** disabling thinking cuts time-to-first-answer by 47-237x and
output by 2.6-3.9x, in every pair and in the same direction.

**Not consistent:** decode moves by -22.0%, -4.2%, +7.7% and +37.4% — both
directions, and two of the four exceed the ~8% drift floor established above.
Do **not** read this as "thinking is free on throughput". The throughput effect
is artifact-dependent and this matrix does not explain it; note that the two
rows that got *faster* with thinking off are also the two whose output shrank
most relative to their decode rate, so shorter sequences and less KV growth are
a candidate, unisolated.

**The practical point stands regardless:** the deployed qwen3.6 config
(thinking off) answers in **0.17s, not 41s** — ranking it on its native-mode
`ttfa` would be wrong by a factor of 237 (OQ-5).

**3. The GGUF row gains most from concurrency, but it is not the only row above
1.0x.** Full ranking by `max(agg) / agg(C=1)`:

| Row | agg C=1/2/3 | scale |
|---|---|---|
| `laguna-xs-2.1:latest` (GGUF) | 72.2 / 104.9 / 87.6 | **1.453x** |
| `gemma4:12b-mlx` [off] | 55.5 / 65.8 / 60.9 | 1.186x |
| `gemma4:26b-mxfp8` | 73.1 / 76.9 / 78.7 | 1.077x |
| `gemma4:12b-mlx-bf16` [off] | 21.2 / 21.2 / 22.7 | 1.070x |
| `qwen3.6:35b-mlx` [off] | 46.3 / 48.6 / 45.9 | 1.050x |
| *(remaining 9 rows)* | | 1.000-1.016x |

`laguna:latest` at **1.45x** is the largest by a wide margin and the only one
attributable to llama.cpp batching with confidence. The others need
qualification rather than dismissal:

- `gemma4:12b-mlx` [off] reaches **1.186x**, but its own twin
  `gemma4:12b-mxfp8` [off] — same size class, same think setting — sits at
  **1.000x**. One of those two is noise, and this data cannot say which.
- The four rows between 1.05x and 1.19x are all short-output rows (547-561
  tokens for the think-off pair) where `aggregate_tok_s` is computed over
  fewer tokens and is correspondingly noisier.
- `nemotron3` at 1.015x is policy-pinned to one slot (OQ-1), so its flatness
  is explained rather than measured.

So the OQ-1 mechanism is visible — every row that could batch is GGUF, and the
one clear scaler is GGUF — but "every MLX row is flat" overstates it.

**4. MLX wins prefill, loses decode — on the same weights.** The laguna pair:

| | prefill | decode | ttfa |
|---|---|---|---|
| `:latest` (GGUF q4_K_M) | 0.323s | **73.15** | 0.341s |
| `:nvfp4` (MLX) | **0.070s** | 67.63 | **0.116s** |

**4.6x faster prefill, 8% slower decode.** End-to-end ordering therefore
depends on a workload's prefill/decode mix, and this matrix sits at one extreme
of it (OQ-3, OQ-6). `laguna:nvfp4` also posts the lowest `ttfa` and `prefill`
in the whole matrix.

**5. `gemma4:26b-mxfp8` shows an 11.8s `ttft`** where every other row is
sub-second, and its `ttft` equals its `ttfa` exactly. That means Ollama is not
splitting Gemma's `|channel thought|` markup into a separate `thinking` field,
so its reasoning is counted inside `response` — its `usable` rate is not
directly comparable with rows where the split works.

### What this does not establish

Per OQ-3 and OQ-6: this is short-prompt (~180-217 token), single-turn decode
without conversation-level cache reuse. It does not measure prefill throughput
at agent scale, multi-turn snapshot reuse, or output quality. **No
daily-driver recommendation follows from this table alone.**

## Finding — large MLX mxfp8 tags are infeasible on this machine

Two rows were dropped mid-run as **infeasible**, which is a result rather than
a gap:

| Tag | Size | Measured |
|---|---|---|
| `gemma4:12b-mxfp8` | 13 GB | 42.1 tok/s ✅ |
| `gemma4:26b-mxfp8` | 27 GB | 67.3 tok/s ✅ |
| `qwen3.6:35b-a3b-coding-mxfp8` | 37 GB | every request hit the 300s timeout ❌ |
| `laguna-xs-2.1:mxfp8` | 39 GB | **1.14 tok/s** ❌ |

At 1.14 tok/s a single 16384-token request needs ~4 hours, so the workload
cannot complete. For comparison the same model as `:nvfp4` (19 GB) measured
59.9 tok/s on an identical probe — a ~52x gap.

**This is a size effect, not an mxfp8 defect.** Both healthy mxfp8 tags run
normally on the same machine and stay in the matrix; only the 37-39 GB tags
collapse. On 64 GB of unified memory, weights at that size plus KV plus OS
leave no headroom.

Supporting but **not conclusive**: loading `laguna:mxfp8` took swap-in-use from
2.5 GB to 8.9 GB and grew the swap file from 4 GB to 10 GB. That probe loaded
at the 131072 server-default context (52 GB resident), whereas the benchmark
requested 32768 (39 GB resident), so it overstates the benchmark's own memory
pressure. A controlled measurement at fixed `num_ctx` is still owed before
calling swap the mechanism.

Configs for both rows are retained, so either can be restored if a future
Ollama or a larger machine changes the picture.

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

### RESOLVED 2026-07-28 — the warm-run test was run

`qwen3.6:35b-mlx`, checking `/api/ps` after each step:

| Step | Action | Resident `context_length` | Resized? |
|---|---|---|---|
| 1 | Cold load, request `num_ctx=32768` | **32768** | — |
| 2 | Warm request, `num_ctx=65536`, no stop | **32768** | **no** |
| 3 | `ollama stop`, cold request `num_ctx=65536` | **65536** | yes |
| 4 | Warm request *down* to `num_ctx=8192` | **65536** | **no** |

**Per-request `num_ctx` is honored only at cold load.** A resident runner
ignores it in **both** directions — it neither grows nor shrinks — exactly as
`sched.go` L1388-1444 predicted.

### The reported `context_length` is not an enforced cap

A first draft of this section claimed the mismatch causes **silent truncation**.
That was wrong, and the evidence for it was invalid: the four steps above ran
with `num_predict=4`, so their `done_reason: length` was fully explained by the
*output* budget and said nothing about context handling.

Tested properly on `gemma4:12b-mlx`, cold-loaded at `num_ctx=8192`, then sent a
**26,065-token prompt** on a warm request — with a distinct marker at the head
*and* at the tail:

```text
prompt_eval_count = 26065      (the whole prompt was evaluated)
done_reason       = stop
error             = none
/api/ps           = gemma4:12b-mlx@8192   (unchanged)
response          = "HEAD=ALPHA-7731\nTAIL=OMEGA-4482"
```

**Both markers were recalled.** A prompt 3.2x the reported resident context was
processed in full, with the tail intact, no error and no truncation. So
`/api/ps`'s `context_length` on the MLX runner is a **load-time bookkeeping
value, not a limit on what the runner will process**.

### What this means for the Modelfile workaround

Honestly: **weaker than the previous two justifications, both of which are now
retired.**

- The original reason — 0.31.1 ignored per-request `num_ctx` outright — is
  superseded by the cold-load behaviour above.
- The replacement reason — silent truncation at the resident size — is
  **disproven** by the marker test.

The workaround is retained, but on narrower grounds: `/api/ps` reports a number
that does not describe actual behaviour, so the system's real context limit on
this path is **not established by anything measured here**. What happens under
memory pressure, at genuinely large contexts, or on the llama.cpp path was not
tested. A baked value at least makes the *requested* context explicit and
inspectable rather than dependent on whichever client loaded the runner.

Note also that baking does **not** make the value deterministic on its own: the
section above establishes that an explicit request `num_ctx` overrides the
Modelfile at cold load. The tag is deterministic only if its clients omit
`num_ctx` or send the same value — `CLAUDE_CODE_MAX_CONTEXT_TOKENS` in the
`oclaudeq` alias is what keeps that true today.

**Still open**, and worth a dedicated test before relying on any of this: what
the effective context limit actually is on the MLX runner, and whether
exceeding it degrades quality silently rather than erroring.

All tests on ollama 0.32.5, MLX runner. The llama.cpp (GGUF) path was not
tested and its reload and context handling may differ.

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

Against ~1500 output tokens at the **48.73 tok/s** decode finally measured for
this model (native mode, `num_predict=16384`), a full turn is roughly
**50-76% prefill** (p50 ≈ 54%) — dominated by the phase this matrix barely
exercises.

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

### OQ-4 — Does MLX serialization make MLX *preferable* single-stream?

**Status: RESOLVED (both halves).** The causal question is **no** —
serialization implies nothing about C=1 speed. The artifact question was
answered by the completed laguna rows: `:latest` (GGUF) decodes fastest at
C=1, so MLX is not ahead on decode here either. Recorded because OQ-1's result
invites a conclusion it does not support.

**RESOLVED — serialization implies nothing about C=1 performance.** OQ-1 shows
Ollama's MLX runner has no continuous batching. That is an implementation
limit, not a design trade for single-stream work, and it carries no information
about per-stream speed. The absence of batching is a structural disadvantage
for *scaling and tail latency*; it is not automatically an absolute loss, since
a sufficiently faster serialized backend could still beat a slower batched one
at low concurrency.

**ANSWERED (2026-07-28) — `:latest` decodes fastest at C=1.** Measured:

| laguna tag | decode C=1 | prefill | ttfa | scale |
|---|---|---|---|---|
| `:latest` (GGUF q4_K_M) | **73.15** | 0.323s | 0.341s | **1.453x** |
| `:nvfp4` (MLX) | 67.63 | **0.070s** | **0.116s** | 1.016x |
| `:mxfp8` (MLX) | *not measurable* — 1.14 tok/s, dropped as infeasible | | | |

GGUF decodes **8% faster** and is the only one of the three that gains from
concurrency; MLX prefills **4.6x faster** and posts the matrix's lowest `ttfa`.
The 8% decode gap sits at the ~8% endpoint-drift floor, so treat it as
directional rather than precise; the 4.6x prefill gap and the 1.45x-vs-1.02x
scaling gap are both far above it.

**What the laguna rows can and cannot settle.** They are the best
same-model-family operational comparison in the matrix, but **not a clean
backend isolation** — backend and quantization vary together:

| Tag | Backend | Quant | On disk |
|---|---|---|---|
| `:latest` | llama.cpp | GGUF Q4_K_M | 20 GB |
| `:nvfp4` | MLX | NVFP4 | 19 GB |
| `:mxfp8` | MLX | MXFP8 | 39 GB |

So the defensible eventual conclusion is bounded:

> Among these three laguna artifacts, under this short-prompt decode workload,
> artifact X has the highest C=1 `gen_tps`; GGUF additionally provides C>1
> scaling. Because backend and quantization differ together, this does not
> isolate an MLX-versus-llama.cpp effect, and does not establish the best
> Claude Code daily driver.

**`:nvfp4` cannot be a daily-driver recommendation on speed alone.** Poolside's
card is explicit: *"This is an experimental NVFP4 MLX build of Laguna XS 2.1,
published for testing purposes only. It has not been validated for quality or
correctness and is not an official release. Do not use it in production."*
([card](https://huggingface.co/poolside/Laguna-XS-2.1-NVFP4-mlx), verified
2026-07-28). Raw decode speed from that artifact is a measurement, not a
recommendation.

**"Preferable" needs more than p50 `gen_tps`.** That column establishes only
*faster short-prompt decode*. Daily-driver preference also needs output
quality, memory footprint, load time, and long-prompt prefill/TTFT — which is
exactly the boundary OQ-3 records.

**Prior evidence, scoped.** INFO-1105 measured GGUF scaling 322 → 388 tok/s,
but that is **C=2→4 on a 1.5B FIM workload**, not C=1→3 on a 33B model, and
should not be read as the batching headroom expected here — the laguna rows
supply the relevant measurement. [[IDEA-1062]] (dropped) separately found that
removing queue-wait produced no perceptible speedup, on the grounds that decode
is memory-bandwidth-bound.

**Single interactive use is unaffected either way.** One Claude Code session
issues one request at a time and loses nothing to serialization. It bites when
fanning out parallel agents at a local model — the operational rule already in
[[IMPR-1066]] (shipped), which INFO-1105 cross-references.

**The pre-flight mxfp8 number is more likely a fault than a quantization
effect.** A 2026-07-28 probe put `:mxfp8` at ~2 tok/s against `:nvfp4` at ~75 —
a ~37x gap against a ~2x weight-size difference, which ordinary quantization
does not explain. **Reproduced 2026-07-28**: a direct probe measured
`:mxfp8` at **1.14 tok/s** against `:nvfp4` at 59.9 on the same model, and the
row was dropped as infeasible. The Finding section above attributes it to model
size on 64 GB unified memory (37-39 GB tags collapse, 13-27 GB tags do not)
rather than to MXFP8 as a format — but runner logs, CPU-vs-GPU residency and
output correctness were **not** inspected, so the mechanism remains unisolated.

**Correction.** An earlier draft of this section claimed INFO-1105's
113-122 tok/s for `qwen3.6:35b-mlx` versus this run differed because "context
length dominates". That is unsupported: INFO-1105 ran `num_ctx=8192 /
num_predict=256`, while this row averages **4,162 generated tokens** from a
178-token prompt on a newer Ollama. Generated sequence length, workload shape
and runtime all changed together; isolating a `num_ctx` effect needs a run that
varies only that. The same draft quoted this row at ~55 tok/s, a superseded
2048-budget figure; the final measured value is **48.73 tok/s** (an
intermediate 60.9 also circulated and is likewise superseded).

### OQ-5 — Large rows run thinking ON; the daily driver runs it OFF (SCOPE BOUNDARY)

**Status: RESOLVED — the missing configuration was added and measured.** The
`qwen36-35b-mlx-nothink` row (config
`pop-moe-qwen36-35b-mlx-nothink-agentic.toml`) is in the final table. Before it
existed the matrix contained no measurement of the baseline as deployed; it now
does, and the gap it exposed was a factor of 237 on `ttfa`.

Per the size rule, every large model here runs in **native mode with no `think`
key**, i.e. thinking enabled — Ollama's documented default for a model
advertising the `thinking` capability. For `qwen3.6:35b-mlx` that is not its
deployed configuration: the exact tag is `enable_thinking: false` in Zed's
inline assistant (`zed/settings.json`), and Pi's Qwen entries set
`reasoning: false` (`pi/agent/models.json`). Hard-disabling is also the
verified mitigation for the empty/garbled output and write-loop behaviour
tracked under [[BUG-1024]] — which is **still open**, with its Python/YAML
write-loop acceptance tests incomplete, so thinking is best described as
*associated with* those failures rather than proven to cause them.

Note the deployment picture is not uniform: the derived `qwen3.6:claude`
favourite is configured with `enable_thinking: true`, so "the daily driver
disables thinking" is true of the specific paths named above, not of every
Qwen usage on this machine.

**Why the native row cannot stand in — now measured, not argued.** Both rows
completed; final figures:

| `qwen3.6:35b-mlx` | decode C=1 | ttft | ttfa | tokens |
|---|---|---|---|---|
| native (thinking on) | 48.73 | 0.174s | **40.97s** | 3557 |
| `think: false` (deployed) | 46.67 | 0.173s | **0.173s** | 908 |

The 40.97s is not prefill — the agentic prompt is ~178 tokens and
`server_prefill_s` is 0.116s, as is time-to-first-*output* (0.174s). The gap is
a reasoning phase, and the harness distinguishes first reasoning output from
first answer content, so the distinction is real rather than an artifact.

At 48.73 tok/s, 40.97s corresponds to roughly **2,000 token-times of
reasoning** before the answer begins. Treat that as **inferred, not measured**:
the harness records only a total `eval_count` and does not separately tokenize
reasoning and answer, so any thinking/answer split is a derivation from
rate × time, not two counted quantities.

The matched row also confirms why no correction factor would have worked:
thinking moved `ttfa` by **237x**, tokens by **3.9x**, and decode by only
**-4.2%** — three different magnitudes in two directions, from one setting.

**Do not conflate this with the observed Claude Code slowness.** Sessions do
feel slow to start, but for a different reason: 77,466 prompt tokens reaching
first assistant output in ~36s — a **prefill-dominated startup**, with thinking
already off (OQ-3, and `claude-session-pop-qwen36-35b-20260707-184943.json`).
That artifact measures client-observed time to first assistant output and
includes ~2.8s of CLI initialisation; it carries no server
`prompt_eval_duration`, so "36s of pure prefill" would be too exact. The
distinction from a reasoning phase still holds. Two unrelated mechanisms
landing within a second of each other — ~37s of reasoning here, ~36s of
prefill-dominated startup there — which is precisely why they read as one
phenomenon.

**Consequence.** Native-mode `ttfa` cannot rank daily-driver configurations
that disable thinking, and the matched row proves the size of the error: 40.97s
against 0.173s for the same model. The three `gemma4:12b` think-pairs quantify
the effect for Gemma (47-119x) and Qwen's own pair for Qwen (237x) — the
spread across models is itself evidence that reasoning length is
architecture-specific and does not transfer, which is why the matched row was
required rather than an adjustment borrowed from Gemma.

**Generalisation, now that all four pairs exist:** any model whose deployed
configuration disables thinking must be ranked on its own `think=false` row.
Every large row in this table other than `qwen3.6:35b-mlx` still runs native
only, so the same caveat applies to them, unmeasured.

### OQ-6 — This workload omits MLX's claimed agent-workload advantages (SCOPE BOUNDARY)

**Status: a bound on what the results license. Read before drawing any
MLX-versus-GGUF conclusion.**

**The decisive evidence is in this matrix, not the vendor posts.** On the
laguna pair — same model, same workload — MLX already wins prefill decisively
while losing decode slightly, at C=1:

| laguna tag | prompt tok | server prefill p50 | decode p50 |
|---|---|---|---|
| `:latest` (GGUF q4_K_M) | 209.3 | 0.3229s | **73.15 tok/s** |
| `:nvfp4` (MLX) | 217 | **0.0699s** | 67.63 tok/s |

**MLX prefills 4.62x faster and decodes 8% slower.** End-to-end ordering
therefore depends on the prefill/decode mix of the workload, and this table
deliberately sits at one extreme of that mix (OQ-3: 97-99% decode). That is a
measured demonstration of workload dependence — it does **not** locate the
crossover point, which remains unmeasured.

**What the vendor posts do and do not establish.**
[Ollama, 2026-06-11](https://ollama.com/blog/mlx-performance) reports *"NVFP4
generates about 20% faster than q4_K_M ... Average output speed over 10 runs
when provided an **8,300-token input prompt**"*. Our laguna prompts are ~209
tokens, roughly **40x shorter**. But the vendor publishes no short-prompt
control, and our comparison changes model, quantized artifact, prompt length
and output shape simultaneously. So this establishes that **their result does
not generalise to laguna here** — it does *not* identify prompt length as the
cause, and it does not establish that short prompts are MLX's worst regime.

**The vendor's rationale agrees with OQ-3.** *"Agent workloads are dominated by
prompt processing. Every tool call is a new request, and every request resends
the whole transcript."* That is the vendor's own framing rather than an
independent confirmation, but it aligns with the boundary OQ-3 records.

**What is genuinely absent here is long prefill and realistic multi-turn
reuse** — not "every mechanism Ollama built". These rows *do* run MLX fused
kernels, NVFP4, the reworked GPU sampling, and Gemma MTP. What they do not
exercise is Ollama's snapshot system, which targets multi-agent handoff,
branching, retries and specifically thinking models: *"Reasoning tokens are
generated, then dropped from the conversation history, so the next request
never matches the state the engine just built."*

**This workload is not cache-cold.** `agentic_workload` cycles three static
prompts with no salting, and the harness warms up on `prompts[0]`, so each
prompt is submitted nine times per row and exact-prefix cache hits are
possible. What is absent is *conversation-level* reuse — snapshots, branching,
a growing transcript — not caching as such.

**Gemma 4 rows already include MTP; the direction of its effect here is
unknown.** MTP is on by default since Ollama 0.31
([2026-06-29](https://ollama.com/blog/faster-gemma-4-mlx-mtp)) and the
installed gemma4 manifests carry `draft.*` tensors, so it is active. Ollama
measured *"nearly 90% faster"* on Aider polyglot and cautioned that *"the
benefit from MTP depends heavily on the workload, and a synthetic benchmark
can be made to show almost any result."* Since MTP auto-tunes and falls back
to plain decoding when speculation stops paying, it should not *hurt* — but
nothing here supports claiming these numbers understate it.

**Consequence.** This table measures short-prompt, single-turn decode and does
not exercise realistic multi-turn snapshot reuse. It establishes that laguna
NVFP4 is not ahead **on decode in this workload**, while already being ahead on
prefill. Because the vendor's result uses a different model and workload, the
discrepancy cannot be attributed to prompt length. A matched long-prefill,
multi-turn comparison is required to determine whether end-to-end ordering
changes.

Note the March 2026 post's headline figures (prefill 1154 -> 1810 tok/s,
decode 58 -> 112 tok/s) compare **Ollama 0.18 to 0.19**, not MLX to llama.cpp
at a fixed version, and should not be cited as a backend comparison.
