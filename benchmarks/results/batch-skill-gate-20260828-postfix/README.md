# Batch-skill gate — post-fix rerun (2026-08-28) — SUPERSEDED

> **Superseded by [`../batch-skill-gate-20260828-postfix2/`](../batch-skill-gate-20260828-postfix2/).**
> The summary scores below are inflated by a defect in the gate's leading-digit
> oracle: it checked only ID/PR/date/version/backtick tokens, so a model that
> deleted the `2 items -` (with a trailing space) prefix — dropping the count, itself a fact — scored a
> pass. Fixed in homelab `94c5a8a`; the corrected rerun puts both editors back at
> 12/20 (no change from the pre-fix run) and every model at 0/5 on leading-digit.
> Also note `--repeats 3` did not repeat the fence set in this run.
> Kept as evidence of the defect, not as a result.

Rerun of the IDEA-1090 partner gate after the two skill-text fixes in
`compendium-batch-summary-repair` (fact-preserving rewrites for the
leading-digit and colon-space conflicts; "report instead" scoped to outcome
uncertainty). Same cases as the first run, three repeats each, majority per
case. Tool: `benchmarks/ollama/tools/batch-skill-gate.py`; cases: `cases.json`
(pinned from the first run via `--from-cases`); per-model detail: `<model>.json`;
run log: `postfix.log`.

Server: timmy RX 9070 XT, Ollama **0.33.1**, Vulkan, production env
(`NUM_PARALLEL=2`, `MAX_LOADED_MODELS=1`, `KV_CACHE_TYPE=q8_0`,
`CONTEXT_LENGTH=131072`); each candidate loaded alone (evicting the FIM runner),
`num_ctx 16384`, `think:false`, temperature 0, `/api/chat` with a JSON-schema
`format`. Models ran sequentially (one local-model consumer); FIM re-warmed
load-only at the end.

**Pinned cases**: `summary_cases` and `fence_cases` are byte-identical to the
first run's `cases.json` (verified — the A/B's validity condition). Only the
skill texts were re-captured, from `idea-batch-agent` at commit `4c86d600`
(the Phase 1 skill edit). `--repeats 3`, per-case verdict = majority (ties
resolve to the worse outcome).

## Results

| Model               | Summary pass | reported | token loss | schema fail | halluc. | bad JSON | Fence     | wall p50 | decode    | load   |
| ------------------- | ------------ | -------- | ---------- | ----------- | ------- | -------- | --------- | -------- | --------- | ------ |
| `qwen3.5:9b-q4_K_M` | **15/20**    | 0        | 1          | 4           | 0       | 0        | 26/30     | 2.0 s    | 82 tok/s  | 8.9 s  |
| `gemma4:e4b-it-qat` | **14/20**    | 0        | 0          | 6           | 0       | 0        | 26/30     | 1.3 s    | 104 tok/s | 9.9 s  |
| `ornith-1.5:9b`     | 7/20         | **12**   | 0          | 1           | 0       | 0        | **27/30** | 1.6 s    | 85 tok/s  | 9.6 s  |

## Pre/post per-mode (summary set, pass counts)

| Mode          | qwen3.5:9b pre → post | gemma4:e4b pre → post | ornith-1.5:9b pre → post |
| ------------- | --------------------- | --------------------- | ------------------------ |
| over-length   | 4/5 → 4/5             | 4/5 → 4/5             | 1/5 → 1/5                |
| quoted        | 5/5 → 5/5             | 4/5 → 4/5             | 3/5 → 3/5                |
| colon-space   | 3/5 → 3/5             | 4/5 → 4/5             | 0/5 → 1/5                |
| leading-digit | 1/5 → **3/5**         | 0/5 → **2/5**         | 2/5 → 2/5                |

The fix moved exactly the mode it targeted: the leading-digit conflict
(`2 items - …` vs "keep every fact") is now handled by both editors. The
colon-space mode was already mostly passing for the editors; ornith's one
colon-space gain is within noise.

## Verdict for the shortlist

| Model               | Verdict                                                                  |
| ------------------- | ------------------------------------------------------------------------ |
| `qwen3.5:9b-q4_K_M` | **keep — leading candidate** (15/20, +2). One backtick token lost on an over-length case (0 → 1 token loss) — a minor fidelity regression to watch, not a disqualifier. |
| `gemma4:e4b-it-qat` | **keep** (14/20, +2), zero token loss, fastest editor.                    |
| `ornith-1.5:9b`     | **drop** — refusals 13 → 12 (unchanged within noise); the scoped "report only when the outcome is unclear" instruction did not collapse the refusal rate. Precise when it acts (27/30 fences, no token loss) but still reports 12/20 summaries across all four modes. |

## Deployment `num_ctx` guidance

Measured prompt sizes from the first run's JSONs: summary cases p50 ≈
3,974–4,192 tok (max ≈ 4,664), fence cases p50 ≈ 2,517–2,659 (max ≈ 2,798).
With `num_predict 1024`, an explicit `num_ctx ≈ 8192` suffices for this
workload — halving KV per slot versus the gate's `num_ctx 16384` at `q8_0`.
That is the lever for the cohabitation envelope math (IDEA-1071 measured
prefill, not decode, as the contention). The gate keeps 16384 for cross-run
comparability; right-sizing is a deployment-envelope decision for the real
agent, not a gate change.

## Caveats

- Majority-of-3 with conservative tie-breaking (ties resolve to the worse
  outcome) — a single-case difference is still noise; the token-loss and
  refusal patterns are not.
- Same synthetic corruption caveat as the first run: the live vault currently
  has zero failing summaries and zero bare fences, so the over-length filler
  is uniform and may be easier than real drift.
- Fence scoring treats `text` as the fallback class; two `text` fences were
  tables/YAML-ish prose that models called `yaml` — arguably defensible,
  scored as failures.
