# Subagent prompt — populate faux entries for Compendium ↔ OV Pattern #1 test

## How to use

Dispatch a `general-purpose` subagent with this file's contents as the
prompt. The subagent operates inside the test worktree at
`~/code/compendium/.worktrees/ov-test/` on branch `ov-test`. It does
NOT touch the main compendium vault.

When the agent completes, it should report:

- Counts of entries created per type
- A list of intentional near-duplicate / keyword-trap pairs (so the
  test plan can probe ranking behavior on them)
- Any deviations from the brief

---

## Brief for the subagent

You are populating a test instance of an Obsidian vault that mirrors a
real personal knowledge base (compendium). The goal is to test whether
OpenViking semantic search can satisfy "find our work related to X"
queries against pointer-style indexed compendium entries.

**Working directory:** `~/code/compendium/.worktrees/ov-test/`
(verify with `pwd` first — do NOT operate on
`~/code/compendium/` directly).

**Branch:** `ov-test` (already created and checked out in this
worktree). All commits land here, never on `main`.

### Step 1 — Read the templates

Before generating anything, read every file in `_templates/`:

- `idea.md`, `bug.md`, `feature.md`, `task.md`, `project.md`,
  `info.md`, `stub.md`

Each defines the YAML frontmatter fields, expected sections, and tone
for that entry type. Faux entries must follow these templates faithfully
(with one addition described in Step 3).

### Step 2 — Distribution

Generate **60 faux entries** with this distribution:

| Type | Count | Subdirectory layout |
|---|---|---|
| ideas | 15 | 12 in `ideas/`, 3 in `ideas/completed/` |
| bugs | 15 | 6 in `bugs/active/`, 9 in `bugs/resolved/` |
| tasks | 12 | 9 in `tasks/`, 3 in `tasks/completed/` |
| features | 8 | 6 in `features/`, 2 in `features/completed/` |
| projects | 5 | flat in `projects/` |
| info | 3 | flat in `info/` |
| plans | 2 | flat in `plans/`, date-prefixed (`YYYY-MM-DD-...`) |

Use sequential numbering per type starting at 001 (e.g.,
`IDEA-001`, `BUG-001`). Filenames: `{TYPE}-{NNN}-{kebab-slug}.md`.
For `plans/`, use the existing date convention without IDs:
`YYYY-MM-DD-<slug>.md`.

### Step 3 — `ov_mode` distribution (test fixture)

To exercise the Pattern #1 sync resolver across all states, distribute
the `ov_mode` frontmatter field as follows:

| State | Approx % | Behavior under sync |
|---|---|---|
| Field missing entirely | ~80% (~48 entries) | Defaults to `pointer` — synced to OV |
| `ov_mode: pointer` (explicit) | ~15% (~9 entries) | Synced to OV |
| `ov_mode: none` | ~5% (~3 entries) | Skipped by sync |

Place the `ov_mode: none` entries in obvious draft / scratch contexts
(e.g., one in `ideas/`, one in `tasks/`, one in `bugs/active/`) so
their exclusion from search results during the test is intuitive.

### Step 4 — Subject-matter coverage and ranking traps

The corpus must support queries like "find our work related to X."
Design entries so the test plan can probe these failure modes:

**Eight thematic clusters** (distribute roughly evenly across types):

1. Database / query performance (slow queries, indexing, N+1, caching)
2. Authentication & authorization (token rotation, session expiry,
   role-based access, RBAC migrations)
3. Data pipelines / ETL (deduplication, schema drift, race conditions,
   batch vs streaming)
4. Frontend / UI (state management, loading skeletons, accessibility,
   form validation)
5. Observability (logging gaps, metric cardinality, alerting fatigue,
   distributed tracing)
6. Testing & CI/CD (flaky tests, snapshot churn, deploy gating,
   pre-commit hooks)
7. Infrastructure / deployment (container OOM, network policies,
   secret rotation, blue/green)
8. Documentation / runbooks (onboarding, incident response,
   architecture diagrams)

**Required ranking traps — produce all of these explicitly:**

| Trap type | Example | Purpose |
|---|---|---|
| **Near-duplicate ideas** | Two IDEA entries proposing similar caching strategies, ~70% topic overlap, different framing | Tests whether OV can disambiguate or returns both |
| **Near-duplicate bugs** | Two BUG entries on similar symptom but different root causes (e.g., both "duplicate row creation" — one race condition, one schema bug) | Tests whether the body text differentiates |
| **Keyword trap** | One entry uses "deletion" for a destructive operation, another uses "deletion" for a cleanup task — same word, different semantic intent | Tests embedding quality vs naive keyword match |
| **Synonym pair** | One entry talks about "auth token rotation," another about "credential rolling" — different vocabulary, same concept | Tests whether semantic search bridges synonyms |
| **Cross-type spread** | One topic (e.g., snowflake schema cleanup) gets representation across IDEA, BUG, TASK, and PROJECT | Tests whether queries surface the most-relevant type |
| **Negative space** | At least 2 thematic clusters above should NOT have a feature/runbook entry | Tests whether queries about those topics return appropriate "weak" hits rather than false confidence |

After populating, **list every trap pair** in the report so the test
plan can target them.

### Step 5 — Realistic prose, not Lorem Ipsum

Embedding quality depends on real semantic content. Each entry needs:

- **First paragraph (TL;DR)**: 2–4 sentences, plain English,
  describes the entry's *aboutness*. This is the primary ranking
  surface for Pattern #1.
- **Section bodies**: short but realistic — 3–6 sentences per
  template-mandated section. Reference plausible technologies, error
  messages, system names. Faux is fine; gibberish is not.
- **Tags**: 3–5 tags per entry drawn from the cluster vocabulary
  (e.g., `auth`, `rbac`, `session-mgmt`).
- **Wikilinks**: include 1–2 `[[OTHER-ENTRY-ID]]` cross-references
  per entry where logical, even to faux IDs in the same corpus.

Faux project / system names are fine — invent things like "kelpie"
(a pipeline framework), "sandbar" (a UI), "tideline" (an observability
stack). Pick a few names and use them consistently across entries so
cross-references feel coherent.

### Step 6 — Rebuild `index.md` and `log.md`

After all entries are written:

- Replace `index.md` with a minimal table of contents listing each
  entry by ID and title under its type heading. Keep it lean.
- Replace `log.md` with one chronological line per entry creation
  (faux dates spread across the past 90 days for realism). Format:
  `YYYY-MM-DD — {ID}: {title}`.

These files exist in the wiped vault but reference deleted entries —
rebuilding makes the worktree feel like a live vault for the test.

### Step 7 — Commit and report

Stage all changes:

```
git add -A
git commit -m "test: populate 60 faux entries for Pattern #1 ranking probes"
```

Then return a structured report:

```
ENTRY COUNTS
- ideas: 15 (12 active, 3 completed)
- bugs: 15 (6 active, 9 resolved)
- ... etc.

OV_MODE DISTRIBUTION
- missing field: 48
- pointer (explicit): 9
- none: 3 — list IDs

RANKING TRAPS (one line per pair, format: "<type>: <ID-A> <-> <ID-B> — <description>")
- near-duplicate ideas: IDEA-007 <-> IDEA-011 — both propose ...
- ...

DEVIATIONS
- (any place you couldn't follow the brief, with reason)
```

### Constraints

- Do NOT modify files outside the worktree.
- Do NOT push to remote.
- Do NOT add Claude attribution to commit messages.
- Do NOT install packages or run external services.
- Templates in `_templates/` are READ-ONLY for this task — do not
  modify them.
- Total work budget: aim for ~45 minutes of subagent time. If
  scope looks like it'll exceed, prioritize coverage of the eight
  thematic clusters and the required ranking traps over hitting exact
  per-type counts.
