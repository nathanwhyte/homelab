# Compendium ↔ OpenViking Pattern #1 — Test Plan

## Status & scope

Test plan for validating the **pointer index** workflow defined in
[`compendium-ov-spec.md`](./compendium-ov-spec.md). Exercises sync,
search, and ranking against a controlled corpus of faux entries.
Pattern #2 (full-content) is out of scope and explicitly deferred.

## Setup state (already complete)

| Item | Status |
|---|---|
| Worktree at `~/code/compendium/.worktrees/ov-test` | Created |
| Branch `ov-test` checked out in worktree | Done |
| `.worktrees/` added to `.gitignore` and committed on `main` | Done |
| Entry directories wiped (`bugs/`, `ideas/`, `features/`, `tasks/`, `projects/`, `plans/`, `info/`) | Done — 161 files removed |
| Structural files preserved (CLAUDE.md, README.md, dashboard.md, DATAVIEW.md, `_templates/`, `_briefs/`, `_inbox/`, `_scripts/`, `_sources/`, `_verification-reports/`, `docs/`) | Kept |
| Wipe committed on `ov-test` branch | Commit `d128c15` |

Stale state to be reset by the subagent population step:

- `index.md` and `log.md` reference deleted entries (rebuilt in Phase 1)

## Phase 1 — Populate faux entries

**Driver:** the prompt at
[`compendium-ov-test-prompt.md`](./compendium-ov-test-prompt.md),
dispatched as a `general-purpose` subagent.

**Expected output:** 60 entries across 7 directories, with a structured
report listing entry counts, `ov_mode` distribution (missing / pointer
/ none), and a list of intentional ranking traps.

**Pass criteria:**

- [ ] 60 ± 5 entries created
- [ ] All 6 entry types represented per the distribution table in the prompt
- [ ] At least one of each `ov_mode` state (missing, pointer, none)
- [ ] All 6 required ranking traps present and reported
- [ ] `index.md` and `log.md` rebuilt
- [ ] Worktree committed cleanly on `ov-test`
- [ ] Working tree status clean after population

**Capture from the report:**

The trap list returned by the subagent becomes the primary input to
Phase 3 — record it verbatim in a follow-up note (e.g.,
`compendium-ov-test-traps.md`) so test queries can target them
deterministically.

## Phase 2 — Sync to OpenViking

**Prerequisite:** the `compendium-sync` skill (deferred — separate
implementation plan). For Phase 2 to run, the skill must:

- Parse YAML frontmatter; default `ov_mode = pointer` when missing
- Build payload per the format in `compendium-ov-spec.md` §"Indexed
  payload format"
- Upsert via `viking_add_text` with **isolated target_dir**:
  `viking://resources/compendium-test/<vault-subdir>/`
- Skip entries with `ov_mode: none`
- Use `name` derived from entry ID (e.g., `idea-001`, `bug-007`,
  `plan-2026-04-22-some-slug`)

**Test runs (in order):**

1. **`sync-all` cold start** — empty target dir → 57 entries indexed
   (60 minus the 3 `ov_mode: none`). Verify count via
   `viking_find` with broad scope.
2. **Idempotency** — run `sync-all` again with no markdown changes.
   No diffs in OV state; no errors.
3. **Single-entry edit** — modify the TL;DR of one entry, run
   `sync-one <path>`. Verify the updated payload reflects the new
   first paragraph.
4. **Mode flip to `none`** — change one synced entry from missing-mode
   to `ov_mode: none`. Run `sync-one`. Verify the entry is removed
   from OV (or flagged for manual cleanup if v1 doesn't auto-delete).
5. **`sync-changed` via pre-commit** — make edits to 3 entries, run
   `git commit`. Verify the hook syncs only the staged files.

**Pass criteria:**

- [ ] Test 1 indexes exactly 57 entries (60 − 3 excluded)
- [ ] Test 2 produces no observable changes (idempotent)
- [ ] Test 3 reflects the edit on next search
- [ ] Test 4 either removes or flags for cleanup per spec §"Deletion"
- [ ] Test 5 syncs only changed files (not all 60)

## Phase 3 — Search workflow

This is the agent-side test: given a "find our work related to X"
prompt, does the workflow return the right markdown content?

**Workflow under test (from `compendium-ov-spec.md` §"Search workflow"):**

1. `viking_find(query, scope="compendium-test")`
2. Parse `[ov_mode: pointer]` header in each hit
3. Extract `Path:` and Read the markdown
4. Synthesize answer from markdown content

### Test queries

Queries are grouped by failure mode they probe. Each is run by the
human tester (or a sandbox session) with the workflow above.

**Direct-match probes** (sanity — must work):

| Query | Expected behavior |
|---|---|
| "show me work on session expiry" | Top hit is the auth/session entry; agent reads its markdown |
| "find the duplicate row creation bug" | Surfaces the BUG entry on duplicate creation (not the IDEA/TASK on similar topic) |
| "what's the deployment runbook" | Surfaces the relevant INFO or RUNBOOK entry |

**Synonym probes** (semantic > keyword):

| Query | Expected behavior |
|---|---|
| "credential rolling strategy" | Should surface the entry actually titled "auth token rotation" (synonym pair from prompt) |
| "ETL race condition" | Should find pipeline race-condition entries even if they don't say "ETL" |

**Disambiguation probes** (near-duplicates):

| Query | Expected behavior |
|---|---|
| Run trap pair query A → returns both near-duplicates → agent reads both and notes the difference | Verifies that pointer payload's headings + TL;DR carry enough signal to differentiate |
| Run trap pair query B (keyword trap: "deletion") → returns the destructive-op AND cleanup entries | Verifies semantic ranking handles polysemy |

**Cross-type probes:**

| Query | Expected behavior |
|---|---|
| Pick the topic that appears across IDEA + BUG + TASK + PROJECT | All four surface; agent presents the cross-type view |

**Exclusion probes:**

| Query | Expected behavior |
|---|---|
| Topic where entries with `ov_mode: none` should be hidden | None of the excluded entries appear in results |

**Negative-space probes:**

| Query | Expected behavior |
|---|---|
| Query a thematic cluster missing from the corpus | Returns weak / zero hits — agent reports "nothing strongly related" rather than fabricating |

### Pass criteria

- [ ] All direct-match probes hit the intended entry in top 3 results
- [ ] At least 1 synonym probe succeeds (acceptable: 1 of 2 if vocabulary is far apart)
- [ ] Disambiguation probes return both near-duplicates; agent's synthesis distinguishes them
- [ ] Cross-type probe surfaces ≥3 of the 4 entry types
- [ ] Exclusion probe returns zero `ov_mode: none` entries
- [ ] Negative-space probe does NOT fabricate — agent correctly reports absence
- [ ] Average chunks-read-then-Read cost: ≤2 Read calls per query (otherwise the pointer payload is too lean)

## Phase 4 — Token cost measurement

Capture for each query in Phase 3:

| Metric | How to measure |
|---|---|
| OV chunk tokens returned | Length of `viking_find` response body |
| Markdown file tokens read | Size of the Read call output |
| Total tokens consumed for the answer | Sum of above |

**Comparison baseline:** for the same query, simulate the
"no-OV" path — `grep` for likely keywords across the worktree,
Read the matching files. Compare:

- Tokens consumed
- Whether the right entry was found
- Time to first useful result

**Pass criteria:**

- [ ] Average OV+Read cost is ≤ baseline grep+Read cost on synonym
      probes (where grep is expected to lose)
- [ ] Average OV+Read cost is ≤ 1.5× baseline on direct-match probes
      (acceptable overhead for fuzzy capability)

## Phase 5 — Cleanup

When the prototype is validated (or abandoned):

```bash
# Remove the worktree
cd ~/code/compendium
git worktree remove .worktrees/ov-test --force

# Delete the branch
git branch -D ov-test

# Wipe the test target_dir from OV
# (manual call — exact command depends on OV CLI, e.g.:)
ov delete --recursive viking://resources/compendium-test/
```

The `.worktrees/` entry in `.gitignore` stays (harmless and useful for
future prototypes).

## Out of scope for this test

- Pattern #2 full-content sync (deferred to a separate test plan)
- Obsidian save-hook automation (manual sync only for v1)
- Two-way sync (OV findings → compendium entries)
- Wikilink prefetch behavior (separate UX concern)
- Performance under realistic vault size (~500+ entries) —
  current test corpus is 60 entries

## Open questions to revisit after the test runs

1. Is the pointer payload (TL;DR + headings) rich enough for ranking,
   or do we need section first-sentences too?
2. How often does the agent need to Read the full markdown vs answering
   from the OV chunk alone? If <30%, the payload is over-rich; if
   >80%, OV is doing very little work.
3. Does the `[ov_mode: pointer]` header convention parse cleanly in
   practice, or does it need a more structured format (JSON frontmatter)?
4. Are 6 entry types too many for stable ranking, or does the
   `target_dir` mirroring already partition cleanly?

These questions are NOT pass/fail — they're inputs to the v2 spec.
