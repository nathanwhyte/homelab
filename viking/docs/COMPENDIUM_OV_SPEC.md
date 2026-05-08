# Compendium ↔ OpenViking: Pointer Index Spec (Pattern #1)

## Status & scope

Prototype specification, v1. Covers **Pattern #1 (pointer index)** only —
full-content indexing (Pattern #2) is reserved as future work. The design
deliberately keeps the addressing scheme and frontmatter contract
forward-compatible so Pattern #2 can be layered in without churning v1
entries.

## Goal

Let agents answer "find our work related to X" queries against the
compendium vault using OV's semantic search, **without** duplicating
markdown content into OV. OV stores a small **pointer payload** per
entry; the source markdown is the only place full content lives. When a
hit is returned, the agent reads the file for detail.

## Frontmatter contract

Every compendium entry SHOULD declare an `ov_mode` field in YAML
frontmatter:

```yaml
---
id: IDEA-042
title: Compendium ↔ OpenViking integration
ov_mode: pointer
tags: [openviking, knowledge-base]
---
```

| Value | v1 behavior |
|---|---|
| `pointer` | Sync to OV with the pointer payload format below. **Default** when field is missing. |
| `none` | Never sync. Use for transient notes, scratch entries, daily logs. |
| `full` | **Reserved.** Pattern #2 placeholder; treated as `pointer` in v1. |

Default-on-missing is intentional: the bulk of existing entries should
become discoverable without per-file edits.

## Indexed payload format

For each entry with effective `ov_mode = pointer`, the sync uploads:

```
[ov_mode: pointer]
Path: ~/code/compendium/<rel-path>/<filename>.md
ID: <entry-id>
Title: <title>
Tags: <tag1>, <tag2>, ...
---
<frontmatter as YAML, preserved verbatim>
---
<first paragraph after frontmatter — the entry's TL;DR>

Headings:
## <h2 #1>
## <h2 #2>
### <h3 #1>
...
```

**Embedding surface rationale:** OV ranks against tokens in the indexed
content. First paragraph + heading list captures the entry's
"aboutness" without requiring authors to write a separate summary. For
templated entries (IDEA-NNN, BUG-NNN, RUNBOOK-NNN) the structural
headings (Problem / Sketch / Symptom / Root cause / Fix) become reliable
match anchors.

**Deliberately omitted from the payload:**

- Section bodies (kept in markdown only — the point of Pattern #1)
- Code blocks
- Inline tables
- Wikilink targets (`Path:` trailer suffices)

## OV addressing

| Field | Value |
|---|---|
| `name` | Lowercase entry ID — `idea-042`, `bug-117`, `runbook-003` |
| `target_dir` | `viking://resources/compendium/<vault-subdir>/` (mirrors `~/code/compendium/<vault-subdir>/`) |

Same `name` is used regardless of `ov_mode`, so a future flip
`pointer → full` upserts in place rather than producing a duplicate.

## Sync mechanism (v1)

Two triggers:

1. **Manual** — agent invokes `compendium-sync sync-one <path>` after
   editing an entry, or `compendium-sync sync-all` after bulk edits.
2. **Automatic** — git `pre-commit` hook in
   `~/code/compendium/.git/hooks/pre-commit` runs
   `compendium-sync sync-changed` over staged `.md` files. Catches edits
   made directly in Obsidian.

**Idempotency:**

- `viking_add_text` is upsert by `name` — re-running on unchanged
  entries produces no observable diff.
- Sync script computes payload deterministically from frontmatter +
  markdown.

**Deletion:**

- Entries flipped to `ov_mode: none` get an explicit OV delete (handles
  the case where they were previously `pointer`).
- Files **removed from the vault** are NOT auto-deleted from OV in v1.
  Manual cleanup or periodic reconciliation deferred to v2.

**Out of scope for v1:**

- Obsidian save-hook / community plugin
- File watcher daemon
- Real-time bidirectional sync

## Search workflow (agent-side)

For prompts like "find our work related to X" / "what have we written
about Y":

1. Call `viking_find(query, scope="compendium")` — narrow scope, use
   QUICK mode.
2. For each hit, inspect the leading `[ov_mode: pointer]` header.
3. Extract the `Path:` line and `Read` the markdown file.
4. **Synthesize the answer from the markdown content, not the OV
   chunk** — the chunk is only a discovery surface, not the source of
   truth.

This workflow lives in `~/code/compendium/CLAUDE.md` (additions section
below). No skill required for the read path; it's prose guidance.

## Backfill plan

One-time operation when the spec lands:

1. Walk `~/code/compendium/` for `*.md` files.
2. For each entry without `ov_mode`: add `ov_mode: pointer` to
   frontmatter. Commit as a single migration with a clear message.
3. Run `compendium-sync sync-all` to populate OV from scratch.
4. Spot-check with three real "find work related to X" queries to
   verify ranking quality before declaring v1 done.

## CLAUDE.md additions (compendium project)

Add the following section to `~/code/compendium/CLAUDE.md`:

```markdown
## OpenViking integration

Compendium entries are mirrored to OpenViking as **pointer payloads**
(entry metadata + first paragraph + heading list + source path). Full
content stays in markdown; OV is for discovery, not retrieval.

### Frontmatter contract

Every entry SHOULD have an `ov_mode` field:

- `pointer` (default when missing) — synced to OV as a pointer payload
- `none` — never synced (transient notes, logs, scratch)

### Search workflow

For "find our work related to X" queries:

1. `viking_find(query, scope="compendium")`
2. Parse the `[ov_mode: pointer]` header in each hit
3. Extract `Path:` and Read the markdown for full detail
4. Answer from the markdown, not the OV chunk

### Sync workflow

After creating or editing an entry:

- Invoke the `compendium-sync` skill (`sync-one <path>`), OR
- Commit changes — the git pre-commit hook syncs staged files.

After bulk changes:

- Invoke `compendium-sync sync-all`.

See `~/code/homelab/viking/COMPENDIUM_OV_SPEC.md` for the full spec.
```

## Skill spec — `compendium-sync`

**Description (skill metadata):** Sync compendium entries to OpenViking
as pointer payloads. Use after creating, editing, or bulk-changing
markdown entries in `~/code/compendium/`.

**Modes:**

| Mode | Args | Behavior |
|---|---|---|
| `sync-one` | `<path>` to single `.md` | Parse frontmatter, build pointer payload, upsert to OV |
| `sync-changed` | none (reads `git diff --cached --name-only`) | Sync staged `.md` files; called by pre-commit hook |
| `sync-all` | none | Walk vault; sync every entry whose effective `ov_mode != none` |
| `backfill` | none | Add missing `ov_mode: pointer` to frontmatter, commit, then `sync-all` |

**Implementation notes (defer to a separate plan doc):**

- Parse YAML frontmatter (e.g., `python-frontmatter`)
- Extract first paragraph (everything between frontmatter close and
  first heading or blank line)
- Walk AST for headings (or simple regex over `^#{1,3} `)
- Compose payload string per the format above
- Call `viking_add_text(content, name, target_dir)` per entry
- For `ov_mode: none` previously-synced entries, call OV delete (if
  available) — otherwise log for manual cleanup

## Open questions / deferred to v2+

1. **Pattern #2 (full-content for runbooks and references)** —
   explicit non-goal for v1. When added: only the payload format and
   mode-handling branch change; addressing and trigger logic stay
   identical.
2. **OV deletion when markdown files are deleted from the vault** — v1
   leaks orphaned OV entries. Options: track deletions via git, run a
   periodic reconciliation pass, set OV TTL.
3. **Obsidian save-hook** — would enable real-time sync without git
   commit, but requires plugin development. Defer.
4. **Reverse flow (OV findings → compendium entries)** — Pattern #4
   from the design discussion. Promote high-value session findings into
   structured IDEA/BUG entries. Separate workflow, not blocked by v1.
5. **Wikilink prefetch** — when a hit's markdown references
   `[[OTHER-ENTRY]]`, should the agent auto-Read the linked entry?
   Likely yes, but agent-side prompt concern, not spec concern.
6. **Ranking quality measurement** — no defined metric for "is the
   pointer payload enough." Spot-check during backfill; revisit if
   recall feels weak.
