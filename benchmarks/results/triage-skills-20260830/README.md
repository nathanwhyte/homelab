# Triage-skill loops — blocker.review + staleness.triage (2026-08-30)

Three build-measure-adjust loops for the two new report-only proposer skills (`compendium-batch-blocker-review`, `compendium-batch-staleness-triage`), run against the production proposer `agentpair:agent-gemma4-12b` on timmy (agentpair posture — the agent half is resident, so no eviction cost per run). Harness: `~/code/compendium/_scripts/proposer-shadow.py` triage lanes (added this round, with `--repeats` majority voting). Registry entries measured and pinned in `~/code/compendium/.claude/skills/registry.json`.

## Corpora (`cases.json`)

- **blocker_cases (23)** — every live stale-blocker finding from `check-closability.py --json`: an open item mentioning a since-resolved item near blocker-ish words. Record = item id/status/title, target id/now-status, and the selector's context excerpt. Labels adjudicated by hand: 6 `lifted`, 12 `not-a-blocker`, 1 `still-blocking`, 4 `unclear`.
- **staleness_cases (12)** — items flagged by the age-only `check-staleness.py`: 5 open/stale bugs + 7 completed feats. Record = frontmatter facts + the last 1300 chars of body. Labels: 5 `follow-up-needed`, 7 `fine`.

Scoring: exact verdict match; `unclear` on a labeled case is a tracked safe-miss; any other mismatch is a wrong verdict (the failure that matters in a report a human trusts).

## Loops

| Loop | Blocker   | Staleness | What changed after                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| ---- | --------- | --------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1    | 16/23     | 11/12     | truncation cases mapped to `not-a-blocker` with the reason "target id not mentioned" — rule rewritten to force `unclear`; "sibling …blocker" wording pulled a `lifted` — blocker-of-something-else rule added; `still-blocking` over-applied to a sibling — precondition added. One label re-adjudicated (TASK-253: the excerpt explicitly says "unrelated to this fix"; the model applied the skill's own explicit-denial rule correctly).               |
| 2    | 22/23     | 11/12     | one truncation case still confidently `not-a-blocker` — the rule gained "if your reason says the target is not mentioned, the only consistent verdict is unclear". FEAT-029 investigated: the model was right both times — two literal `- [ ]` boxes sit in the tail that the labeling pass (reading a shorter excerpt) missed. **Label flipped, not the skill.** The staleness skill still gained the literal-checkbox rule from loop 1's phrasing risk. |
| 3    | **23/23** | **12/12** | measured run, `--repeats 3`, every case unanimous across votes. Nothing changed after.                                                                                                                                                                                                                                                                                                                                                                    |

## Reading

- **The loop structure worked as designed.** Every loop-1 failure was legible from the model's own `reason` string, and each fix was one reading-rule edit. The trajectory (16→22→23) is skill-text improvement, not noise: repeats-3 unanimity on loop 3 says the final behaviour is stable at temperature 0.
- **Two "model failures" were label failures.** TASK-253 (the model applied the explicit-denial rule the skill states) and FEAT-029 (the model found real unchecked boxes the labeler missed). On a corpus this small, the model auditing the labels is a feature — but it also means the final 23/23 + 12/12 includes 2 cases where the label moved toward the model. The other 33 labels never moved.
- **Contamination note:** the blocker skill quotes TASK-1193 (one corpus case) as its worked input example, per house style. The staleness skill's example uses BUG-138's frontmatter but rules were genericized away from corpus phrasing after drafting.
- **Known limits:** one model, one run night, 35 cases total; the `unclear` path of staleness.triage was never exercised (no case needed it); the blocker lane's `unclear` class exists mostly because `check-closability`'s excerpts truncate — widening the selector's context window would shrink it at the source and is the highest-leverage next change.

Files: `cases.json` (records + labels + adjudication notes), `{blocker,staleness}-loop{1,2,3}.json` (per-case rows incl. votes and raw replies). Run dirs: `~/code/compendium-runs/shadow-agentpair_agent-gemma4-12b-20260831T{034639,034820,034944}Z`.
