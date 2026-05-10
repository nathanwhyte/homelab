# OpenViking + Compendium Usage Scenarios

Date: 2026-05-10

These scenarios validate the real Compendium pointer index after the
single-worker local-AGFS OpenViking cutover. They use the live target:

```text
viking://resources/compendium
```

The current resync indexed 62 entries by default, excluding active/open work
items via `compendium-sync.py`'s default active-status filter.

## How to run

Run the scenario helper from the homelab repo:

```bash
python3 viking/tools/compendium-ov-scenarios.py --list
python3 viking/tools/compendium-ov-scenarios.py --run all
```

If your `ov` CLI config points at the public ingress and fails auth/network
checks, use the same `OPENVIKING_URL` and `OPENVIKING_KEY` environment that was
used for the successful resync.

## Scenario Matrix

### 1. Direct Bug Lookup

Query:

```bash
ov find -u viking://resources/compendium -n 5 "kinde roles pagination"
```

Expected:

- Top results include `viking://resources/compendium/bugs/bug-053.md`.
- The abstract mentions the DipDash/Kinde roles pagination issue.

Purpose:

- Validates direct title/topic lookup.
- Confirms active bug entries with non-active statuses are discoverable.

### 2. Resolved Bug Lookup

Query:

```bash
ov find -u viking://resources/compendium -n 5 "duplicate company info peoplease locations"
```

Expected:

- Top results include `viking://resources/compendium/bugs/resolved/bug-005.md`.
- The abstract mentions duplicate alias / peoplease locations.

Purpose:

- Validates resolved bug entries are indexed and retrievable.

### 3. Semantic Pipeline Lookup

Query:

```bash
ov find -u viking://resources/compendium -n 5 "column mapping macro calculated columns"
```

Expected:

- Results include `info/pipeline-column-mappings.md`.
- Results include either `bugs/bug-046.md` or
  `info/pipeline-calculated-columns.md`.

Purpose:

- Exercises semantic overlap between reference material and bug reports.
- Checks that information docs compete appropriately with incident notes.

### 4. Cross-Type Policy Extension Lookup

Query:

```bash
ov find -u viking://resources/compendium -n 5 "policy extension stage 1 column mappings"
```

Expected:

- Results include `tasks/task-035.md`.
- Results include `info/pipeline-column-mappings.md` or
  `tasks/task-032.md`.

Purpose:

- Validates cross-type retrieval across tasks and reference docs.

### 5. Snowflake Lookup Crash Lookup

Query:

```bash
ov find -u viking://resources/compendium -n 5 "snowflake crash lookup column spaces"
```

Expected:

- Results include `bugs/resolved/bug-044.md`.
- Related Snowflake/Mage lookup failures may also appear.

Purpose:

- Tests fuzzy retrieval where query terms do not exactly match the file slug.

### 6. Active-Status Exclusion

Command:

```bash
ov stat viking://resources/compendium/bugs/bug-000.md
```

Expected:

- The command should fail or report missing resource.

Purpose:

- Confirms default resync excluded active/open work items.

### 7. Pointer Payload Path

Command:

```bash
ov grep -u viking://resources/compendium/bugs/bug-053.md -n 5 "Path: ~/code/compendium/bugs/BUG-053"
```

Expected:

- Output contains a `Path: ~/code/compendium/...` line.
- Agent workflow should then read the local markdown path for full detail.

Purpose:

- Validates the pointer-index contract rather than treating OpenViking as the
  source of full truth.
- Avoids depending on OpenViking's generated child filename under each uploaded
  resource directory.

### 8. Tree Shape

Command:

```bash
ov tree -L 3 -n 120 viking://resources/compendium
```

Expected:

- Top-level directories include `bugs`, `features`, `ideas`, `info`, `plans`,
  `tasks`, and `thoughts`.

Purpose:

- Confirms the resync populated expected namespaces.

## Agent Acceptance Criteria

For any "find our work related to X" task:

1. Use `ov find -u viking://resources/compendium`.
2. Inspect returned pointer payloads.
3. Extract the `Path:` line.
4. Read the local markdown file under `~/code/compendium`.
5. Answer from the local markdown, using OpenViking only as discovery.

A scenario passes if the expected URI appears in the top five results and the
agent can identify the correct local markdown file from the pointer payload.

## Watch Items

- If direct lookups miss obvious entries, inspect whether the target entry was
  excluded by active status.
- If semantic lookups return only generic info pages, the pointer payload may
  need richer first-paragraph or heading content.
- If reads return generated child paths under a resource directory, prefer the
  `Path:` field inside the pointer payload over the generated OpenViking child
  filename.
