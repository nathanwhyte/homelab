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
themes: [retrieval]
domains: [knowledge-base]
retrieval_summary: OpenViking pointer indexing for Compendium knowledge-base retrieval.
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
Type: <type>
Status: <status>
Repo: <repo>
Priority: <priority or severity>
Customer: <customer>
Customers: <customer/customer list>
People: <people/person/assignee list>
Themes: <themes>
Domains: <domains/domain/topic>
Pipeline: <pipeline>
Tags: <tag1>, <tag2>, ...
Aliases: <aliases>
Retrieval Summary: <retrieval_summary>
Related: <related>
Source: <source>
Sources: <sources>
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
content. Frontmatter search fields capture people, customers, aliases,
and project-level intent that may not appear in the body. First
paragraph + heading list captures the entry's "aboutness" without
duplicating full content. For templated entries (IDEA-NNN, BUG-NNN,
RUNBOOK-NNN) the structural headings (Problem / Sketch / Symptom / Root
cause / Fix) become reliable match anchors.

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

1. **Manual** — operator runs
   `python3 _scripts/compendium-sync.py sync` from the compendium repo.
   Use `--limit` for staged batches.
2. **Controlled bulk refresh** — run `sync` after metadata migrations.
   `add-resource --to` refreshes existing resources in place. Reserve
   wipe/resync for reconciliation or corrupted state.

**Idempotency:**

- Sync script computes payload deterministically from frontmatter +
  markdown.
- `ov add-resource <tmpfile> --to <uri>` creates new resources and
  refreshes existing resources in place.
- The script keeps `--update` as a deprecated no-op for compatibility;
  delete/re-add is not required for normal pointer payload refreshes.
- Existing-resource refresh replaces the pointer payload for selected
  entries and rebuilds OpenViking-derived embeddings/semantic artifacts.

**Deletion:**

- Entries flipped to `ov_mode: none` are skipped by future syncs. If the
  entry already exists in OV, delete that URI manually or run a
  reconciliation cleanup.
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
3. Run `python3 _scripts/compendium-sync.py sync` from the compendium repo to populate OV
   from scratch.
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

Entries should also maintain retrieval metadata: `people`,
`customers`, `themes`, `domains`, `aliases`, `tags`, and
`retrieval_summary`.

### Search workflow

For "find our work related to X" queries:

1. `viking_find(query, scope="compendium")`
2. Parse the `[ov_mode: pointer]` header in each hit
3. Extract `Path:` and Read the markdown for full detail
4. Answer from the markdown, not the OV chunk

### Sync workflow

After creating or editing an entry:

- Run a controlled sync from the homelab repo.
- Existing OV resources are refreshed in place by the normal sync path.

After bulk changes:

- Run `python3 _scripts/compendium-sync.py sync` from the compendium repo. Wipe the target
  namespace only when reconciling ghosts, removed files, or corrupted
  state.

See `~/code/homelab/viking/COMPENDIUM_OV_SPEC.md` for the full spec.
```

## Script spec — `compendium-sync.py`

**Description:** Sync compendium entries to OpenViking as pointer
payloads. Use after creating, editing, or bulk-changing markdown entries
in `~/code/compendium/`.

**Modes:**

| Mode | Args | Behavior |
|---|---|---|
| `stats` | none | Count eligible entries and summarize payload sizes |
| `preview` | `<path>` to single `.md` | Print the computed pointer payload |
| `plan` | optional paths or filters such as `--limit`, `--type`, `--repo` | Print selected target URIs without syncing |
| `sync` | optional paths or filters plus `--dry-run`, `--no-wait`, `--delete-path`, deprecated `--update` | Add or refresh selected resources; optionally remove old pointer URIs for moved files |

**Implementation notes (defer to a separate plan doc):**

- Parse YAML frontmatter (e.g., `python-frontmatter`)
- Extract first paragraph (everything between frontmatter close and
  first heading or blank line)
- Walk AST for headings (or simple regex over `^#{1,3} `)
- Compose payload string per the format above
- Call `ov add-resource <tmpfile> --to <uri>` per entry
- For moved files, pass `--delete-path <old-path>` while syncing the new
  path so the previous pointer URI is removed.
- For `ov_mode: none` previously-synced entries, delete that URI
  manually or rely on a reconciliation cleanup.

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
