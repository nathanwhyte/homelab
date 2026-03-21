# Round 5 — Config E (Qwen3-8B on manu/1080)
**Time:** 18s | **Tokens out:** 559 | **Gen tok/s:** 32.8

## Output
```markdown
# Longhorn Backup Architecture Decision Summary

## Current State
- **Longhorn version**: v1.8.1 running on K3s cluster (3 nodes: manu, wemby, patty)
- **PVC configuration**: All PVCs use local-path or Longhorn with 1 replica (no redundancy)
- **Backup status**: No off-cluster backups configured
- **Storage usage**: ~47GB across 12 volumes

## Options Evaluated

### Option A: S3 to Cloudflare R2
- **Cost**: $0 for storage (10GB free), $0.015/GB after. Egress free.
- **Features**:
  - Longhorn native S3 backup target
  - Minimal configuration required
  - Encryption at rest via Longhorn (AES-256)
  - Incremental backups supported
- **Con**: Requires creating R2 bucket, generating API keys, and configuring secret

### Option B: NFS to local NAS
- **Cost**: $0 (existing Synology NAS)
- **Features**:
  - Simple NFS backup target
- **Cons**:
  - NAS is in same room → no disaster recovery
  - NAS is old, disk health unknown

### Option C: Restic to Backblaze B2
- **Cost**: $0.006/GB/mo storage, $0.01/GB egress
- **Features**:
  - Velero + Restic for full cluster backup (not just volumes)
- **Cons**:
  - More complex setup
  - Separate tooling from Longhorn
  - Restore requires Velero

## Decision
- **Primary**: Option A (R2) for off-site disaster recovery at near-zero cost
- **Secondary**: Option B (NFS) as local fast-restore cache
- **Reasoning**:
  - R2 provides off-cluster, secure storage with minimal overhead
  - NFS offers fast local restores without egress latency
  - Longhorn natively supports both targets → no extra tooling needed
- **Schedule**: Daily incremental backups, weekly full backups, 30-day retention

## Action Items
- [ ] Create R2 bucket: `homelab-longhorn-backup`
- [ ] Generate R2 API credentials with write access
- [ ] Create K8s secret with S3 endpoint, key, and secret
- [ ] Configure Longhorn backup target via UI or settings
- [ ] Test backup + restore cycle on a non-critical volume
- [ ] Set up recurring backup schedule
- [ ] Configure NFS backup target as secondary
```
