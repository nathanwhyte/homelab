# Round 5 — Config A
**Time:** 42s | **Tokens out:** 484

## Output
## Longhorn Backup Architecture Decision

### Current State
- **Longhorn Version**: v1.8.1
- **Cluster**: K3s (3 nodes: manu, wemby, patty)
- **PVC Usage**: ~47GB across 12 volumes
- **Redundancy**: None (local-path or Longhorn with 1 replica)

### Options Evaluated

**Option A: S3 to Cloudflare R2**
- **Cost**:
    - $0 for storage (first 10GB)
    - $0.015/GB after free tier
    - Egress free
- **Pros**:
    - Longhorn native S3 backup target
    - Encryption at rest via Longhorn (AES-256)
    - Incremental backups supported
- **Cons**:
    - Requires creating R2 bucket, generating API keys, configuring secret

**Option B: NFS to local NAS**
- **Cost**: $0 (existing Synology NAS)
- **Pros**:
    - Simple NFS backup target
- **Cons**:
    - No disaster recovery (NAS in same room)
    - Disk health unknown

**Option C: Restic to Backblaze B2**
- **Cost**:
    - $0.006/GB/mo storage
    - $0.01/GB egress
- **Pros**:
    - Velero + Restic for full cluster backup
- **Cons**:
    - More complex setup, separate tooling from Longhorn
    - Restore requires Velero

### Decision
**Primary**: Option A (R2)
- Off-site disaster recovery at near-zero cost
- NFS as local fast-restore cache

**Backup Schedule**:
- Daily incremental
- Weekly full
- 30-day retention

### Action Items
- [ ] Create R2 bucket "homelab-longhorn-backup"
- [ ] Generate R2 API credentials with write access
- [ ] Create K8s secret with S3 endpoint, key, secret
- [ ] Configure Longhorn backup target via UI or settings
- [ ] Test backup + restore cycle on a non-critical volume
- [ ] Set up recurring backup schedule
- [ ] Configure NFS backup target as secondary
