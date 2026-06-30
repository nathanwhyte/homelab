# syncthing

⬚ **STATUS: SCALED DOWN** — The cluster-side anchor was scaled to 0 replicas on 2026-06-29 due to duplication issues it caused in the compendium vault. Peers are syncing directly over Tailscale without the cluster anchor. To re-enable: `kubectl -n syncthing scale deployment syncthing --replicas=1`.

Always-on Syncthing peer for vault sync — the cluster-side anchor for an Obsidian/compendium device mesh that includes MacBook(s), iPad, and phone. Design rationale lives in compendium **IDEA-1024** (homelab sync architecture) and **IDEA-1046** (personal notes/task/memory OS).

## What this is

A single Pod running `syncthing/syncthing`, backed by a 50 Gi `longhorn-nvme` PVC, with daily Longhorn snapshots. It does not host any compute or business logic — its job is to be online so peers can converge against it.

**Current status: scaled down (replicas=0) as of 2026-06-29.** The cluster anchor was causing duplication issues in the compendium vault; peers are syncing directly over Tailscale without it.

| Piece | Detail |
| --- | --- |
| Image | `docker.io/syncthing/syncthing:1.27.12` (TODO: digest-pin before first deploy) |
| Node | `timmy` (pinned via `nodeSelector`). Timmy has the largest NVMe disk (2 TB) and is where `longhorn-nvme` placed the replica; the pin colocates the engine with the replica. |
| Storage | PVC `syncthing-data`, 50 Gi `longhorn-nvme` (RWO, **single-replica NVMe**). Single replica is acceptable here because every paired peer (MacBook, iPad, phone) also holds the vault — the cluster anchor is one of N copies. |
| Snapshots | `syncthing-data-snap-daily` Longhorn RecurringJob, 04:30 daily, 7-day retention |
| Identity | Syncthing device ID generated on first start, persists in `/var/syncthing/config/cert.pem` |

## Services

| Service | Type | Port | Reachability |
| --- | --- | --- | --- |
| `syncthing-gui` | ClusterIP | 80 → 8384 | In-cluster only (port-forward for setup) |
| `syncthing-sync` | NodePort | 22000 TCP/UDP → `32200`, 21027 UDP → `32127` | LAN (`192.168.1.19:32200`), Tailscale (`100.95.215.105:32200` for the direct path to timmy where the pod runs; other tailnet IPs work via cluster networking) |

GUI is intentionally ClusterIP-only on first deploy because there is no password until the user sets one through the web UI. Once the GUI password is configured, follow-up work can add a Tailscale NodePort or Cloudflare-Access-gated Ingress for off-cluster admin access.

## Device identity model

Peers are addressed and reasoned about by their **Tailscale hostname** (`timmy`, `wemby`, `manu`, MacBook, iPad, phone) plus the K3s node name where applicable. Syncthing still uses its own cryptographic device IDs under the hood for pairing — that's a one-time setup detail. Day-to-day routing, addressing, and "which devices are in the mesh" reasoning all happens in the Tailscale/K3s namespace.

iCloud Drive is intentionally not in this design (work MacBook cannot sync via iCloud).

## Initial bring-up

The manifests do not get applied automatically — `kubectl apply` is a deliberate, user-driven step. See `~/code/homelab/AGENTS.md`.

```bash
# from the worktree, dry-run first
kubectl apply -k syncthing/ --dry-run=server

# real apply
kubectl apply -k syncthing/

# wait for the pod
kubectl -n syncthing rollout status deploy/syncthing

# port-forward the GUI for first-time setup
kubectl -n syncthing port-forward svc/syncthing-gui 8384:80
# → open http://localhost:8384, set a GUI user + password under
#   Actions → Settings → GUI, and grab the cluster device ID from
#   Actions → Show ID
```

## Pairing peers

For each peer device (MacBook, iPad, phone):

1. Install Syncthing on the peer:
   - macOS: `brew install syncthing` (or use the GUI app)
   - iOS / iPadOS: **Möbius Sync** (third-party Syncthing client) or **VaultSync** (syncs straight into the Obsidian iOS sandbox)
2. From the cluster Syncthing GUI: **Add Remote Device** → paste the peer's device ID.
3. From the peer: **Add Remote Device** → paste the cluster device ID (the one from "Show ID").
4. On both sides, accept the pairing request when it appears.
5. Add the vault folder on the peer, share it with the cluster device. The cluster will offer to accept it; choose the destination path under `/var/syncthing/<folder>/`.

For the vault folders, configure **File Versioning** in the GUI (per folder, Advanced tab). `Staggered` versioning is a sensible default — keeps recent edits frequently and older edits less so. Versioning is the first line of defense against accidental deletes propagating from a peer; the daily Longhorn snapshot is the backstop.

## What this does *not* cover

- **Codebases and config files** — not vault-shaped; use the rsync-over-SSH-via-Tailscale pattern or `git pull` cron, per IDEA-1024.
- **GUI off-cluster access** — deferred until after first setup (need a password set first).
- **Off-cluster backups** — Longhorn snapshots are on-volume; an off-cluster mirror (Longhorn → Garage S3) is a follow-up.

## Related

- `~/code/compendium/ideas/IDEA-1024-other-rsync-cron-for-compendium-and-codebase-sync.md`
- `~/code/compendium/ideas/other/IDEA-1046-other-personal-notes-task-memory-operating-system.md`
- `../copyparty/` — same node, same pattern (PVC + Recreate + Longhorn snapshots)
