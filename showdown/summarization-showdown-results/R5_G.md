# Round 5 — Config G (Qwen3-8B OPTIMIZED on manu/1080)
**Settings:** ctx=4096, q4_0 KV, parallel=8, flash-attn, batch=2048
**Time:** 16s | **Tokens out:** 493 | **Gen tok/s:** 32.6

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
- **Con**: requires creating R2 bucket, generating API keys, configuring secret

### Option B: NFS to local NAS
- **Cost**: $0 (existing Synology NAS)
- **Simple setup** - NFS backup target
- **Cons**: 
  - NAS in same room → no disaster recovery
  - NAS is old, disk health unknown

### Option C: Restic to Backblaze B2
- **Cost**: $0.006/GB/mo storage, $0.01/GB egress
- **Velero + Restic** for full cluster backup (not just volumes)
- **Cons**: 
  - More complex setup
  - Separate tooling from Longhorn
  - Restore requires Velero

## Decision
- **Primary**: Option A (R2) for off-site disaster recovery at near-zero cost
- **Secondary**: Option B (NFS) for fast local restores without egress latency
- **Longhorn native support** - no extra tooling required
- **Schedule**: daily incremental, weekly full, 30-day retention

## Action Items
- [ ] Create R2 bucket "homelab-longhorn-backup"
- [ ] Generate R2 API credentials with write access
- [ ] Create K8s secret with S3 endpoint, key, secret
- [ ] Configure Longhorn backup target via UI or settings
- [ ] Test backup + restore cycle on a non-critical volume
- [ ] Set up recurring backup schedule
- [ ] Configure NFS backup target as secondary
```
