# OpenViking Indexing Performance Tuning

**Date**: 2026-05-07
**Scope**: Config-only changes to improve indexing throughput. No architecture changes.

## Problem

Indexing speed is bottlenecked by conservative concurrency limits that underutilize backend capacity:

- Workers cap VLM at 2 concurrent, but the LLM has 4 parallel slots (benchmarked at 82.5 aggregate tok/s)
- Workers cap embedding at 1 concurrent, but the embedder has 8 parallel slots
- OV server runs a single worker process
- Worker memory limits are tight for concurrent VLM response handling

## Design

### Concurrency Tuning

| Setting | Before | After | Rationale |
|---------|--------|-------|-----------|
| Worker `vlm.max_concurrent` | 2 | 4 | GTX 1080 handles 4 concurrent at 20.6 tok/s each; workers fill slots during bulk indexing |
| Worker `embedding.max_concurrent` | 1 | 3 | Embedder is fast (~50ms/call); slight oversubscription (3x3=9 vs 8 slots) is harmless |
| `server.workers` | 1 | 2 | Better concurrent API request handling for parallel indexing |

### Batch & Memory Tuning

| Setting | Before | After | Rationale |
|---------|--------|-------|-----------|
| `embedding.batch_size` | 64 | 128 | Fewer HTTP round-trips; embedder handles it easily with 8 parallel / 4096 batch |
| Worker memory limit | 3Gi | 4Gi | Headroom for 4 in-flight VLM responses + AST parsing + embedding batches |

### What We're NOT Changing

- **LLM parameters** (4 slots, 32K ctx, q4_0 KV, batch 2048/512, flash-attn) — already optimized
- **Embedder parameters** (CPU-only, 8 parallel, 16K ctx, yarn, mlock) — already optimized
- **Merge interval** (60s) — faster merging doesn't help indexing, just increases S3 traffic
- **Worker replica count** (3) — more workers would stress manu (83% memory)
- **GPU power limit** (220W) — 238W was tested, no improvement

## Changes

| File | Setting | Before | After |
|------|---------|--------|-------|
| `viking/ov-worker-statefulset.yaml` | `vlm.max_concurrent` | 2 | 4 |
| `viking/ov-worker-statefulset.yaml` | `embedding.max_concurrent` | 1 | 3 |
| `viking/ov-worker-statefulset.yaml` | memory limit | 3Gi | 4Gi |
| `viking/openviking-configmap.yaml` | `embedding.batch_size` | 64 | 128 |
| `viking/openviking-configmap.yaml` | `server.workers` | 1 | 2 |

5 config changes across 2 files. No new deployments, no architecture changes.

## Validation

After applying:
1. `ov add_text` a batch of 5-10 resources and time total completion
2. Check VLM slot utilization during indexing (`/metrics` endpoint on llamacpp)
3. Verify no OOM kills on worker pods (`kubectl describe pod ov-worker-X`)
4. Confirm embedder handles concurrent load without 503s