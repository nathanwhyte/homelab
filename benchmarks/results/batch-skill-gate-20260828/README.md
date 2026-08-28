# Batch-skill gate — partner models for the compendium batch agent (2026-08-28)

First test of the IDEA-1090 partner shortlist on the **real** batch-agent skills rather than synthetic prompts. Tool: `benchmarks/ollama/tools/batch-skill-gate.py`; cases: `cases.json` (seeded, identical for every model); per-model detail: `<model>.json`; run logs: `batch1.log`, `batch2.log`.

Server: timmy RX 9070 XT, Ollama **0.33.1**, Vulkan, production env (`NUM_PARALLEL=2`, `MAX_LOADED_MODELS=1`, `KV_CACHE_TYPE=q8_0`, `CONTEXT_LENGTH=131072`); each candidate loaded alone (evicting the FIM runner), `num_ctx 16384`, `think:false`, temperature 0, `/api/chat` with a JSON-schema `format`. Models ran sequentially (one local-model consumer); FIM re-warmed load-only at the end.

## What was tested

| Case set                                 | Source                                                                                                                                                                                                                                                                                               | Count           | Oracle                                                                                                                                                                                                                                                                         |
| ---------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **summary** — `retrieval_summary` repair | real records from `build-lane-records.py` over bugs/improvements/ideas/info, each compliant summary corrupted one way: over-length (filler appended past 200 chars), double-quoted, colon-space (first space-dash-space separator rewritten as colon-then-space), leading digit (`2 items -` prefix) | 20 (5 per mode) | IMPR-1105 schema (≤200, one line, no `"`, no colon-space, no space-hash, no leading digit); every ID / PR / date / version / backtick token of the _current_ summary preserved; `_scripts/fact-token-verify.py` finds no hallucinated token; `null` = "report instead of edit" |
| **fence** — bare-fence (MD040) tag       | real tagged fences from the vault with the tag stripped, 5 per class                                                                                                                                                                                                                                 | 30              | exact class per the skill's table (bash / yaml / json / python / sql / text; `sh`→bash, `md`→text, `jsonc`→json)                                                                                                                                                               |

System prompt = the verbatim `compendium-batch` contract + the batch-type `SKILL.md` from `~/code/compendium` branch `idea-batch-agent` (64efad3e). The model sees the same text the agent will.

## Results

| Model                         | Summary pass | reported | token loss | schema fail | halluc. | bad JSON | Fence     | wall p50 | decode    | load   |
| ----------------------------- | ------------ | -------- | ---------- | ----------- | ------- | -------- | --------- | -------- | --------- | ------ |
| `nemotron-3-nano:4b-bf16`     | **14/20**    | 0        | **4**      | 3           | 0       | 0        | 18/30     | 1.6 s    | 74 tok/s  | 8.0 s  |
| `gemma4:12b-it-qat` (ceiling) | 13/20        | 0        | 1          | 6           | 0       | 0        | **27/30** | 2.6 s    | 60 tok/s  | 11.6 s |
| `qwen3.5:9b-q4_K_M`           | 13/20        | 1        | 0          | 6           | 0       | 0        | 26/30     | 1.9 s    | 82 tok/s  | 9.8 s  |
| `gemma4:e4b-it-qat`           | 12/20        | 0        | 0          | 8           | 0       | 0        | 26/30     | 1.2 s    | 104 tok/s | 16.4 s |
| `ornith-1.5:9b`               | 6/20         | **13**   | 0          | 1           | 0       | 0        | **27/30** | 1.6 s    | 85 tok/s  | 8.9 s  |
| `lfm2.5` (8B-A1B)             | 1/20         | **19**   | 0          | 0           | 0       | 0        | 9/30      | 0.5 s    | 265 tok/s | 7.0 s  |

Failure breakdown (summary set):

| Model              | over-length     | quoted | colon-space   | leading-digit | notes                                                                                                                                                                                               |
| ------------------ | --------------- | ------ | ------------- | ------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| nemotron-3-nano:4b | 3/5             | 3/5    | 4/5           | **4/5**       | the only model that handles the leading digit, but it **drops facts** (an ID, a PR number, a date, a backtick token across 4 cases) — the failure the skill explicitly ranks worse than not editing |
| gemma4:12b-it-qat  | 3/5             | 5/5    | 4/5           | 1/5           | keeps `2 items - …` verbatim; one dropped backtick                                                                                                                                                  |
| qwen3.5:9b         | 4/5 (+1 report) | 5/5    | 3/5           | 1/5           | zero token loss; the one `report` was a correct refusal                                                                                                                                             |
| gemma4:e4b-it-qat  | 4/5             | 4/5    | 4/5           | 0/5           | zero token loss; fastest of the editors                                                                                                                                                             |
| ornith-1.5:9b      | 1/5             | 3/5    | 0/5 (+1 fail) | 2/5           | refuses to edit 13/20 — reports instead; never wrong, rarely useful                                                                                                                                 |
| lfm2.5             | 0/5             | 1/5    | 0/5           | 0/5           | refuses 19/20; fence 9/30 (calls almost everything `bash`)                                                                                                                                          |

## Reading

- **Nobody hallucinated and nobody broke the JSON.** `fact-token-verify.py` was clean for all six models, and schema-constrained `format` output parsed 120/120 times on GGUF/Vulkan (INFO-1127's `format` drop is MLX-only, as expected).
- **The dominant failure is a conflict in the skill text, not in the models.** The leading-digit case starts `2 items - …`; the edit rule says both "keep every fact" and "does not start with a digit", and four of six models chose fidelity. `compendium-batch-summary-repair` should say what to do with a number that is a fact (reorder the clause or spell it out — never drop it). Same for colon-space: `config: .claude/…` is a real colon-then-space inside a fact; the rule needs an example of the sanctioned rewrite (`config - .claude/…` or `config .claude/…`). Both are one-line skill edits for IDEA-1091. **They did not move any editor's score** — see `../batch-skill-gate-20260828-postfix2/`: with the corrected count-preserving oracle, every model is 0/5 on leading-digit after the fix, keeping the digit or dropping the count rather than reordering it. The conflict is a capability gap; the gate or the schema has to change, not the prompt.
- **Fidelity beats compliance for this job.** By the skill's own ranking (a dropped token is worse than a report, which is worse than a bad-format edit that lint will catch), the order is `qwen3.5:9b` ≈ `gemma4:e4b` (13 and 12 passes, zero token loss) > `gemma4:12b` (1 loss) > `nemotron` (4 losses). Nemotron's headline 14/20 is the wrong number to optimise.
- **Refusers.** `ornith-1.5:9b` is precise when it acts (27/30 fences, no token loss) but reports 13/20 summaries — it reads "if you are unsure … report the entry" as the default. Worth one retry with a firmer instruction before dropping it. `lfm2.5` is out: 19/20 refusals and a 9/30 fence score at any speed.
- **Cost.** All editors finish a summary repair in 1.2–2.6 s wall (prompt ≈ 3–5k tokens incl. the two SKILL.md texts); decode 60–104 tok/s. A 20-entry batch is well under a minute of GPU per model — the agent's cost is prefill, which is what the IDEA-1090 contention probe already measured (D: one such request delays an inline FIM completion by up to ~3 s if it lands mid-prefill).

## Verdict for the shortlist

| Model                     | Verdict                                                            | Next                                                                                                                                |
| ------------------------- | ------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------- |
| `qwen3.5:9b-q4_K_M`       | **keep — leading candidate**                                       | rerun done (12/20, unchanged — the +2 was an oracle false positive); slot probe (`n_seq_max` on 0.33.1) before cohabitation         |
| `gemma4:e4b-it-qat`       | **keep** — smallest editor with zero token loss, fastest           | rerun done (12/20, unchanged — the +2 was an oracle false positive); IDEA-1084 parser re-test still applies if tool calling is used |
| `gemma4:12b-it-qat`       | reference ceiling — fits only as the option-D (FIM unloaded) model |                                                                                                                                     |
| `nemotron-3-nano:4b-bf16` | park — drops facts; fence 18/30                                    | keep for the 64K-context NemoHermes case only                                                                                       |
| `ornith-1.5:9b`           | drop — refusals unchanged (12/20) after the scoped instruction     |                                                                                                                                     |
| `lfm2.5`                  | drop                                                               |                                                                                                                                     |

Post-fix rerun (pinned cases, 3 repeats): ../batch-skill-gate-20260828-postfix/ —
**superseded**, its leading-digit scores are inflated by an oracle false positive.
Corrected rerun: ../batch-skill-gate-20260828-postfix2/

## Caveats

- 20 + 30 cases at temperature 0, one run each: differences of one or two cases are noise; the token-loss and refusal patterns are not.
- The corruptions are synthetic (the live vault currently has zero failing summaries and zero bare fences — the checks pass), so the over-length filler is uniform and may be easier than real drift. The `report`-vs-edit behaviour and fence classification use real content.
- Fence scoring treats `text` as the fallback class; two `text` fences were tables/YAML-ish prose that three models called `yaml` — arguably defensible, scored as failures.
