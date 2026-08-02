# gemma4 authoritative matrix — pop, 2026-07-31/08-01 (PROJ-1003)

Successor to [`../report-2026-07-28-moe-matrix/README.md`](../report-2026-07-28-moe-matrix/README.md).
That run stated three limits on itself: 8.3% endpoint drift on a byte-identical
repeated config, row position as a confound, and "differences under ~8% are not
resolvable". This run was built to fix those, and to answer one question no
prior benchmark had touched — how the daily-driver models behave at the 131072
context that opencode and pi actually request.

**Headline: `gemma4:coding-26b-nvfp4` replaced `gemma4:coding-26b` as the
opencode/pi default.** That is the only decision this run supports outright.
Several other findings are interesting but weaker than they first appeared, and
two claims made during the investigation are corrected below.

**2026-08-01 rerun** (`../authoritative-20260801/`): tiers C and D, the
determinism control, and the agentic bench were rerun the same evening under
the updated tooling. Outcomes are folded into each section below; the short
version is that the 12b family reproduced within ~1.6% against a clean drift
control, the tier C -8.1% anomaly did not reproduce, the 31b re-measured at
~193 prefill (making the original tier D session the outlier), and the agentic
interaction-efficiency observation reproduced in direction at n=2.

## Method

| Property        | Choice                                                                     |
| --------------- | -------------------------------------------------------------------------- |
| Harness         | `prefill-size-breakdown.py` ONLY — one request shape for every cell        |
| Repeats         | 3 per model, order rotated per repeat                                      |
| Drift control   | `gemma4:12b-mlx`, same config + request shape, first AND last row per tier |
| Per-row capture | `pmset -g therm`, swap, `ollama ps` residency, live server env             |
| Prompt sizes    | 500 / 2000 / 8000 tokens, salted per run against prefix-cache hits         |
| Reported below  | the 8000-token bucket, median of 3                                         |
| Runner          | `../../ollama/tools/run-pop-gemma4-authoritative.sh`                       |

`concurrency-bench.py` was deliberately NOT mixed in. Decode figures from the
two harnesses do not compose — `gemma4:26b-mxfp8` reads 77.07 tok/s under the
07-28 agentic workload (`num_predict` 16384) and 95.8 here (`num_predict` 512).
Restating those under one column heading is how the 07-28 numbers and this
run's numbers get silently averaged. The concurrency and usable-rate columns
stay in the 07-28 report where they were measured.

## Results — 8000-token bucket, median of 3

| Tier | Model                     | Type  | Weights | Ctx  | Prefill tok/s | Decode tok/s | TTFT   |
| ---- | ------------------------- | ----- | ------- | ---- | ------------- | ------------ | ------ |
| A    | `gemma4:coding-26b-nvfp4` | MoE   | 17 GB   | 131K | **1410.8**    | **112.7**    | 4.64s  |
| A    | `gemma4:coding-26b`       | MoE   | 27 GB   | 131K | 1372.5        | 91.9         | 4.74s  |
| B    | `gemma4:26b-mlx` (nvfp4)  | MoE   | 17 GB   | 32K  | 1418.1        | 110.8        | 4.61s  |
| B    | `gemma4:26b-mxfp8`        | MoE   | 27 GB   | 32K  | 1379.1        | 95.8         | 4.73s  |
| C    | `gemma4:12b-mlx-bf16`     | dense | 24 GB   | 32K  | 591.2         | 55.1         | 10.89s |
| C    | `gemma4:12b-mxfp8`        | dense | 13 GB   | 32K  | 513.6         | 77.3         | 12.52s |
| C    | `gemma4:12b-mlx` (nvfp4)  | dense | 7.7 GB  | 32K  | 505.1         | 86.8         | 12.76s |
| D    | `gemma4:31b-nvfp4`        | dense | 18 GB   | 32K  | 159.2         | 41.7         | 40.34s |

Drift controls (`gemma4:12b-mlx`, same configuration and request shape, open vs
close of each tier — prompts are freshly salted per invocation, so the inputs
are not byte-identical):

| Tier | Prefill drift | Decode drift |
| ---- | ------------- | ------------ |
| A    | +0.6%         | -4.2%        |
| B    | -0.1%         | -3.0%        |
| C    | **-8.1%**     | +3.7%        |
| D    | -1.1%         | -3.8%        |

## Read this before ranking anything

**Prefill and decode have different noise floors, and the 07-28 report's flat
"under 8% is unresolvable" rule was wrong in both directions.**

| Metric  | Repeat spread (n=3) | Drift control (A/B/D) |
| ------- | ------------------- | --------------------- |
| Prefill | 0.5-1.1%            | 0.1-1.1%              |
| TTFT    | 0.6-0.7%            | —                     |
| Decode  | **10.9-15.6%**      | 3.0-4.2%              |

Prefill differences of 2-3% are resolvable. Decode differences under ~15% are
not, without repeats. Any future comparison must repeat decode and need not
repeat prefill.

**Tier C's drift control failed in this run — and the rerun cleared the tier.**
Its control moved -8.1% on prefill against 0.1-1.1% everywhere else, so the
12b rows were initially provisional. The 2026-08-01 rerun of the full tier came
back with a clean control (+1.1% prefill) and reproduced all three 12b models
within ~1.6% of the numbers above, so the published tier C rows stand.

Tier C coincided with +5.30 GB endpoint swap growth and -8.10% control prefill
drift; tiers A, B, and D each grew swap by only 0.3-0.9 GB. Memory pressure was
the leading hypothesis, but the rerun weakened it: tier C swap grew comparably
again (+4.2 GB, climbing 3.9 -> 9.7 GB across the tier) with NO drift, so
endpoint swap growth looks like a property of a three-model tier cycling
loads on this machine, not the cause of the 07-31 drift. The cause remains
unidentified and thermal effects remain unexcluded — the runner captured
aggregate swap only once, before each row; no temperature, frequency,
swap-compression, page-in/out, or memory-pressure timeline exists. The
original control's +3.7% decode movement is below this run's own ~15% decode
noise floor and carries no signal.

**Cross-session variance is much larger than within-tier drift.**
`gemma4:31b-nvfp4` has now measured 190.0 prefill / 51.2 decode (2026-07-31),
159.2 / 41.7 (tier D, 2026-08-01 morning), and 192.9 / 53.9 (rerun, 2026-08-01
evening, n=3, clean +0.3% control) — a 16-19% spread while every session's own
internal drift stayed under 4%. Two of three sessions agree at ~190-193, which
makes the 159.2 session the likely outlier, but nothing measured explains what
made it slow. Numbers from different sessions should not be compared at better
than ~20%, regardless of how tight the within-tier error bars look.

## What the data supports

### 1. nvfp4 beats its mxfp8 twin, at both contexts (the config decision)

| Context | Prefill delta | Decode delta | Residency |
| ------- | ------------- | ------------ | --------- |
| 131072  | **+2.8%**     | +22.6%       | -10 GB    |
| 32768   | **+2.8%**     | +15.7%       | -10 GB    |

Prefill's +2.8% is identical to three significant figures across a 4x context
change and two different model builds. Decode reproduces in direction with the
magnitude scatter expected of a metric whose own repeat noise is 10-16%.

The tier A decode ranges do not overlap:

```text
mxfp8 [82.4, 96.7]      nvfp4 [106.9, 119.2]
```

Every nvfp4 repeat beat every mxfp8 repeat, and the gap is 5.6x that tier's
measured drift. This is the one claim in the run that clears the 07-28 report's
own resolvability bar with room to spare.

**Acted on**: `dotfiles/opencode/opencode.jsonc` (`model` + `small_model`),
`dotfiles/pi/agent/settings.json` (`defaultModel`), and both provider entries in
`dotfiles/pi/agent/models.json` now point at `gemma4:coding-26b-nvfp4`.

### 2. 131K costs throughput nothing; it costs residency

`gemma4:26b-mlx` @32K (1418.1 / 110.8) and `gemma4:coding-26b-nvfp4` @131K
(1410.8 / 112.7) are within noise of each other. Declaring the large window is
free in speed terms. Its only cost is memory — and that cost was the real
problem: the mxfp8 twin was observed resident at **36 GB** at 131K, inside the
band where large mxfp8 tags degrade on this 64 GB machine. Moving to nvfp4
removes it.

### 3. Prefill scales inversely with ACTIVE parameters

gemma4 26B is MoE at 26B/4B active (per `benchmarks/harbor/README.md`); the 12b
and 31b are dense. Multiplying active parameters by prefill rate:

| Model                     | Active | Prefill | Effective    |
| ------------------------- | ------ | ------- | ------------ |
| `gemma4:coding-26b`       | 4.0B   | 1372.5  | 11.0 TFLOP/s |
| `gemma4:coding-26b-nvfp4` | 4.0B   | 1410.8  | 11.3         |
| `gemma4:26b-mxfp8`        | 4.0B   | 1379.1  | 11.0         |
| `gemma4:26b-mlx`          | 4.0B   | 1418.1  | 11.3         |
| `gemma4:12b-mlx`          | 12.4B  | 505.1   | 12.5         |
| `gemma4:12b-mxfp8`        | 12.4B  | 513.6   | 12.7         |
| `gemma4:12b-mlx-bf16`     | 12.4B  | 591.2   | 14.7         |
| `gemma4:31b-nvfp4`        | 31.7B  | 159.2   | 10.1         |

Median **~11.3 TFLOP/s**, spread **40%** (10.1-14.7) across a 7.9x
active-parameter range. The inverse relationship is real and useful — it
predicts a model's prefill before you download it — but it is a rule of thumb
with 40% error, not a constant. An earlier draft of this analysis reported
±14% from four points; adding the 12b family widened it, and tier C's bad drift
control accounts for part of that.

**This retires the "31b prefill is anomalous" reading.** Its 159-193 tok/s is
simply the price of 31.7B active parameters. Nothing is broken; the model is
7.9x more expensive per prefill token than the 26B MoE.

### 4. The 31b dense is disqualified for interactive use

At ~159-193 tok/s prefill, the measured 77,466-token Claude Code turn (07-28
report, `claude-session-pop-qwen36-35b-*.json`) needs **7-8 minutes to first
token**. TTFT at just 8000 tokens is 33-40s against the 26B MoE's 4.64s. No
decode advantage exists to offset this — its decode is the slowest in the table.

## Corrections

### Decode is NOT bandwidth-bound; `570 GB/s ÷ weights` is not a predictor

An independent MLX streaming probe (`copy`/`scale`/`sum`, 64 MiB->4 GiB,
best-of-7, idle GPU) measured **570.1 GB/s** achievable. That measurement
stands. Its use as a decode model does not:

| Dense model           | Weights | Decode | Implied  | vs peak  |
| --------------------- | ------- | ------ | -------- | -------- |
| `gemma4:12b-mlx`      | 7.7 GB  | 86.8   | 668 GB/s | 117%     |
| `gemma4:12b-mxfp8`    | 13 GB   | 77.3   | 1005     | 176%     |
| `gemma4:12b-mlx-bf16` | 24 GB   | 55.1   | 1322     | **232%** |
| `gemma4:31b-nvfp4`    | 18 GB   | 41.7   | 751      | 132%     |

Every dense model exceeds the ceiling, so none is bandwidth-bound under this
harness — MTP speculative decoding dominates, emitting more than one token per
weight pass. During the investigation `12b-mlx-bf16` was cited as a clean
bandwidth reference "at 90% of peak"; that used the 07-28 agentic decode figure
(21.33 tok/s) against this run's weights — precisely the cross-harness mixing
this report's Method section forbids. Measured on this harness it is 55.1 tok/s
and the agreement vanishes.

Weight size still drives decode _within a model family_ (the nvfp4-vs-mxfp8
result above, and the monotonic 12b ladder 86.8 > 77.3 > 55.1 as weights grow
7.7 -> 13 -> 24 GB). It does not predict an absolute rate.

### Greedy decoding is non-deterministic on every gemma4 tag

A 4-probe control on 2026-07-31 flagged only `gemma4:12b-mxfp8`. At 12 probes,
across two independent runs:

```text
                      2026-08-01 morning              2026-08-01 rerun
gemma4:12b-mlx-bf16   NON-DETERMINISTIC (1/12)        NON-DETERMINISTIC (2/12)
gemma4:12b-mxfp8      NON-DETERMINISTIC (2/12)        NON-DETERMINISTIC (2/12)
gemma4:12b-mlx        NON-DETERMINISTIC (1/12)        OK (0/12)
```

At `temperature 0`, `top_k 1`, `top_p 1`, fixed seed, **~8-17% of prompts do not
reproduce**. This is a property of the stack, not of one tag — MTP is the
obvious suspect and is not confirmed. Which tags flag varies run to run (the
rerun passed `12b-mlx` clean) while the overall rate stays in the same band,
which is exactly the stochastic behavior the single-tag reading missed.

Consequence: `quant-divergence.py` correctly **refused** to report the 12b
fidelity comparison, in both runs (the bf16 reference failed its control both
times). It is unanswerable on this stack until determinism is resolved or
speculation can be disabled.

## Quantization fidelity — partial, and weaker than it looks

Run on 2026-07-31 under the 4-probe control now known to be insufficient:

| Comparison                                   | Exact match   | Edit ratio |
| -------------------------------------------- | ------------- | ---------- |
| `coding-26b-nvfp4` vs `coding-26b` (peers)   | 95.8% (23/24) | 0.0417     |
| `12b-mlx` vs `12b-mlx-bf16` (true reference) | 83.3% (20/24) | 0.1667     |

The 26b twins agreeing on 23 of 24 prompts is not reachable by chance, so the
quant pair is substantially the same model — enough to say the choice is
low-stakes. But with ~8-17% per-prompt non-determinism now measured, the single
divergence cannot be attributed to quantization rather than run-to-run noise,
and neither row is a clean fidelity measurement. There is no bf16 26B locally,
so that row was never a fidelity test in the first place.

## Quality — tied at the ceiling; an efficiency observation (n=2)

`agentic-coding-bench.py`, 6 verifier-scored tasks, tiers 1-3:

| Model              | Solved  | Bad tool calls | T1    | T2     | T3     |
| ------------------ | ------- | -------------- | ----- | ------ | ------ |
| `gemma4:26b-mxfp8` | **6/6** | 0/52           | 53.3s | 240.4s | 185.4s |
| `gemma4:31b-nvfp4` | **6/6** | 0/30           | 76.1s | 104.0s | 333.7s |

A ceiling effect: both solved everything, so the instrument cannot rank them.
This was predicted before the run — at n=6 the standard error on a pass rate is
~20%, which cannot resolve the 1-3% gaps typical of a quant or size comparison.
Both models tripped the anti-cheat (`rewrote fixture suite(s)`); the harness
restored the fixtures before verifying, so the scores are valid.

Beneath the tied pass rate, both independent six-task runs show the same
interaction-efficiency difference:

| Metric        | Run 1 `26b-mxfp8` | Run 1 `31b-nvfp4` | Run 2 `26b-mxfp8` | Run 2 `31b-nvfp4` |
| ------------- | ----------------: | ----------------: | ----------------: | ----------------: |
| Solved        |               6/6 |               6/6 |               6/6 |               6/6 |
| Turns         |                56 |                36 |                57 |                42 |
| Tool calls    |                52 |                30 |                51 |                36 |
| Server tokens |           180,343 |            72,646 |                 — |                 — |
| Wall time     |            479.1s |            513.8s |            420.1s |            474.7s |

In both repeats the 31b consumed fewer model turns and tool calls while both
models solved 6/6, and was slower in wall time (+7.2%, +13.0%). This is a
hypothesis-generating interaction-efficiency observation, not a quality
ranking. Turns count model requests until the model stops or hits the 14-turn
cap — not time-to-first-passing-solution — and 26b tasks hit the cap in both
runs, which inflates its totals mechanically. n=2 establishes the direction
reproduces; it is still far short of the repeat count a published comparison
needs. And architecture, size, and quantization all differ between the two
models, so none of the three — in particular parameter density — is isolated
by this pairing.

**The dense-31b quality hypothesis remains untested.** It was the only axis that
could have justified the 31b's speed cost, and this instrument cannot address
it. The efficiency observation above is the concrete thing a repeat-backed
follow-up would test.

## `gemma4:31b-mxfp8` — deleted, with one caveat

Pulled and probed 2026-07-31, then deleted. Measurements, **n=1**:

- 9.61 tok/s decode at `num_ctx` 32768 (single 128-token generation)
- 32 GB resident on short prompts, **42 GB at 8k prompts**
- Its prefill ladder produced no rows in ~25 minutes and was killed

At 32 GB of weights an ideal bandwidth-bound dense decode would reach
570/32 = 17.8 tok/s; it managed 9.61. That was originally read as "kernel-bound
at 54% of peak", but given the correction above — that no dense model here is
bandwidth-bound — the ratio is not interpretable. What survives is that it was
**5.5x slower than the 31b nvfp4 twin**, on one sample, corroborated only by
its 42 GB residency and its failure to complete a ladder. That is enough to
justify deleting it and not enough to publish a number. Being re-pulled
elsewhere for an independent check.

## What this does not establish

- **No quality ranking of any kind.** The task bench hit its ceiling and the
  fidelity comparison was invalidated by non-determinism.
- **No absolute decode model.** Only within-family, same-harness comparisons.
- **Nothing about concurrency.** Every row is C=1. The 07-28 report is still the
  only source for concurrency scaling and usable-rate.
- **Nothing about multi-turn cache reuse.** Prompts are salted to defeat prefix
  caching, which is the opposite of a real agent session's growing transcript
  (07-28 OQ-6).
- ~~Nothing about the 12b family with confidence~~ — resolved by the 08-01
  rerun: the family reproduced within ~1.6% against a clean drift control.

## Open questions

1. **Is MTP the source of the non-determinism?** Would also confirm the
   above-peak (over 100% of 570 GB/s) decode readings. In Ollama v0.32.5 the
   documented Modelfile switch is `draft_num_predict 0` (not `num_draft`), and
   it is wired into the llama.cpp server path only — reading the v0.32.5
   source, the native MLX scheduler passes just model and context into its
   client while the MLX runner constructs speculation at load time, so the
   switch appears NOT to reach the MLX path these tags run on. Verify runtime
   behavior (e.g. that disabling it actually changes decode rate) before
   trusting any MTP-disabled comparison.
2. **Why did tier C drift -8.1%?** Still open, and harder now: the 08-01 rerun
   came back clean (+1.1%) while swap grew comparably (+4.2 GB vs +5.30 GB), so
   endpoint swap growth alone does not produce the drift and the
   memory-pressure hypothesis is weakened. Whatever slowed the 07-31 tier C —
   like whatever slowed the 159-tok/s tier D session — was transient and left
   no signature in the captures. Any further chase needs continuous telemetry;
   note that on this host `powermetrics --samplers smc` fails with
   "unrecognized sampler" and `smctemp` is not installed, so it must use
   supported continuous thermal, power-limit, and frequency sampling instead.
3. **Does the 31b dense win on quality?** Needs an instrument with a real
   discrimination floor — more tasks, or a graded rubric, not 6 binary
   outcomes. The n=2 turn/tool-call efficiency signal above is the concrete
   hypothesis to test.
4. **Why is cross-session variance 16-19%** when within-tier drift is under 4%?
   Now three 31b sessions deep (190 / 159 / 193): the slow session is the
   outlier, and nothing captured distinguishes it.
5. **Does `gemma4:31b-mxfp8` reproduce its 9.61 tok/s elsewhere?** Pending the
   independent pull.

## Artifacts

```text
authoritative-20260731/
├── {A,B,C,D}-r{0..3}-{label}-{model}-ctx{32768,131072}.json   per-row summaries
├── ...env.txt / ...ps.txt                                     therm, swap, residency
├── runner.log / remainder.log                                 execution logs
├── divergence-12b.json / divergence-26b-coding.json           fidelity (see caveats)
├── determinism-12b.log                                        12-probe control
└── ../agentic-coding-gemma4_{26b-mxfp8,31b-nvfp4}-2026080*.json

../authoritative-20260801/                                     08-01 rerun (C, D,
    same layout as above, incl. its own remainder.log,          determinism,
    determinism-12b.log, and agentic JSONs/logs                 agentic)
```

Tools: `run-pop-gemma4-authoritative.sh`, `run-pop-overnight-remainder.sh`,
`quant-divergence.py` (all in `../../ollama/tools/`).
