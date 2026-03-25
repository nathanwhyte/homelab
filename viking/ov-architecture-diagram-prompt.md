# OpenViking Stack Architecture Diagram — Instructions for Claude

Create a detailed architecture diagram of the OpenViking (OV) distributed knowledge base stack running on a K3s homelab cluster. Use a clean, professional style with clear flow arrows and grouped components.

## Components

### External Clients (top of diagram)
- **Claude Code MCP** — primary client, connects via MCP tools
- **Other MCP Clients** — any tool using the OpenViking MCP server
- These connect to the **ov-coordinator** as their single entry point

### ov-coordinator (central hub)
- FastAPI proxy on port 1933
- Single deployment, runs on wemby
- Routes ALL client requests using these strategies:
  - **Hash-route writes**: `POST /resources` hashed by target URI (MD5) to a specific worker
  - **Broadcast writes**: `mkdir` and `DELETE` sent to ALL workers
  - **Temp uploads**: round-robin or hash-routed by `X-OV-Route-To` header
  - **Fan-out reads**: `search`, `find`, `grep` sent to all workers, results merged by score and deduped by URI
  - **Single-worker reads**: `ls`, `tree`, `sessions` sent to any one healthy worker (shared S3 backend)
  - **Merged reads (optional)**: if the merged instance is healthy and fresh, reads go there instead of fan-out
- Background health loop polls all workers + merge service every 30s

### ov-worker StatefulSet (5 replicas: ov-worker-0 through ov-worker-4)
- Each runs OpenViking v0.2.9 on port 1933
- Spread across nodes: wemby (0, 2), timmy (1), manu (3, 4)
- Each has:
  - **Local VectorDB** (on PVC, 10Gi longhorn-ssd) for embedding indexes
  - **Shared AGFS** on Garage S3 (shared object store across all workers)
  - **Semantic processor** — generates abstracts/overviews via VLM, triggers embeddings
  - **Path lock system** — prevents concurrent writes to same URI tree
- Workers connect outbound to:
  - **embedder-llamacpp** for text embeddings
  - **llamacpp-rocm** for VLM abstract generation

### openviking (merged instance)
- Single deployment on wemby, OpenViking v0.2.9, port 1933
- Acts as the "merged" read replica — contains a unified copy of all worker data
- Has its own PVC and VectorDB
- The coordinator routes reads here when merge service reports it is healthy + not stale

### ov-merge (background sync service)
- Python FastAPI service on port 8080
- Runs every 300 seconds (5 min cycle):
  1. Lists all URIs across all 5 workers (full `fs/tree` scan)
  2. Lists URIs on merged instance
  3. Computes delta (missing URIs)
  4. Reindexes missing URIs on merged instance
- Exposes `/merge-status` endpoint that coordinator polls
- Reports: healthy/stale/in_progress/uri_count/error_count

### LLM Services (bottom-right group, on timmy node)
- **llamacpp-rocm** — Qwen3-8B on AMD RX 9070 XT GPU
  - 8 parallel slots, 8192 ctx per slot, q4_0 KV cache
  - Service: `llamacpp-rocm-llm.viking.svc:80`
  - Used by workers for VLM abstract generation (max 2 concurrent per worker)
- **embedder-llamacpp** — nomic-embed-text-v1.5, CPU-only, f16
  - 768-dimension embeddings, batch size 64
  - Service: `embedder-llamacpp.viking.svc:8080`
  - Used by workers for semantic embeddings (max 1 concurrent per worker)

### Storage (bottom-left group)
- **Garage S3** — `garage.garage.svc:3900`
  - Bucket: `openviking-agfs`
  - Shared filesystem backend (AGFS) for all workers + merged instance
  - All workers read/write the same S3 objects
- **Longhorn PVCs** — each worker has a 10Gi `longhorn-ssd` PVC
  - Stores local VectorDB indexes, temp files, and processing state
  - 2 replicas per PVC for redundancy

## Request Flow Arrows

### Write Flow (e.g., `viking_add_text`)
```
Client -> Coordinator -> (temp_upload to round-robin worker)
                      -> (hash target URI -> specific worker)
                      -> Worker writes to Garage S3 (AGFS)
                      -> Worker triggers semantic processor
                      -> Semantic processor -> llamacpp-rocm (VLM abstract)
                      -> Semantic processor -> embedder-llamacpp (embeddings)
                      -> Embeddings stored in local VectorDB
```

### Read Flow (e.g., `viking_search`)
```
Client -> Coordinator -> checks if merged instance healthy+fresh
  If yes: -> Merged Instance -> returns results
  If no:  -> Fan-out to ALL workers in parallel
        -> Each worker searches local VectorDB
        -> Coordinator merges results by score, dedupes by URI
        -> Returns unified result to client
```

### Merge Sync Flow (background)
```
ov-merge -> (every 300s) lists URIs on each worker via fs/tree
         -> lists URIs on merged instance
         -> computes delta
         -> reindexes missing URIs on merged instance
         -> merged instance fetches content from S3 + regenerates embeddings
Coordinator -> (every 30s) polls ov-merge /merge-status
            -> updates merged_healthy / merged_stale flags
```

### Health Check Flow
```
Kubernetes -> readiness probe GET /health on each worker (every 10s, 120s timeout)
Kubernetes -> readiness probe GET /ready on openviking (every 10s, 120s timeout)
Coordinator -> GET /health on each worker (every 30s, 120s timeout)
Coordinator -> GET /merge-status on ov-merge (every 30s)
```

## Node Layout
Show which K8s node each component runs on:

| Node | Components |
|------|-----------|
| wemby | ov-coordinator, openviking (merged), ov-merge, ov-worker-0, ov-worker-2 |
| timmy | llamacpp-rocm (GPU), embedder-llamacpp, ov-worker-1 |
| manu | ov-worker-3, ov-worker-4 |

## Visual Style
- Group components by node with labeled borders
- Use different colors for: clients (blue), coordinator (orange), workers (green), LLM services (purple), storage (gray)
- Arrow styles: solid for request flow, dashed for background/health, dotted for merge sync
- Show port numbers on connections
- Include a legend for arrow types and routing strategies
