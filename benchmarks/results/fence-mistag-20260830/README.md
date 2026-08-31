# fence.propose v2 gate — mistag adjudication (2026-08-30)

First measurement of the rewritten `compendium-batch-fence-repair` skill (contract v2 — mistag adjudication; the v1 bare-fence numbers in `../batch-skill-gate-20260828-postfix2/` do not carry over). Harness: `~/code/compendium/_scripts/proposer-shadow.py` (`--only fence`), fence lane rewritten to build the record exactly as `batch-run.py`'s `fence_propose` does. Registry entry updated and re-pinned in `~/code/compendium/.claude/skills/registry.json`.

Server: timmy RX 9070 XT, Ollama 0.33.1, Vulkan, production env; paired tags with baked `num_ctx 16384`, no per-request `num_ctx`, `think:false`, temperature 0, `/api/chat` with the FENCE JSON schema. One local-model consumer: models sequential; `deepseek-coder-v2:fim` evicted for the window and re-warmed load-only (`keep_alive -1`) afterwards.

## Cases (`cases.json`, 15)

- **11 live candidate-tier findings** — everything `fence-audit.py --json` reports today (provable tier is empty). Adjudicated by hand: 3 real retags (`expect: retag`), 7 keeps (`expect: keep`), 1 genuinely ambiguous (`expect: report` — IMPR-1021:212, `Bucket:`/`Path:` keys against sentence bullets).
- **4 reviewed false positives** as negatives (`expect: keep`) — the post-merge classifier-chase cases (three `#`-comment `text` blocks the old PROMPT_RE read as bash; GUIDE-1003:144, a real 95-line bash script the arrow heuristic wanted as `text`), replayed with the old suspicion attached.

Scoring: `retag` passes only on a propose with the adjudicated tag; `keep` passes on propose-current or report (both are non-writes in production); `report` passes only on report. Contamination note: BUG-1031:223, IDEA-1035:81, and TASK-1143:188 are quoted in the skill text as worked examples; all three came back correct, and the informative score is the other twelve.

## Results

| Tag                          | pass      | retags (3) | keeps (11)            | ambiguous (1)   | wrong writes |
| ---------------------------- | --------- | ---------- | --------------------- | --------------- | ------------ |
| `agentpair:agent-gemma4-12b` | **14/15** | 3/3        | 1 kept, 10 reported   | retagged `yaml` | 1            |
| `agentpair:agent-gemma4-e4b` | 13/15     | 3/3        | 10 kept, 1 id-mangled | retagged `yaml` | 1            |
| `agentpair:agent` (qwen3.5)  | 12/15     | 1/3        | 1 kept, 10 reported   | kept `text`     | 0            |

Wall p50 ≈ 1.0 s (e4b) / 1.8 s (12b) / 1.9 s (qwen) per call.

## Reading

- The selector/model division of labour holds: the v1 hard class (language fragments, F04/F20) never reaches the model any more, and the reviewed false positives — which the old classifier got wrong — were all read correctly by both gemma tags.
- `agentpair:agent-gemma4-12b` (run 2026-08-30, same window pattern) has the best posture of the three: it acts on all 3 real retags and reports on everything else — it discriminates, where e4b commits everywhere and qwen reports everywhere. No id mangle. Its only miss is the same ambiguous case (`yaml` instead of `report`), which no model has passed. Caveats: the 12b tag exists on timmy but is not yet in the homelab recipes/configmap (built for the pair-VRAM probe), and its keep-path behaviour is report-heavy — fine for the metric (both are non-writes) but it hands more items to the human report than e4b would.
- `agentpair:agent-gemma4-e4b` was the pass-1 pick on score alone (superseded by pass 2 below). Its miss is the one genuinely ambiguous case, where it commits (`yaml`) instead of reporting; the draft-PR gate (which caught the eight 3a wrong swaps) is the backstop for exactly this.
- qwen fails safe but inert: 12/15 reports, 1/3 retags found — consistent with its v1 refuser pattern.
- gemma mis-echoed one long path-based id by a single character (BUG-1042, `payloads` → `payload`); production declines a wrong id echo, so the failure is safe but wastes the case. If it recurs at scale, shorter ids are the fix.

## Pass 2 — skill reworded for the mapping/sentence split (same night)

One rule added to `fence.propose` ("Half a mapping, half sentences is neither" — key-value lines above imperative bullets go to `report`, with an invented example so IMPR-1021:212 stays informative), same 15 cases, `*-pass2.json`:

| Tag                          | pass      | Δ pass-1 | retags (3) | ambiguous (1) | wrong writes    |
| ---------------------------- | --------- | -------- | ---------- | ------------- | --------------- |
| `agentpair:agent-gemma4-12b` | **15/15** | +1       | 3/3        | reported      | 0               |
| `agentpair:agent` (qwen3.5)  | 13/15     | +1       | 1/3        | reported      | 0               |
| `agentpair:agent-gemma4-e4b` | 12/15     | −1       | 3/3        | still `yaml`  | 2 (IDEA-1035 ↯) |

The rewording splits the field by posture. gemma4-12b, which already discriminated, now clears the set: every retag found, everything else reported, the ambiguous case reported with the new rule's own reasoning. qwen also gains the ambiguous case (and one retag came back as an empty reply — serving noise, not a wrong write). e4b, the commit-everywhere model, got worse: it still retags the ambiguous case and now also retags IDEA-1035's bullet list `yaml` — the new "keys don't decide" rule apparently primed it to see mappings. **The skill text is now tuned for a discriminating proposer; the pick is `agentpair:agent-gemma4-12b`**, with the caveat that it is not yet in the homelab recipes/configmap.

Files: `cases.json` (seeded case set), per-model `*.json` (pass 1) and `*-pass2.json` (per-case rows incl. raw replies). Run dirs with `meta.json`/`report.md` under `~/code/compendium-runs/shadow-agentpair_*` (pass 1: `T023522Z`/`T023555Z`/`T024131Z`; pass 2: `T025628Z`/`T025716Z`/`T025748Z`).
