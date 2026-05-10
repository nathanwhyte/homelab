# OpenViking Wipe/Resync Preflight Gaps

Date: 2026-05-10

This captures the gaps found before another destructive OpenViking wipe and
Compendium resync. It is intentionally a preflight note, not an apply plan.

## Current deployment state

- The canonical repo deployment is moving toward one beefy `openviking`
  Deployment pinned to `timmy`.
- `openviking-standalone-config` uses local VectorDB plus local AGFS:
  - VectorDB: local on the `openviking-data` PVC.
  - AGFS: local on the `openviking-data` PVC.
  - VLM: local ROCm llama.cpp service on `timmy`.
- `timmy` has about 31Gi allocatable memory. Do not run the old Ollama ROCm
  deployment and the new ROCm llama.cpp backend at the same time; both contend
  for the 9070 XT path and model runtime memory.
- `openviking-config` remains the S3/Garage-backed config for fallback or future
  distributed experiments, but it is no longer the intended single-worker path.

## Gaps to resolve or accept before wipe/resync

### 1. Storage mode is not explicit in the runbook

The previous deploy path used S3-backed AGFS. If that path is used and the wipe
only clears the `openviking-data` PVC, object content in Garage can still
remain. That can leave the new VectorDB/index state out of sync with old AGFS
objects.

Before wiping, choose one mode:

- Local AGFS: wipe the PVC once and resync.
- S3 AGFS: wipe the PVC state and the intended Garage bucket/prefix.

### 2. Sync defaults include active/open items

`viking/tools/compendium-sync.py` treats missing `ov_mode` as `pointer`, so a
bare sync includes active, open, proposed, and in-progress entries unless
`--exclude-active` is passed.

The sync tool now defaults to excluding active work items. Use
`--include-active` only when the goal is to index all non-`ov_mode: none`
entries.

### 3. Validator and sync policy disagree by default

`viking/tools/compendium-ov-check.py` encodes "active items not in OV" as a
consistency check, but `compendium-sync.py sync` can upload those active items
by default.

Either run sync with `--exclude-active`, or update the tools so the default sync
selection matches the validator policy.

### 4. External health checks may be misleading

The public ingress uses Traefik BasicAuth plus OpenViking API-key auth. The
sync script health check only sends `X-API-Key`, so health checks through
`https://context.nathanwhyte.dev` can fail even when OpenViking is healthy.

For a resync, prefer a port-forwarded or internal URL, or extend the script to
support BasicAuth headers.

### 5. Offset-based resume is fragile

The sync script supports `--offset`, but the offset is positional over the
current sorted selection. If local files change between attempts, a resumed run
can skip or repeat entries.

For a destructive resync, freeze the vault during the run or save the generated
plan and sync exactly that list.

## Local AGFS vs S3 AGFS

OpenViking keeps two broad classes of state:

- VectorDB/index state: embeddings, abstracts, queue/runtime metadata. In this
  deployment, that lives on the `openviking-data` PVC.
- AGFS content state: the resource file tree and uploaded resource payloads.
  This can live either on the same local filesystem or in S3-compatible object
  storage.

### Local AGFS

Local AGFS stores resource content under the OpenViking workspace filesystem,
which means the `openviking-data` PVC is the source of truth for both index
state and resource content.

Pros:

- One storage system to wipe, snapshot, and reason about.
- Avoids Garage/S3 credential, bucket, and object consistency problems.
- Fits the current single-replica OpenViking deployment.

Cons:

- Tied to one ReadWriteOnce PVC and one active OpenViking instance.
- Scaling to multiple writers/workers is harder because all instances need
  coherent shared file access.
- PVC loss means resource content loss unless Longhorn snapshots/backups are
  configured.

### S3 AGFS

S3 AGFS stores resource content in Garage while the VectorDB/index state still
lives locally on the OpenViking PVC.

Pros:

- Resource payloads are decoupled from the OpenViking pod filesystem.
- Better fit for multi-worker or future distributed layouts.
- Garage can replicate object data independently from the OpenViking PVC.

Cons:

- Wipes must account for both PVC state and Garage bucket state.
- Misconfigured or rotated S3 credentials can break resource reads even when
  OpenViking starts.
- Index state and object state can drift if only one side is cleaned or
  restored.

## Practical recommendation for the next run

For the next wipe/resync, use the local AGFS path:

1. Apply the standalone config and single-worker Deployment pinned to `timmy`.
2. Restart OpenViking.
3. Keep the old Ollama deployment scaled to zero while using the ROCm llama.cpp
   backend.
4. Wipe the `openviking-data` PVC once.
5. Resync with the default active-item exclusion.

In either path, start with a small waited batch before the full run:

```bash
python3 viking/tools/compendium-sync.py sync --limit 5
```
