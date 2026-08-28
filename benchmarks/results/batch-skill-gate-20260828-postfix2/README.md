# Batch-skill gate — corrected rerun (2026-08-28)

Corrected rerun of the IDEA-1090 partner gate after two defects in the
`batch-skill-gate.py` oracle were fixed (homelab `94c5a8a`):

1. **Leading-digit semantic false positive** — the oracle's `TOKEN_RES` only
   checked id/pr/date/version/backtick tokens, so a model that *deleted* the
   `2 items -` (with a trailing space) prefix (dropping the count, a fact) still passed. The oracle now
   requires the count to survive as `2`/`two` + `items` (`count_preserved()`).
2. **Fence cases did not repeat** — `--repeats 3` only repeated the summary
   set; the fence loop ran once. It now repeats with a majority verdict.

Same pinned cases as the first run and the post-fix run (`cases.json`,
byte-identical), three repeats each, majority per case. Tool:
`benchmarks/ollama/tools/batch-skill-gate.py`; per-model detail: `<model>.json`;
run log: `postfix2.log`.

Server: timmy RX 9070 XT, Ollama **0.33.1**, Vulkan, production env
(`NUM_PARALLEL=2`, `MAX_LOADED_MODELS=1`, `KV_CACHE_TYPE=q8_0`,
`CONTEXT_LENGTH=131072`); each candidate loaded alone (evicting the FIM runner),
`num_ctx 16384`, `think:false`, temperature 0, `/api/chat` with a JSON-schema
`format`. Models ran sequentially (one local-model consumer); FIM re-warmed
load-only at the end.

## Results

| Model               | Summary pass | reported | token loss | schema fail | halluc. | bad JSON | Fence     | wall p50 | decode    | load   |
| ------------------- | ------------ | -------- | ---------- | ----------- | ------- | -------- | --------- | -------- | --------- | ------ |
| `qwen3.5:9b-q4_K_M` | **12/20**    | 0        | 1          | 7           | 0       | 0        | 26/30     | 2.0 s    | 82 tok/s  | 11.2 s |
| `gemma4:e4b-it-qat` | **12/20**    | 0        | 0          | 8           | 0       | 0        | 26/30     | 1.3 s    | 104 tok/s | 9.6 s  |
| `ornith-1.5:9b`     | 5/20         | **12**   | 0          | 3           | 0       | 0        | **27/30** | 1.6 s    | 85 tok/s  | 8.7 s  |

## Pre/post per-mode (summary set, pass counts)

| Mode          | qwen3.5:9b pre → post | gemma4:e4b pre → post | ornith-1.5:9b pre → post |
| ------------- | --------------------- | --------------------- | ------------------------ |
| over-length   | 4/5 → 4/5             | 4/5 → 4/5             | 1/5 → 1/5                |
| quoted        | 5/5 → 5/5             | 4/5 → 4/5             | 3/5 → 3/5                |
| colon-space   | 3/5 → 3/5             | 4/5 → 4/5             | 0/5 → 1/5                |
| leading-digit | 1/5 → **0/5**         | 0/5 → **0/5**         | 2/5 → **0/5**            |

The post-fix run's headline "improvement" (qwen3.5 13→15, gemma4:e4b 12→14) was
an oracle artifact. The leading-digit oracle did not check count preservation,
so models that deleted `2 items -` (with a trailing space) (dropping the count) passed. With the
count-preservation check, **no model passes a single leading-digit case**:
they either keep the digit verbatim (failing the "no leading digit" rule) or
drop the count (failing the new fact-preservation rule). The skill-text fix
("reorder or spell out the number, never drop it") did not teach any model to
do that.

The only real movement is ornith's colon-space gain (0/5 → 1/5), which is
within noise. Net count-preserving scores: qwen3.5 12→12, gemma4:e4b 12→12,
ornith 4→5.

## Verdict for the shortlist

| Model               | Verdict                                                                  |
| ------------------- | ------------------------------------------------------------------------ |
| `qwen3.5:9b-q4_K_M` | **keep — leading candidate** (12/20, unchanged). One backtick token lost on an over-length case (0 → 1 token loss) — a minor fidelity regression to watch, not a disqualifier. |
| `gemma4:e4b-it-qat` | **keep** (12/20, unchanged), zero token loss, fastest editor.             |
| `ornith-1.5:9b`     | **drop** — refusals 13 → 12 (unchanged within noise); the scoped "report only when the outcome is unclear" instruction did not collapse the refusal rate. Precise when it acts (27/30 fences, no token loss) but still reports 12/20 summaries across all four modes. |

The skill-text fix is **not** the lever for the leading-digit conflict. The
models cannot reorder or spell out a leading count even when told to; the
conflict is a capability gap, not an instruction gap. The leading-digit
corruption should be dropped from the gate (or the skill should accept a
leading digit as a valid repair) rather than re-litigated with more prompt
edits.

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
- Fence scores are unchanged from the single-shot run (26/30, 26/30, 27/30):
  temperature 0 makes the fence cases deterministic, so the repeat fix is a
  correctness fix (the README now accurately claims "three repeats"), not a
  score change.
- Same synthetic corruption caveat as the first run: the live vault currently
  has zero failing summaries and zero bare fences, so the over-length filler
  is uniform and may be easier than real drift.
- Fence scoring treats `text` as the fallback class; two `text` fences were
  tables/YAML-ish prose that models called `yaml` — arguably defensible,
  scored as failures.
