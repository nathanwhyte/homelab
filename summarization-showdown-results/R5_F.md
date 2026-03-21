# Round 5 — Config F (Qwen3-8B OPTIMIZED on manu/1080)
**Settings:** ctx=4096, q4_0 KV, parallel=8, flash-attn, batch=2048
**Time:** 16s | **Tokens out:** 479 | **Gen tok/s:** 32.5

## Output
```markdown
# Longhorn Backup Architecture Decision Summary

## Current State
- Longhorn v1.8.1 running on K3s cluster (3 nodes: manu, wemby, patty)
- All PVCs use local-path or Longhorn with 1 replica (no redundancy)
- No off-cluster backups configured
- Total PVC usage: ~47GB across 12 volumes

## Options Evaluated

### Option A: S3 to Cloudflare R2
- **Cost**: $0 for storage (10GB free), $0.015/GB after. Egress free.
- **Longhorn native support** - minimal config
- **Encryption at rest**: AES-256 via Longhorn
- **Incremental backups supported**
- **Con**: requires R2 bucket, API keys, and secret configuration

### Option B: NFS to local NAS
- **Cost**: $0 (existing Synology NAS)
- **Simple setup** - no egress required
- **Cons**: 
  - Same location - no disaster recovery
  - NAS is old with unknown disk health

### Option C: Restic to Backblaze B2
- **Cost**: $0.006/GB/mo storage, $0.01/GB egress
- **Velero + Restic** for full cluster backup (not just volumes)
- **Cons**: more complex setup; separate tooling from Longhorn

## Decision
- **Primary**: Option A (R2) for off-site disaster recovery at near-zero cost
- **Secondary**: Option B (NFS) for fast local restores without egress latency
- **Longhorn native support** - no extra tooling required
- **Schedule**: Daily incremental, weekly full, 30-day retention

## Action Items
- [ ] Create R2 bucket "homelab-longhorn-backup"
- [ ] Generate R2 API credentials with write access
- [ ] Create K8s secret with S3 endpoint, key, secret
- [ ] Configure Longhorn backup target via UI or settings
- [ ] Test backup + restore cycle on a non-critical volume
- [ ] Set up recurring backup schedule
- [ ] Configure NFS backup target as secondary
```
