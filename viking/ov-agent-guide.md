# `ov` CLI — Agent Usage Guide

OpenViking is an agent-native context database. The `ov` CLI (v0.2.13) is the primary interface for reading, writing, and searching knowledge.

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
| Temp | `viking://temp/` | Ephemeral scratch space |

## Global Options

| Flag | Default | Description |
|------|---------|-------------|
| `-o, --output` | `table` | Output format: `table` or `json` |
| `-c, --compact` | `true` | Compact JSON or simplified table |

## Command Reference

### 1. Service Health & Status

```sh
# Quick health check — workers, merged read, overall status
ov health

# Full component status — queues, vector DB, VLM, retrieval stats, locks
ov status

# Wait for async processing to drain
ov wait --timeout 120

# System-level subcommands (aliases for the above)
ov system health
ov system status
ov system wait
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
ov tree viking://resources --level-limit 5

# Simple path output (no table — good for scripting)
ov ls viking://resources -s

# Show hidden files
ov ls viking://agent -a

# Limit nodes (default: 256)
ov ls viking:// -n 100

# Get resource metadata (size, type, modified time)
ov stat viking://resources/projects/claudinator
```

### 3. Content Retrieval — Three-Level Hierarchy

OpenViking has a **L0/L1/L2** content hierarchy for efficient context-aware reading.

| Level | Command | Content | Use Case |
|-------|---------|---------|----------|
| L0 | `ov abstract <uri>` | One-paragraph summary (~50-150 words) | First pass — decide if worth reading |
| L1 | `ov overview <uri>` | Multi-paragraph overview | Second pass — understand structure |
| L2 | `ov read <uri>` | Full file content | Deep read — consume the actual resource |

```sh
# L0 — quick skim
ov abstract viking://resources/projects/claudinator

# L1 — detailed summary
ov overview viking://resources/projects/claudinator

# L2 — full content
ov read viking://resources/projects/claudinator/CHANGELOG.md

# Download binary file to local path
ov get viking://resources/projects/claudinator/logo.png /tmp/logo.png
```

**Note:** `abstract` and `overview` only work on file URIs, not directory URIs. They return AI-generated summaries stored as `.abstract.md` and `.overview.md` sidecar files.

When reading for an agent workflow:
1. `ov ls -r` to discover what's available
2. `ov abstract <uri>` to decide relevance
3. `ov read <uri>` to consume the actual content

### 4. Search

```sh
# Semantic search — vector similarity
ov find "kubernetes cluster setup" -n 10

# Context-aware search — uses session context for reranking
ov search "how is the cluster configured" --session-id <id>

# Pattern search (grep) — string/regex matching
ov grep "error|timeout" -i -n 20

# Glob pattern — filename matching
ov glob "*.yaml" -n 50

# Scope search to a subtree
ov find "GPU configuration" -u viking://resources/projects/homelab -n 5

# Score threshold filter
ov find "ollama" -t 0.5
```

| Command | Method | Default Limit | Default Scope |
|---------|--------|---------------|---------------|
| `find` | Vector semantic | 10 | root |
| `search` | Context-aware + rerank | 10 | root |
| `grep` | String/regex | 256 | root |
| `glob` | File glob | 256 | root |

**Pro tip:** Use `grep` for exact matches (code, error messages), `find` for conceptual queries (architecture, design).

### 5. Writing Content

```sh
# Import a local file/directory with metadata
ov add-resource ./docs/architecture.md \
  --parent viking://resources/projects/homelab/ \
  --reason "Architecture documentation" \
  --wait

# Specify exact target URI (directory must already exist)
ov add-resource ./README.md \
  --to viking://resources/projects/homelab/README.md \
  --wait

# Import options
ov add-resource ./codebase \
  --parent viking://resources/projects/homelab/ \
  --no-strict \                    # Loose directory scanning
  --ignore-dirs "node_modules,dist" \
  --include "*.md,*.py" \          # Only these extensions
  --exclude "*.tmp,*.log" \        # Exclude these
  --watch-interval 60              # Auto-reindex every 60 min
  --wait

# Add a skill
ov add-skill ./path/to/SKILL.md --wait

# Add memory in one shot (creates session, adds messages, commits)
ov add-memory "Learned that the cluster uses Flannel host-gw backend"
ov add-memory '{"role":"user","content":"What models are available?"}'
ov add-memory '[{"role":"user","content":"..."),{"role":"assistant","content":"..."}]'

# Directory operations
ov mkdir viking://resources/projects/new-project
ov rm viking://resources/projects/old-project -r

# Move/rename
ov mv viking://resources/projects/old viking://resources/projects/new
```

**Processing notes:**
- Content is vectorized asynchronously after import
- Use `--wait` to block until embedding completes
- Check `ov status` → queue section to monitor pending work
- The `--parent` flag may not work over remote connections (BasicAuth limitation). Use `--to` with the exact URI for remote adds.

### 6. Relations (Links Between Resources)

```sh
# Link resources
ov link viking://resources/projects/homelab/README.md \
  viking://resources/projects/homelab/CLAUDE.md \
  --reason "related documentation"

# Unlink
ov unlink viking://resources/projects/homelab/README.md \
  viking://resources/projects/homelab/CLAUDE.md

# List relations of a resource
ov relations viking://resources/projects/homelab/README.md
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

# Add message to existing session
ov session add-message <session-id> "user message here"

# Commit session (archive + extract memories)
ov session commit <session-id>

# Delete session
ov session delete <session-id>
```

The session workflow for agents:
1. `ov session new` → gets session ID
2. `ov session add-message <id> "..."` → add messages progressively
3. `ov session commit <id>` → finalize, extract memories
4. `ov search "..." --session-id <id>` → context-aware search using session context

### 9. Reindexing

If content changes or summaries are stale:

```sh
# Regenerate .abstract.md and .overview.md
ov reindex viking://resources/projects/homelab/README.md

# Force regenerate even if summaries exist
ov reindex viking://resources/projects/homelab/README.md -r

# Wait for reindex to complete
ov reindex viking://resources/projects/homelab/README.md --wait
```

### 10. Admin (Multi-Tenant)

```sh
ov admin --help    # Account and user management
ov observer --help # Observer status
ov system crypto   # Cryptographic key management
```

## Agent Workflows

### Discover what's in the system

```sh
ov health                      # Is it up?
ov status                      # What's the queue/DB state?
ov ls -r                       # Full directory tree
ov stat viking://resources     # Metadata
```

### Find relevant knowledge

```sh
# Quick — pattern match a known term
ov grep "ollama" -i

# Semantic — find conceptually similar
ov find "GPU inference setup"

# Focused — scope to a subtree
ov find "GPU inference" -u viking://resources/projects/homelab
```

### Read content efficiently

```sh
# L0 skim → L2 read only if relevant
ov abstract viking://resources/projects/homelab/CLAUDE.md
# [if abstract looks useful:]
ov read viking://resources/projects/homelab/CLAUDE.md
```

### Add knowledge

```sh
# Single memory
ov add-memory "The homelab has 3 nodes: timmy (AMD), manu (NVIDIA), wemby (NVIDIA)"

# File import
ov add-resource ./docs/architecture.md \
  --to viking://resources/projects/homelab/docs/architecture.md \
  --wait
```

### Session-based research

```sh
SESSION=$(ov session new -o json | grep -oP '"id":"[^"]+"' | cut -d'"' -f4)
ov session add-message "$SESSION" "Looking into GPU utilization patterns"
# ... do searches, read content ...
ov session commit "$SESSION"
```

## Tips & Known Issues

- **`ov export` over remote**: API returns `{"ok":true}` but no file on disk — server-side only.
- **`ov add-resource --parent`**: May fail over remote connection. Use `--to` with exact URI instead.
- **Async processing**: After adding content, check `ov status` → queue to confirm embedding completed.
- **Default limits**: `ls` caps at 256 nodes, `find`/`search` at 10 results. Adjust with `-n`/`--node-limit`.
- **L0/L1 on directories**: `abstract`/`overview` only work on file URIs, not directories.
- **Output format**: `-o json` for programmatic consumption, `-c` for compact representation.
- **Config file**: Override server URL and API key with `ov config` or env vars (check `ov config --help`).
