# `ov` CLI — Agent Usage Guide

OpenViking is an agent-native context database. The `ov` CLI (v0.3.14) is the primary interface for reading, writing, and searching knowledge.

## Why CLI over MCP

Prefer `ov` CLI commands over MCP tools for all OV operations. MCP tool calls have significant token overhead — each call burns ~500-1000 tokens for the tool invocation frame, and many MCP operations return verbose structured data that floods context.

The `ov` CLI produces compact, table-formatted output by default and runs via `Bash` with minimal token cost. For any OV operation, use the CLI equivalent:

| MCP Tool | CLI Equivalent | Token Savings |
|----------|----------------|---------------|
| `viking_find` | `ov find "query" -u <scope>` | ~80% |
| `viking_search` | `ov search "query" -u <scope>` | ~80% |
| `viking_add_text` | `ov add-resource <file> --to <uri> --wait` | ~60% |
| `viking_ls` | `ov ls <uri>` | ~75% |
| `viking_mkdir` | `ov mkdir <uri>` | ~70% |
| `viking_rm` | `ov rm <uri>` | ~70% |

**Exceptions**: Use MCP only when no CLI equivalent exists or when working inside a subagent that lacks shell access.

## Installation

```sh
uv tool install openviking
# Binary: ~/.local/bin/ov (symlink to ~/.local/share/uv/tools/openviking/bin/ov)
```

## Configuration

The CLI connects to a remote server. Config is stored locally and shown via:

```sh
ov config show
```

| Field | Value |
|-------|-------|
| `url` | `https://context.nathanwhyte.dev` (external via Cloudflare Tunnel) |
| `api_key` | 64-char hex key |
| `timeout` | 60s |
| `output` | `table` (default) |

Server: in-cluster URL is `http://openviking.viking.svc.cluster.local:1933`.

## URI Structure

| Scope | URI | Purpose |
|-------|-----|---------|
| Resources | `viking://resources/` | Long-lived knowledge independent of agent/account |
| Agent | `viking://agent/` | Agent memories, instructions, skills |
| Session | `viking://session/` | Single conversation snapshots |
| User | `viking://user/` | User's long-term memory |
| Temp | `viking://temp/` | Ephemeral scratch space (internal) |
| Queue | `viking://queue/` | Processing queue (internal) |

Only `resources`, `user`, `agent`, and `session` are addressable through the public API.

Use **trailing slash** for directories: `viking://resources/homelab/` (dir) vs `viking://resources/homelab/gpu.md` (file).

## Global Options

| Flag | Default | Description |
|------|---------|-------------|
| `-o, --output` | `table` | Output format: `table` or `json` |
| `-c, --compact` | `true` | Compact JSON or simplified table |
| `--account` | — | Account identifier (X-OpenViking-Account header) |
| `--user` | — | User identifier (X-OpenViking-User header) |
| `--agent-id` | — | Agent identifier (X-OpenViking-Agent header) |
| `--sudo` | — | Use root API key for admin privileges |

## Command Reference

### 1. Service Health & Status

```sh
# Quick health check — workers, merged read, overall status
ov health

# Full component status — queues, vector DB, VLM, retrieval stats, locks
ov status

# Wait for async processing to drain
ov wait --timeout 120

# Version check
ov version

# System-level subcommands (aliases for the above)
ov system health
ov system status
```

**`ov health` output:**
- `status` — overall health (`ok` or error)
- `workers` — per-worker health map (`worker_url: true/false`)
- `healthy_count` / `total_count` — worker tally
- `merged` — merged read endpoint status (`active`, `healthy`, `stale`)

**`ov status` output sections:**
- **queue** — embedding, semantic, semantic-nodes pending/in-progress/processed/errors
- **vikingdb** — collections with index count, vector count, status
- **vlm** — token usage data
- **lock** — active locks
- **retrieval** — query stats (total, zero-result rate, avg score, avg latency, rerank usage)
- **system** — overall system health

### 2. Browsing & Navigation

```sh
# List root scopes
ov ls

# List with recursion
ov ls viking://resources -r

# Tree view (default depth: 3)
ov tree viking://resources -L 5

# Simple path output (no table — good for scripting)
ov ls viking://resources -s

# Show hidden files (system files like .abstract.md)
ov ls viking://agent -a

# Limit nodes (default: 256)
ov ls viking:// -n 100

# Get resource metadata (size, type, modified time)
ov stat viking://resources/homelab
```

**`ov tree` options:**

| Flag | Default | Description |
|------|---------|-------------|
| `-L, --level-limit` | 3 | Maximum depth to traverse |
| `-n, --node-limit` | 256 | Maximum nodes to list |
| `-l, --abs-limit` | 128 | Abstract content limit |
| `-a, --all` | false | Show hidden files |

### 3. Content Retrieval — Three-Level Hierarchy

OpenViking has a **L0/L1/L2** content hierarchy for efficient context-aware reading.

| Level | Command | Content | Use Case |
|-------|---------|---------|----------|
| L0 | `ov abstract <uri>` | ~100 tokens — one-paragraph summary | First pass — decide if worth reading |
| L1 | `ov overview <uri>` | ~2k tokens — multi-paragraph overview | Second pass — understand structure |
| L2 | `ov read <uri>` | Full file content | Deep read — consume the actual resource |

```sh
# L0 — quick skim
ov abstract viking://resources/homelab/gpu/thermal-assessment

# L1 — detailed summary with navigation
ov overview viking://resources/homelab/gpu/thermal-assessment

# L2 — full content
ov read viking://resources/homelab/gpu/thermal-assessment.md

# Download binary file to local path
ov get viking://resources/homelab/logo.png /tmp/logo.png
```

**Efficient reading workflow:**
1. `ov ls -r` or `ov tree` to discover what's available
2. `ov abstract <uri>` to decide relevance (cheap — ~100 tokens)
3. `ov read <uri>` only if the abstract is useful (expensive — full content)

### 4. Search

```sh
# Semantic search — vector similarity (no session context needed)
ov find "kubernetes cluster setup" -n 10

# Context-aware search — uses session context for reranking
ov search "how is the cluster configured" --session-id <id>

# Pattern search (grep) — string/regex matching
ov grep "error|timeout" -i -n 20

# Glob pattern — filename matching
ov glob "*.yaml" -n 50

# Scope search to a subtree (IMPORTANT: always scope to narrow results)
ov find "GPU configuration" -u viking://resources/homelab -n 5

# Score threshold filter
ov find "ollama" -t 0.5

# Time-bounded search
ov find "deployment" --after 48h --before 7d
```

| Command | Method | Default Limit | Default Scope | Best For |
|---------|--------|---------------|---------------|----------|
| `find` | Vector semantic | 10 | root | Conceptual queries, architecture, design |
| `search` | Context-aware + rerank | 10 | root | Complex tasks needing session context |
| `grep` | String/regex | 256 | root | Exact matches, error messages, code |
| `glob` | File glob | 256 | root | Finding files by name pattern |

**`find` and `search` options:**

| Flag | Description |
|------|-------------|
| `-u, --uri` | Target URI to scope search |
| `-n, --node-limit` | Maximum results (default: 10) |
| `-t, --threshold` | Minimum score threshold |
| `--after` | Only results on/after this time (e.g., `48h`, `7d`, `2026-03-10`) |
| `--before` | Only results on/before this time |

**Always use `-u` to scope searches.** Unscoped searches scan everything and return noisier results.

**Quote multi-word queries.** The `ov find` and `ov search` commands accept a single `<QUERY>` argument. Multi-word queries must be quoted:

```sh
# Correct — single quoted argument
ov find "GPU thermal management" -n 5

# Wrong — shell splits into separate arguments
ov find GPU thermal management -n 5
# → error: unexpected argument 'thermal'
```

**`ov grep` requires a scope.** Grep from root (`viking://`) searches all scopes and will fail. Always scope to a subtree:

```sh
# Correct
ov grep "error" -i -u viking://resources/homelab/

# Wrong — searches from root, fails
ov grep "error" -i
```

### 5. Writing Content

```sh
# Import a local file with metadata
ov add-resource ./docs/architecture.md \
  --parent viking://resources/homelab/ \
  --reason "Architecture documentation" \
  --wait

# Specify exact target URI (directory must already exist)
ov add-resource ./README.md \
  --to viking://resources/homelab/README.md \
  --wait

# Import a directory with filters
ov add-resource ./codebase \
  --parent viking://resources/homelab/ \
  --ignore-dirs "node_modules,dist" \
  --include "*.md,*.py" \
  --exclude "*.tmp,*.log" \
  --wait

# Write text content to an existing resource (create or update)
ov write viking://resources/homelab/gpu/thermal-assessment.md \
  --content "GPU thermal throttling occurs at 83C on the GTX 1080..."

# Write from a local file
ov write viking://resources/homelab/gpu/assessment.md \
  --from-file ./local-assessment.md

# Append to existing content
ov write viking://resources/homelab/gpu/notes.md \
  --content "Additional finding: power limit set to 220W" \
  --append

# Add a skill
ov add-skill ./path/to/SKILL.md --wait

# Add memory in one shot (creates session, adds messages, commits)
ov add-memory "Learned that the cluster uses Flannel host-gw backend"
ov add-memory '{"role":"user","content":"What models are available?"}'
ov add-memory '[{"role":"user","content":"..."},{"role":"assistant","content":"..."}]'

# Directory operations
ov mkdir viking://resources/homelab/new-topic --description "Description of this topic directory"
ov rm viking://resources/homelab/old-topic -r

# Move/rename
ov mv viking://resources/homelab/old-name viking://resources/homelab/new-name
```

**`ov add-resource` options:**

| Flag | Description |
|------|-------------|
| `--to <URI>` | Exact target URI (must not exist yet) |
| `--parent <URI>` | Target parent directory (must already exist) |
| `--reason` | Reason for import |
| `--instruction` | Additional instruction for processing |
| `--wait` | Block until embedding completes |
| `--timeout <sec>` | Wait timeout (only with `--wait`) |
| `--strict` | Fail if any unsupported files found |
| `--ignore-dirs` | Comma-separated dirs to skip |
| `--include` | File extensions to include (e.g., `"*.md,*.py"`) |
| `--exclude` | File extensions to exclude |

**`ov write` options:**

| Flag | Description |
|------|-------------|
| `--content <text>` | Content to write |
| `--from-file <path>` | Read content from local file |
| `--append` | Append instead of replacing |
| `--mode` | Write mode: `replace`, `append`, or `create` (default: replace) |
| `--wait` | Wait for async processing to finish |
| `--timeout <sec>` | Optional wait timeout |

**Processing notes:**
- Content is vectorized asynchronously after import
- **Always use `--wait`** to block until embedding completes — without it, concurrent uploads compete for queue locks
- Check `ov status` → queue section to monitor pending work
- The `--parent` flag may not work over remote connections (BasicAuth limitation). Use `--to` with the exact URI for remote adds.

### 6. Relations (Links Between Resources)

```sh
# Link resources
ov link viking://resources/homelab/README.md \
  viking://resources/homelab/CLAUDE.md \
  --reason "related documentation"

# Unlink
ov unlink viking://resources/homelab/README.md \
  viking://resources/homelab/CLAUDE.md

# List relations of a resource
ov relations viking://resources/homelab/README.md
```

### 7. Export & Import

```sh
# Export to .ovpack (packaged export format)
ov export viking://resources /tmp/backup.ovpack

# Import .ovpack
ov import /tmp/backup.ovpack viking://resources

# Force overwrite on conflict, skip vectorization
ov import /tmp/backup.ovpack viking://resources --force --no-vectorize
```

**Known issue:** `ov export` reports `{"ok":true}` remotely but doesn't write the file to the local filesystem — the file stays server-side.

### 8. Session Management

```sh
# Create new session
ov session new

# List sessions
ov session list

# Get session details
ov session get <session-id>

# Get full merged session context
ov session get-session-context <session-id>

# Get one completed archive for a session
ov session get-session-archive <session-id>

# Add message to existing session
ov session add-message <session-id> "user message here"

# Commit session (archive + extract memories)
ov session commit <session-id>

# Delete session
ov session delete <session-id>
```

**Session workflow for agents:**
1. `ov session new` → gets session ID
2. `ov session add-message <id> "..."` → add messages progressively
3. `ov session commit <id>` → finalize, triggers background memory extraction
4. `ov search "..." --session-id <id>` → context-aware search using session context

### 9. Reindexing

Regenerates `.abstract.md` and `.overview.md` files when content changes or summaries are stale:

```sh
# Regenerate abstracts/overviews for a specific resource
ov reindex viking://resources/homelab/gpu/assessment.md

# Force regenerate even if summaries already exist
ov reindex viking://resources/homelab/ -r

# Wait for reindex to complete (recommended after bulk changes)
ov reindex viking://resources/homelab/ -r --wait
```

| Flag | Description |
|------|-------------|
| `-r, --regenerate` | Force regenerate summaries even if they exist |
| `--wait` | Wait for reindex to complete |

### 10. Admin (Multi-Tenant)

```sh
ov admin --help    # Account and user management
ov observer --help # Observer status
ov system crypto   # Cryptographic key management
```

### 11. Privacy

```sh
ov privacy --help  # Privacy config management
```

### 12. Interactive Commands

```sh
# Interactive TUI file explorer (terminal UI)
ov tui

# Chat with vikingbot agent
ov chat
```

### 13. Version & Config

```sh
# Show CLI version
ov version

# Show current configuration
ov config show

# Config management
ov config --help
```

## Agent Workflows

### Discover what's in the system

```sh
ov health                      # Is it up?
ov status                      # What's the queue/DB state?
ov ls viking://resources/      # List top-level resources
ov tree viking://resources/homelab/ -L 3  # Project tree
```

### Find relevant knowledge

```sh
# Quick — pattern match a known term
ov grep "ollama" -i

# Semantic — find conceptually similar
ov find "GPU inference setup" -u viking://resources/homelab/

# Focused — scope to a subtree
ov find "GPU inference" -u viking://resources/homelab/gpu/ -n 5
```

### Read content efficiently

```sh
# L0 skim → L2 read only if relevant
ov abstract viking://resources/homelab/gpu/assessment.md
# [if abstract looks useful:]
ov read viking://resources/homelab/gpu/assessment.md
```

### Add knowledge at point of discovery

```sh
# Single memory (one-liner)
ov add-memory "VLM routes to Dashscope by default unless model name prevents auto-detection"

# File import (structured content)
ov add-resource ./docs/fix-report.md \
  --to viking://resources/homelab/openviking/vlm-fix.md \
  --wait

# Inline write (no local file needed)
ov write viking://resources/homelab/openviking/queue-cleanup-fix.md \
  --content "# Queue Database Stale Lock Fix\n\n## Issue\nQueue processing stalled after pod restart...\n## Root Cause\nQueue database persists across restarts...\n## Fix\nAdded initContainer to clear queue.db...\n## Commit\n4225710" \
  --wait
```

### Session-based research

```sh
SESSION=$(ov session new -o json | jq -r '.id')
ov session add-message "$SESSION" "Looking into GPU utilization patterns"
# ... do searches, read content ...
ov session commit "$SESSION"
```

### Restructure or clean up resources

```sh
# Remove stale content
ov rm viking://resources/volcengine/ -r

# Move content to better location
ov mv viking://resources/homelab/old-name viking://resources/homelab/gpu/

# Reindex after reorganization
ov reindex viking://resources/homelab/ -r --wait
```

## Tips & Known Issues

- **`ov export` over remote**: API returns `{"ok":true}` but no file on disk — server-side only.
- **`ov add-resource --parent`**: May fail over remote connection. Use `--to` with exact URI instead.
- **Async processing**: After adding content, check `ov status` → queue to confirm embedding completed. Always use `--wait`.
- **Default limits**: `ls` caps at 256 nodes, `find`/`search` at 10 results. Adjust with `-n`/`--node-limit`.
- **L0/L1 on directories**: `abstract`/`overview` only work on file URIs, not directory URIs.
- **Output format**: `-o json` for programmatic consumption, `-c` for compact representation.
- **Config file**: Override server URL and API key with `ov config` or env vars (check `ov config --help`).
- **Concurrent uploads**: Avoid adding multiple resources without `--wait` — they compete for queue locks and can stall processing.
- **Quote multi-word queries**: `ov find "GPU thermal"` not `ov find GPU thermal`. The CLI treats unquoted words as separate arguments.
- **Scope grep/find searches**: `ov grep "error" -u viking://resources/homelab/` — grep from root fails because it tries to search across all scopes.
- **Time filters**: `--after`/`--before` accept relative (`48h`, `7d`) or absolute (`2026-03-10`, ISO-8601) timestamps.
- **Semantic processing delay**: After `add-resource` or `write`, content enters the embedding queue. Use `--wait` or poll `ov abstract <uri>` until ready before searching for it.