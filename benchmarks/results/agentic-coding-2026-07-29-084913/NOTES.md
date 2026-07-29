# Agentic coding-tag matrix — 2026-07-29 run notes

## Final standings (primaries; solve rate is the ranking, style advisory)

| Tag | Solved | Bad calls | Tampers | Style (min/cause/contract) | Note |
| --- | --- | --- | --- | --- | --- |
| gemma4:coding-26b | 18/18 | 0/152 | 6 | 4.83 / 5.00 / 5.00 | ceiling, tampers every T3 run |
| gemma4:coding-12b | 18/18 | 0/106 | 4 | 4.56 / 4.56 / 4.83 | ceiling |
| qwen3.6:coding-gguf | 18/18 | 1/120 | 0 | 4.78 / 5.00 / 5.00 | ceiling, cleanest discipline |
| laguna:coding | 13/18 | 24/220 | 4 | 4.46 / 4.77 / 4.92 | all failures whitespace; 11 per-task-cap hits |
| nemotron3:coding | 8/18 | 22/68 | 0 | 4.75 / 5.00 / 5.00 | tool-name mangling; 5 server 500s (8 verifier passes, 7 error-free) |
| qwen3.6:coding (MLX) | 7/18 | 0/94 | 0 | 4.43 / 5.00 / 5.00 | BUG-1067 whitespace corruption |

`solved` means the final sandbox passed the verifier — a task can be solved
AND carry a transport/protocol error (nemotron t1a r0 is both). A future
harness pass should report `verified_solved` and `clean_solved` separately.

Sensitivity (`--no-think`, NOT part of the ranking): qwen-MLX 9/18 (7×
IndentationError — thinking is not required for the BUG-1067 defect),
qwen-GGUF 17/18 (one genuine logic miss, zero whitespace).

Judge: qwen3.6:subagent, 0 unparseable / 0 transport across all 8 files;
scores compressed at 4.4–5.0, so they order but do not measure (standing
TASK-1169 caveat; the judge is a sibling of the qwen tags under test).

Headline conclusions: (1) three-way ceiling at 18/18 → T1–T3 cannot rank the
top tier (a benchmark-scope limit, not a verifier defect — it still separates
weaker tags and tool discipline); T4 needed (see IMPR-1109 Harbor/Inspect
direction); (2) BUG-1067 — whitespace corruption strongly associated with the
MLX-origin serving/artifact path (the two qwen artifacts differ in digest,
reported architecture, parameter count, and quantization — 35.1B nvfp4 vs
36.0B Q4_K_M — so engine vs conversion/quantization remains unresolved);
thinking is not required for the defect; (3) the tool-discipline spread
(0% to 32% bad calls) is the discriminator the harness predicted;
(4) fixture-suite tampering is real, gemma-heavy, and fully neutralized by
the restore path.

Running log of findings for the 2026-07-29 matrix (first run on the hardened
post-PR-#61 harness). Harness: `num_predict=16384`, `num_ctx=32768`, seed 42+,
`REPEATS=3`, sandbox `network-write`. Log:
`benchmarks/results/agentic-matrix-run-20260729-084913.log`.

## Row findings

### gemma4:coding-12b — 18/18 (started 08:49, finished 09:11)

- **Perfect score, and it is clean**: `truncated_turns=0` on all 18 task runs
  (busiest task accumulated 12,092 output tokens across its turns — note
  `output_tokens` sums across API turns while `num_predict=16384` is
  per-request, so no headroom percentage can be read off these two numbers).
  The 2026-07-28 cap confound (3 failures at 4096, each with exactly one
  truncated turn) is confirmed resolved — those were cap artifacts, not
  model limits.
- **Benchmark ceiling concern**: 18/18 means the current tier set no longer
  discriminates at the 12B gemma tier. If the larger tags also ceiling, the
  matrix ranks nothing and a harder tier (T4) is needed.
- **Fixture-tamper habit**: rewrote `test_cache.py` (t3a) and `test_router.py`
  (t3b) on repeats — 4 tamper events total, all recorded in `tampered_suites`
  and restored before scoring; all 4 runs still solved on the merits of the
  target-file edits. The anti-cheat restore path works; the habit itself is a
  style/discipline signal for the judge, not a score issue.
- **Cold-load metric fix verified in the wild**: `load_s=2.71` recorded on the
  first task of the row (previously this field held the last warm turn).
- Zero bad tool calls anywhere in the row.

### gemma4:coding-26b — 18/18 (09:12–09:32)

- **Second ceiling score, also clean**: `truncated_turns=0`, 0 bad calls in 152
  tool calls, max 10,574 accumulated output tokens per task (per-turn cap
  never hit). `load_s=4.7` cold.
- **Tamper habit is total at this size**: rewrote the fixture suite on **all
  six** T3 runs (t3a ×3, t3b ×3) vs the 12b's 4-of-6. Recorded and restored;
  no score effect.
- Total row wall time 1,232 s of task work (~20.5 min including cooldowns).

### qwen3.6:coding (MLX) — 7/18, row marked FAILED(exit 4) (09:33–09:48)

- **First non-ceiling row, and the failure is systematic**: all 10 verifier
  failures share ONE signature — `IndentationError: unindent does not match
  any outer indentation level`. The model ships syntactically broken Python
  through `write_file`. Zero bad tool calls (94 calls), zero truncation — the
  tool channel works; the file contents are the problem.
- **Inverted difficulty curve**: T1 2/6, T2 0/6, T3 5/6. It aces the hardest
  tier and flunks the trivial one. Hypothesis: short single-file rewrites get
  whitespace-mangled (template or MTP path?), larger from-scratch T3 files
  survive. The GGUF row is the closest available control — same base weights,
  different serving artifact AND engine (the artifacts differ in digest,
  architecture string, parameter count, and quantization), so a clean GGUF
  row associates the failure with the MLX-origin path without resolving
  engine vs conversion/quantization.
- **The "transport" failure is actually a server-side parse 500**: Ollama
  returned `http 500: XML syntax error on line 3: element <function> closed
  by </parameter>` — the model emitted malformed tool-call XML and the
  server's template parser choked. One occurrence (r1 t3b, turn 2). Because
  any `error` row triggers exit 4, `row-status.tsv` records this row as
  `FAILED(exit 4)` even though 17/18 tasks ran to verdict — read the JSON,
  not just the manifest, for this row. (Classification nuance for a future
  harness pass: a 500 from the server's tool-parser is model-output-induced,
  not infrastructure.)
- No tamper events — the suite-rewriting habit stays gemma-specific so far.

### qwen3.6:coding-gguf — 18/18 (09:49–~09:57)

- **The control row landed: same base weights on llama.cpp, perfect score.**
  0 truncated turns, 1 bad call in 120, 0 tampers.
- Together with the MLX row this strongly associates the IndentationError
  collapse with the MLX-origin serving/artifact path — but the two artifacts
  are genuinely different (digest, reported architecture, 35.1B vs 36.0B
  parameter count, nvfp4 vs Q4_K_M), so engine vs conversion/quantization
  remains unresolved. Filed as **BUG-1067** in the vault, cross-linked to
  INFO-1127 (the sibling MLX fidelity defect: dropped `format` schemas).

### nemotron3:coding — 8/18, row marked FAILED(exit 4) (09:57–10:05)

- **Normal difficulty curve, abnormal tool discipline**: T1 6/6, T2 1/6,
  T3 1/6 — and a **32% bad-call rate** (22 of 68), all tool-NAME mangling:
  `write_File` ×5, `read_File` ×11, `read__file`, `write_ File`,
  `write_ file`, plus one hallucinated `execute_code`. This is the
  bad_tool_calls separation the harness docstring predicted.
- **5 server 500s** (4× the `<function>`/`</parameter>` XML error, 1×
  `unexpected EOF at line 59`). NOT each an instant task loss: t1a r0 is
  `solved: true` AND carries a 500 — the fix landed in earlier turns, the
  500 ended the loop, and the verifier still passed (`solved` judges only
  the final sandbox). So the row is 8 verifier passes, 7 of them error-free.
  Manifest row is `FAILED(exit 4)`. Combined with qwen-MLX's single hit:
  6 occurrences across 2 models. ollama#16383 documents qwen tool-template
  drift causing parser 500s; the nemotron root cause is not established by
  that issue.
- No truncation, no tampers.

### laguna:coding — 13/18 (10:06–10:43)

- Mid-field: T1 4/6, T2 4/6, T3 5/6 (flat curve, no tier inversion). No
  server 500s, 0 truncation.
- **All 5 failures are whitespace**: `IndentationError: unexpected indent` ×3,
  `unindent does not match` ×1, `TabError` ×1 — 60 odd-indent lines across
  failed sources (0 tab-led; the TabError's tab is embedded mid-indent).
  Second non-gemma MLX-path tag with whitespace-only failures, but the
  signature is NOISIER than qwen's surgical uniform +1 — could be
  model-emitted sloppiness rather than engine corruption, and laguna has no
  GGUF twin for a controlled split. Logged to BUG-1067 as corroborating,
  not confirming. Check laguna's tokenizer family (Mistral/Tekken?) — if
  byte-BPE space-prefix, the clean/dirty split lines up exactly with
  tokenizer family across all four MLX rows.
- **Tool discipline middling**: 24 bad calls / 220 (11%) — hallucinated
  `shell` tool ×16, `run_tests`/`list_files` with invented args, and two
  angle-bracket-contaminated names (`run_tests<`, `run_tests>`) that smell
  like template bleed.
- **Turn-cap pressure**: hit its per-task turn cap (caps are T1=8, T2=14,
  T3=20) on 11/18 tasks, most still passing — laguna grinds; its per-task
  wall times are the row's cost.
- **4 tamper events (t3b ×2, t3a ×1, +1)** — first non-gemma tamperer;
  "gemma-specific habit" is now "gemma-heavy, not gemma-exclusive".

### Sensitivity rows (qwen pair, --no-think)

- **qwen3.6:coding-gguf no-think: 17/18, zero IndentationErrors** — the one
  miss is a genuine logic failure (t3b r2, resolve returned None). The
  engine contrast holds in both reasoning modes: GGUF 35/36 solves with 0
  whitespace failures; MLX 16/36 with 17. Execution finished 10:53 with 8/8
  rows recorded; matrix status per `row-status.tsv` is incomplete/tainted
  (two rows exited 4).
- **qwen3.6:coding (MLX) no-think: 9/18 — the IndentationError persists**
  (7 of 9 failures; 0 transport) — thinking is NOT REQUIRED for the BUG-1067
  defect. It is not excluded as a contributor: whitespace failures moved
  10 → 7 and solves 7 → 9 with thinking off (direction consistent with a
  contribution, sample too small to claim one). The T1 collapse persists
  (T1 1/6, T2 4/6, T3 4/6), so the context-conditionality tracks the task,
  not the reasoning budget. Logged in the bug's investigation notes.
- **The t3a `max_size=0` verifier assertion caught its first real model**:
  two no-think failures (r1, r2) are `AssertionError: max_size=0 must retain
  nothing` — the exact truthiness-gate implementation the PR #61 review
  predicted (`if self.max_size:` treating 0 as unbounded). The review-added
  assertion is earning its keep on live traffic.

## Cross-run observations

- **Both gemma tiers ceiling at 18/18.** The current T1–T3 set does not
  discriminate within the gemma4 family at 16384 num_predict. If the qwen /
  nemotron / laguna rows also ceiling, the matrix produces no ranking and a
  harder T4 tier (multi-file feature with cross-cutting constraints) is the
  next harness investment. The discriminating signal that DOES survive at the
  ceiling: style-judge scores, tamper counts, turns, and wall time.
- **Suite tampering is gemma-heavy, not gemma-exclusive** (revised after
  laguna): gemma4 10 events in 12 T3 runs (12b 4/6, 26b 6/6 — intensifies
  with scale); laguna 4; qwen and nemotron 0. The anti-cheat restore path
  neutralizes it, but as a deployment signal it matters: these models edit
  tests they were told to keep passing.
- **The Ollama tool-parser 500 is cross-model, not a qwen quirk**: `http 500:
  XML syntax error on line 3: element <function> closed by </parameter>` hit
  qwen3.6-MLX once and nemotron3:coding 4+ times (mid-row count). When a model
  emits malformed XML tool syntax, the server 500s the whole request instead
  of surfacing a parseable bad-call. Harness follow-up (revised after
  review): do NOT fold these into ordinary bad calls — no structured call
  ever reaches the harness, so removing the row taint would overstate the
  result. Instead add a distinct `model_protocol_error` classification while
  RETAINING the error/tainted status, and report `verified_solved` vs
  `clean_solved` so a solved-with-500 task (nemotron t1a r0) is visible.
  Final counts: qwen-MLX 1, nemotron 5 (4× the function/parameter mismatch +
  1× unexpected EOF). Upstream: ollama#16383 documents the class for qwen
  template drift; the nemotron root cause is not established by it.
- **The MLX-origin path costs qwen3.6 the benchmark** (7/18 vs 18/18 for the
  GGUF counterpart; single IndentationError signature; logic in the failed
  files is correct, whitespace is not). Strongly associated with the
  serving/artifact path; engine vs conversion/quantization unresolved
  (different digests, architectures, parameter counts, quants). BUG-1067.
  Practical guidance until root-caused: agentic/code-writing workloads belong
  on GGUF tags; `-mlx` MTP speed is for prose. The `--no-think` rows showed
  thinking is not required for the defect (contribution not excluded).
