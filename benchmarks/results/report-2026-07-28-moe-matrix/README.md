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

Against ~1500 output tokens at the **60.9 tok/s** decode measured for this
model at `num_predict=16384`, a full turn is roughly **55-80% prefill**
(p50 ≈ 59%) — dominated by the phase this matrix barely exercises.

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

**Status: split.** The causal question is **RESOLVED — no**. What remains
**OPEN** is narrower and artifact-level: which of the three laguna builds has
the highest C=1 decode. Recorded because OQ-1's result invites a conclusion it
does not support.

**RESOLVED — serialization implies nothing about C=1 performance.** OQ-1 shows
Ollama's MLX runner has no continuous batching. That is an implementation
limit, not a design trade for single-stream work, and it carries no information
about per-stream speed. The absence of batching is a structural disadvantage
for *scaling and tail latency*; it is not automatically an absolute loss, since
a sufficiently faster serialized backend could still beat a slower batched one
at low concurrency.

**OPEN — which laguna artifact decodes fastest at C=1.** Nothing measured so
far bears on it. The fastest MLX row here (`north-mini` 86.6 tok/s C=1) and the
fastest GGUF row (`nemotron3` 83.7 tok/s) are different models, so that
comparison is void.

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
does not explain. Treat it as a suspected pathological kernel/runtime or CPU
fallback path until reproduced. If the row confirms it, check runner logs,
CPU-vs-GPU residency, memory pressure, and output correctness **before**
interpreting it as a property of MXFP8.

**Correction.** An earlier draft of this section claimed INFO-1105's
113-122 tok/s for `qwen3.6:35b-mlx` versus this run differed because "context
length dominates". That is unsupported: INFO-1105 ran `num_ctx=8192 /
num_predict=256`, while this row averages **4,162 generated tokens** from a
178-token prompt on a newer Ollama. Generated sequence length, workload shape
and runtime all changed together; isolating a `num_ctx` effect needs a run that
varies only that. The same draft quoted this row at ~55 tok/s, which was the
superseded 2048-budget figure — it is **60.9 tok/s** at 16384.

### OQ-5 — Large rows run thinking ON; the daily driver runs it OFF (SCOPE BOUNDARY)

**Status: a missing configuration, not merely a caveat.** Addressed by adding
a `qwen36-35b-mlx-nothink` row (config
`pop-moe-qwen36-35b-mlx-nothink-agentic.toml`). Until that row exists, the
matrix contains no measurement of the baseline as deployed.

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

**Why the native row cannot stand in.** `qwen3.6:35b-mlx` measures `ttfa` =
37.4s at C=1. That is not prefill — the agentic prompt is 180 tokens and
`server_prefill_s` is ~0.11s, as is time-to-first-*output* (0.156s). The gap is
a reasoning phase; the harness distinguishes first reasoning output from first
answer content, so this is a real distinction rather than an artifact.

At 60.9 tok/s, 37.4s corresponds to roughly **2,280 token-times of reasoning**
before the answer begins. Treat that as **inferred, not measured**: the harness
records only a total `eval_count`, and does not separately tokenize reasoning
and answer, so the "~2,280 thinking plus ~1,880 answer" split is a derivation
from rate × time, not two counted quantities.

Thinking changes more than `ttfa` — it also moves total tokens, wall time,
truncation risk, context consumption, and potentially answer quality. No single
adjustment recovers the deployed configuration from the native row, which is
why the matched row is needed rather than a correction factor.

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
that disable thinking. The **three** `gemma4:12b` think-pairs (six rows)
quantify that effect **for Gemma only** — reasoning length and quality effects
are architecture- and prompt-specific and do not transfer to Qwen. Qwen
requires its own matched `think=false` row before its interactive latency or
output behaviour can be compared against its deployed configuration.
