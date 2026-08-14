# timmy (RX 9070 XT) model comparison matrix

Standing reference for models served on timmy's RX 9070 XT (16 GB GDDR6, Ollama
0.31.1, llama.cpp Vulkan backend, K3s deployment, `OLLAMA_KV_CACHE_TYPE=q8_0`).
The timmy counterpart to `2026-05-08-model-comparison-matrix.md` (which remains
the cross-host reference); rows here come from the TASK-1186 re-benchmark on the
standardized harness (IMPR-1016) — native Ollama API, in-cluster execution, no
proxy, per-row provenance in each run directory. Numbers are copied verbatim
from their run artifacts; nothing is estimated.

> **Methodology**: `concurrency-bench.py`, C=1..8 sweep, 8 requests × 3 repeats
> per level, NP=8 single-model isolation (deployment patched per run and
> restored). "mixed" = `mixed_mem0` @ 16K ctx / 512 predict; "agentic" =
> the concurrency harness's canned agentic workload @ 32K ctx / 2048 predict —
> NOT the `agentic-coding-bench.py` scored pass, which is separate and pending.
> Every config passed a coherence gate before its throughput job ran, but **not
> every gate ran at its row's benchmarked sampling**: the gate only began
> inheriting config sampling partway through the day (commit `09a03ea`). Only
> the two qwen3.5 rows were gated at their benchmarked temperature (0.3); the
> two gemma4 rows were gated at the pre-fix default (temp 1.0 / top_p 1.0) and
> benchmarked at 0.3, and nemotron matches only incidentally (gate and config
> are both temp 1.0). Re-gating the gemma4 rows at 0.3 is owed — see the
> pending ledger.
>
> **Sampling differs across rows.** gemma4 and qwen3.5 ran at temp 0.3 with
> `think` unset (server default); nemotron ran at temp 1.0 with `think = false`
> explicitly. That matters most for the usable column — see the caveat below.
>
> **Read the usable column before the throughput column.** Aggregate tok/s
> counts every generated token, thinking included. On the mixed workload's
> 512-token cap, a thinking-by-default model (gemma4) spends most of the budget
> reasoning: its mixed rows are valid **decode-rate** measurements but produced
> almost no complete answers. Rows with usable_rate 1.0 are the only ones that
> support end-to-end serving conclusions.
>
> **The usable column is not like-for-like *across* rows.** nemotron's mixed
> 24/24 was measured with `think = false`; gemma4's mixed 4/24 was measured with
> thinking on, on the same 512-token cap. That gap is substantially a config
> difference, not a model property — compare usable rates only within a row's
> concurrency sweep, or between rows that share a think posture, until the
> `think = false` reruns in finding 7 land.

## Throughput (current harness, 2026-08-14 unless noted)

| Model | Quant | Workload | C=1 agg tok/s | C=2 | C=4 | C=8 | P50 TTFT C=1 | Usable (C=1 → C=8) | Scaling shape |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- |
| `gemma4:12b-it-qat` | Q4 QAT | agentic 32K | 58.1 | 65.4 | 92.4 | **126.2** | 11.8s | **24/24 → 24/24, zero truncation** | **Scales 2.2× — the clean scaling result** |
| `gemma4:12b-it-qat` | Q4 QAT | mixed | 57.0 | 73.3 | 125.0 | 186.8 | 9.0s | 4/24 → 1/24 (all `length`; thinking ate the 512 cap) | Scales 3.3× — **decode-rate only, answers unusable** |
| `qwen3.5:9b-q4_K_M` | Q4_K_M | mixed | 79.6 | 82.3 | 82.2 | 82.2 | 0.35s | 11/24 → 12/24 (mostly `length`) | ⚠️ **INVALID — ran on 1 slot** |
| `qwen3.5:9b-q4_K_M` | Q4_K_M | agentic 32K | 82.8 | 83.6 | 83.5 | 83.5 | 0.41s | 13/24 → 14/24 (P95 TTFT 160s @ C=8) | ⚠️ **INVALID — ran on 1 slot** |
| `nemotron-3-nano:4b-bf16` | BF16 | mixed (2026-08-11) | 68.8 | 70.8 | 70.7 | 70.7 | 0.32s | 24/24 → 24/24 (9–12 truncated, 0 empty) | ⚠️ **INVALID — ran on 1 slot** |

> ⚠️ **The three rows marked INVALID are not concurrency measurements.** The
> 2026-08-14 slot probe (`slot-probe-20260814/`) established that `qwen3.5` and
> `nemotron-nano` were loaded with `n_seq_max = 1` — a single slot — while
> `NUM_PARALLEL=8` was set. A one-slot runner cannot batch by construction, so
> their flat curves measure the harness, not the model. Their **C=1 columns and
> TTFT figures remain valid** (every row used one slot at C=1); everything at
> C≥2 must be re-measured. See finding 8.

## Findings

1. **The clean scaling result is gemma4 agentic: 2.2× at 8 slots with 24/24
   usable, zero-truncation answers at every level.** The mixed 3.3× figure is
   real decode-rate scaling but its answers were 96% empty/truncated at C=8
   (thinking + the 512-token cap) — quote it only as a decode ceiling, never
   as serving capacity. ~~qwen3.5 and nemotron-nano both show the serialization
   signature; gemma4 remains the only model measured that scales under
   concurrency.~~ **RETRACTED 2026-08-14** — the other two were never given more
   than one slot, so they had no opportunity to batch. gemma4's own scaling
   stands (verified 8 slots), but **"the only model that scales" is not a
   supported claim**; it is untested for every other row. See finding 8.
   **The gemma4 curves are noisy, not smooth** — the table samples C=1/2/4/8
   and hides level-to-level reversals of up to ~13%: agentic dips to 103.5 at
   C=6 from 119.0 at C=5, and mixed dips to 142.9 at C=7 from 149.9 at C=6.
   The 2.2× and 3.3× endpoints are real, but treat the shape as an upward
   trend with ±13% level noise, not a monotone ladder.
   qwen3.5's serialization is the sharpest signal in the set: ITL is 0.0119 s
   and per-request `gen_tps` ~84.2 identical to four decimals at **every**
   concurrency level, on both workloads. **But "serializes" is an observation,
   not a diagnosis — see finding 8 for what is and isn't established.**
2. **qwen3.5:9b wins solo latency** (79.6 tok/s single-stream, sub-second
   TTFT vs gemma4's ~9–12s) and only ~46–63% of its answers were usable under
   these workloads — solo-latency pick; check output-budget fit before relying
   on it. **The "wrong for multi-session serving" half of this finding is
   withdrawn**: it rested on the flat curve, which the slot probe showed was a
   one-slot artifact. Its multi-session behavior is simply unmeasured.
3. **vs July (INFO-1047), like-for-like**: ~+21% aggregate (47.0 → 56.95
   C=1) or ~+17% per-request generation (51.3 → 60.1). The earlier "+12%"
   compared aggregate against per-request speed — wrong pairing. The delta is
   also **confounded** (different harness, context size, NP posture, Ollama
   version, and ROCm→Vulkan backend since July), so it is a then-vs-now
   observation, not evidence that any single layer "got faster".
4. **Thinking-mode coherence at a real budget is qualified, not clean**
   (`coherence-think-budget-20260814/`, `--think true --num-predict 4096`, run
   at **temp 1.0 / top_p 1.0** — a worst-case sampling point, not the 0.3 the
   throughput rows used): gemma4 passed 4/4 on first attempts — clean.
   qwen3.5 passed 4/4 only via retries (attempts 1/2/2/3), and its "passing"
   weekday answer contains a
   **literal `</think>` leak plus a stray token** that the checker's
   substring matching missed — a thinking-separation defect, now caught by a
   dedicated think-tag-leak check in the gate. Conclusion: no *systematic*
   Vulkan thinking garbling; gemma4 clean; qwen3.5 shows think-tag leakage
   and sampling flakiness.
5. **Quality note — qwen3.5:9b arithmetic flakiness**: answered 17×23 wrong
   on 1 of 3 sampled coherence gates at temperature 0.3 (and again at 1.0).
   Sampling-level, not backend; the gate now runs best-of-3 attempts and
   records attempt counts.
6. `nemotron-3-nano:4b-bf16` runs on the 0.31.1 pod (the ≥0.32.9 requirement
   applies to nemotron-3.5-lightning, not nano) — the agentic think/nothink
   pair (pop-mirrored, NP=3) is runnable whenever scheduled.
7. **Workload-design caveat for thinking models**: `mixed_mem0`'s 512-token
   cap cannot benchmark a thinking-by-default model end-to-end. Future mixed
   rows for think-capable models need `think = false` in the config or a
   larger `num_predict`, with the choice recorded.

8. **Why the two flat models serialize — one answer is settled, one is not.**
   The mechanism is unambiguous from the data: batching trades per-request
   latency for aggregate throughput, and gemma4 shows exactly that (ITL
   0.0168 → 0.0473 s, per-request generation 59.4 → 21.1 tok/s, aggregate
   58.1 → 126.2). qwen3.5 and nemotron instead hold ITL constant to four
   decimals across all eight levels, and their aggregate equals their
   single-stream rate. The queue arithmetic confirms strict FIFO: for
   qwen3.5 mixed, 512 tokens at 84.17 tok/s predicts **6.08 s** of added wait
   per concurrent request, and observed P50 TTFT increments are +5.94, +6.17,
   +6.17 s. nemotron matches its own prediction (~5.3 s) too; gemma4's
   increments (~1.4–3.5 s) fall far below its serialized prediction of 8.5 s,
   because its requests overlap.
   **RESOLVED 2026-08-14 by direct probe** (`slot-probe-20260814/`): the cause
   is slot allocation, not model behavior. Loading each model at its row's
   `num_ctx` under the benchmark posture and reading the runner's `n_seq_max`:

   | Model | `num_ctx` | `n_seq_max` | Scaled? |
   | --- | ---: | ---: | --- |
   | `deepseek-coder-v2:fim` (control) | 16384 | **8** | n/a |
   | `gemma4:12b-it-qat` | 32768 | **8** | yes — 2.2× |
   | `qwen3.5:9b-q4_K_M` | 32768 | **1** | no |
   | `qwen3.5:9b-q4_K_M` | 16384 | **1** | no |
   | `nemotron-3-nano:4b-bf16` | 16384 | **1** | no |

   Slot count correlates perfectly with observed scaling. Every "serializing"
   model had exactly one slot; a one-slot runner cannot batch, which alone
   explains the flat aggregate, the four-decimal-constant ITL, and the FIFO
   queue arithmetic — no model property is needed.

   - **This is not a simple VRAM ceiling.** `gemma4:12b-it-qat` is the *larger*
     model (8.0 GB resident vs qwen3.5's 6.1 GB) and got 8 slots at the *larger*
     context — 262144 total KV tokens versus the 16384 qwen3.5 received.
     Whatever caps qwen3.5 and nemotron at `n_seq_max = 1` is model-specific
     scheduler behavior; diagnosing it is separate follow-up work.
   - **nemotron's SSM explanation is now unsupported *by this row*.** The
     `nemotron_h` hybrid-Mamba serialization argument retains independent
     support from the pop MLX result (INFO-1140), but the timmy Vulkan row
     cannot corroborate it — a one-slot runner looks identical.
   - **What survives:** all C=1 figures, TTFT, single-stream decode rates, and
     the coherence/quality findings. Every row used one slot at C=1 regardless,
     so solo numbers are unaffected.

## Cross-host notes (vs pop, M5 Max)

- No same-model pairing with pop's `qwen3.6:35b-mlx` daily driver exists — the
  official qwen3.6 GGUF artifacts (~17–24 GB) exceed this card's VRAM. The
  planned byte-identical cross-host anchor is `gemma4:12b-it-qat` (library
  GGUF) pulled onto pop; that pop-side ladder run is still pending.
- pop's MLX path does not continuous-batch (aggregate ≈ single-stream); timmy's
  llama.cpp/Vulkan path demonstrably does — for gemma4-class models the dGPU
  wins multi-session workloads even where pop wins single-stream decode.
- nemotron family serializes on pop's MLX path (INFO-1140). The timmy Vulkan
  row **cannot corroborate that** — it ran on one slot (slot probe, 2026-08-14),
  so it is silent on whether `nemotron_h` batches under Vulkan. Excellent
  single-stream on both; fan-out behavior on Vulkan is unmeasured.

## Pending rows and passes (TASK-1186)

| Item | Status |
| --- | --- |
| **8K-ctx throughput ladder** (the task's pass 1) | not yet run — the rows above are 16K mixed / 32K agentic; the contract's `num_ctx=8192` ladder is still owed for every row |
| ~~qwen3.5 runner slot-count probe~~ | ✅ **done 2026-08-14** — `slot-probe-20260814/`. Result: 1 slot for qwen3.5 (both contexts) and nemotron, 8 for gemma4. Three rows invalidated |
| ~~Harness: capture runner parallelism~~ | ✅ **done 2026-08-14** — `capture_backend_proof()`'s grep widened to keep `n_seq_max` / `n_ctx_per_seq` / `new slot`, which it previously discarded. Future rows prove the parallelism they received |
| ~~gemma4 re-gate at benchmarked sampling~~ | ✅ **done 2026-08-14** — `coherence-regate-20260814/`, temp 0.3 / top_p 1.0, **4/4 first attempt, no think-tag leaks**. The gemma4 rows' precondition now holds at their benchmarked sampling |
| **Re-measure the 3 invalidated rows at 8 real slots** | **owed** — qwen3.5 mixed + agentic, nemotron bf16 mixed. Must assert `n_seq_max == 8` from the runner log before the numbers count; if the scheduler still caps at 1, that cap is the finding and the rows should be labeled `np=1` rather than presented as scaling curves |
| **Diagnose the `n_seq_max = 1` cap** | owed — why does a 6.1 GB model at 16K get one slot when a 8.0 GB model at 32K gets eight? Not a VRAM ceiling; likely scheduler KV estimation. Blocks trusting any future multi-slot row |
| **`think = false` mixed reruns** (gemma4, qwen3.5) | owed — needed before the mixed usable column is comparable across rows (nemotron already ran `think = false`) |
| **Scored agentic pass** (`agentic-coding-bench.py` @ 32K) | not yet run — executes from pop with `--base` (macOS-only sandbox); distinct from the canned agentic workload above |
| Prefill pass (real-payload corpus, cold + warm-prefix) | corpus committed (`benchmarks/ollama/corpus/`), runs from pop over LAN |
| `gemma4:coding-12b` anchor | **excluded** — built `FROM gemma4:12b-mlx`, cannot run on timmy; replaced by the pop-side `gemma4:12b-it-qat` anchor ladder (pending) |
| `gemma4:12b` Q8_0, `gemma4:e2b` FP16 | quant ladder — pull pending (disk-budgeted batches; 37 GB free vs ~75 GB remaining pulls) |
| `ministral-3:14b-instruct-2512-q4_K_M`, `qwen2.5-coder:14b-instruct-q4_K_M` | pull pending |
| `nemotron-3-nano:4b-q8_0` | pulled; configs live (`cluster-vulkan-nemotron-nano-q8-*`) |
| nemotron bf16 agentic think/nothink pair | configs live, NP=3, run with `BENCH_NUM_PARALLEL=3` |
| Single-slot tier (`gpt-oss:20b`, `devstral:24b`, opt. `devstral-small-2:24b`) | NP=1 / 8K ctx only, labeled `np=1` |

## Sources

Exact artifacts backing each row, all tracked in git and rooted at
`benchmarks/results/`. Two layout facts matter for auditing:

- **The run directories hold only `results.json`, `summary.csv`, `summary.md`.**
  The coherence, provenance, env, and backend-proof artifacts live one level up,
  in the enclosing `vulkan-<TS>/` fetch directory — not in the run dir.
- **Each fetch's `pod/` subdirectory carries co-copied artifacts from earlier
  runs** (byte-identical copies, verified by checksum — not divergent data, just
  redundant). Prefer the top-level `vulkan-<TS>/` path for a run's own gate,
  provenance, and env; use the `pod/<run-dir>/` path for its `results.json`.

| Row | `results.json` | Coherence gate (top-level in the same fetch) | Provenance / backend proof |
| --- | --- | --- | --- |
| gemma4 mixed | `vulkan-20260814T164006Z/pod/ollama-cluster-vulkan-default-20260814-170111/results.json` | `vulkan-20260814T164006Z/coherence-cluster-vulkan-default.json` (16:43:30Z, 4/4, temp 1.0) | `vulkan-20260814T164006Z/model-provenance-cluster-vulkan-default.txt`, `backend-proof-cluster-vulkan-default{,-load}.log` |
| gemma4 agentic | `vulkan-20260814T164006Z/pod/ollama-cluster-vulkan-agentic-20260814-175618/results.json` | `vulkan-20260814T164006Z/coherence-cluster-vulkan-agentic.json` (17:01:22Z, 4/4, temp 1.0) | `vulkan-20260814T164006Z/model-provenance-cluster-vulkan-agentic.txt`, `backend-proof-cluster-vulkan-agentic{,-load}.log` |
| qwen3.5 mixed | `vulkan-20260814T175855Z/pod/ollama-cluster-vulkan-qwen35-9b-default-20260814-182426/results.json` | `vulkan-20260814T175855Z/coherence-cluster-vulkan-qwen35-9b-default.json` (18:02:18Z, 4/4, temp 0.3) | `vulkan-20260814T175855Z/model-provenance-cluster-vulkan-qwen35-9b-default.txt`, `backend-proof-cluster-vulkan-qwen35-9b-default{,-load}.log` |
| qwen3.5 agentic | `vulkan-20260814T182610Z/pod/ollama-cluster-vulkan-qwen35-9b-agentic-20260814-194422/results.json` | `vulkan-20260814T182610Z/coherence-cluster-vulkan-qwen35-9b-agentic.json` (18:29:25Z, 4/4, temp 0.3) | `vulkan-20260814T182610Z/model-provenance-cluster-vulkan-qwen35-9b-agentic.txt`, `backend-proof-cluster-vulkan-qwen35-9b-agentic{,-load}.log` |
| nemotron bf16 mixed | `vulkan-20260811T212335Z/pod/ollama-cluster-nemotron-nano-bf16-default-20260811-214623/results.json` | `vulkan-20260811T212335Z/coherence-cluster-vulkan-nemotron-nano-bf16-default.json` (2026-08-11 21:26:49Z, 4/4, temp 1.0) | `vulkan-20260811T212335Z/model-provenance-cluster-vulkan-nemotron-nano-bf16-default.txt`, `backend-proof-cluster-vulkan-nemotron-nano-bf16-default{,-load}.log` |

Each fetch directory also has its own `cluster-vulkan-env.json`.

### Superseded coherence gates in the bundle — do not read these as row evidence

Three gate transcripts in the tracked bundle **failed** and were re-run; they
share filenames with the passing gates above and are distinguished only by
fetch directory and internal timestamp. They are kept for audit history:

| Superseded artifact | Result | Superseded by |
| --- | --- | --- |
| `vulkan-20260814T163454Z/coherence-cluster-vulkan-default.json` (16:38:37Z) | 5/8 — 3 empty answers under `think=true`, no `num_predict` override | 16:43:30Z gate above |
| `vulkan-20260814T164006Z/coherence-cluster-vulkan-qwen35-9b-default.json` (17:56:45Z) | 2/4 — arithmetic wrong, days out of order; ran at pre-fix temp 1.0 | 18:02:18Z gate above (temp 0.3) |
| `vulkan-20260814T175855Z/coherence-cluster-vulkan-qwen35-9b-agentic.json` (18:24:42Z) | 3/4 — arithmetic wrong | 18:29:25Z gate above |

The qwen3.5 pair is itself evidence for finding 5: the same probe set failed at
temp 1.0 and passed at the benchmarked 0.3.

| Other | Artifact |
| --- | --- |
| Runner slot probe (settles finding 8) | `slot-probe-20260814/README.md` + `runner-slot-lines.log` — note the runner lines are **transcribed**, not a re-capture: restoring the deployment rolled the pod and destroyed the original container log. Reproduction steps are in the README, and `capture_backend_proof()` now records these lines automatically |
| gemma4 re-gate at temp 0.3 | `coherence-regate-20260814/coherence-gemma4-12b-it-qat-temp0.3.json` — 4/4, all first attempt, attempt counts present, no think-tag leaks. Gate ran at `NUM_PARALLEL=2` over a port-forward (a coherence check, not a throughput run — slot count is irrelevant to it) |
| Think-budget transcripts | `coherence-think-budget-20260814/{gemma12bqat,qwen35-9b}.json` — note the **per-probe attempt counts are only in the sibling `.log` files** (gemma 1/1/1/1, qwen 1/2/2/3); the JSONs predate the attempts field. Both JSONs also still record `passed: true` for the leaking qwen weekday probe — they predate the think-tag-leak check (`benchmarks/ollama/tools/coherence-smoke.py:253`) and were not re-run against the hardened gate |
| Vault records | TASK-1186 (methodology, selection), TASK-1013 (nemotron), INFO-1047 (July baseline), INFO-1140 (nemotron_h serialization on pop) |
