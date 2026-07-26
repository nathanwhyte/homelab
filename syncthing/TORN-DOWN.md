# Torn down 2026-07-25

The `syncthing` namespace was deleted — both Services, the Deployment, and the
`syncthing-data` PVC. The vault owner confirmed Syncthing is no longer used; the
compendium and Obsidian vault sync through **git**, which is what made the
cluster-side anchor redundant.

Manifests retained for reference, per the [`mem0`](../mem0/TORN-DOWN.md) and
[`hermes`](../hermes/RETIRED.md) precedent.

## What was removed

| Resource | Detail |
| --- | --- |
| `deployment.apps/syncthing` | Already scaled `0/0` since 2026-06-29; never scaled back up |
| `service/syncthing-gui` | ClusterIP `10.43.59.212`, 80→8384 |
| `service/syncthing-sync` | **NodePort `32200`** (TCP/UDP) + `32127/UDP` — was exposed on every node |
| `pvc/syncthing-data` | **50 GiB** on `longhorn-nvme`, Bound 30d. PV reclaim policy was `Delete`, so the Longhorn volume was reclaimed with it |

**No backup was taken, deliberately.** Unlike the mem0 teardown — where a
`pg_dumpall` was archived because the data existed nowhere else — this volume
held copies of the Obsidian/compendium vault, which is fully version-controlled
in `nathanwhyte/compendium`. There was nothing on it that git does not already
hold.

## Why it went

The deployment had been dormant for ~4 weeks: scaled to zero on 2026-06-29
because the cluster-side anchor caused duplication in the compendium vault, with
peers syncing directly over Tailscale instead. On 2026-07-25 the owner confirmed
Syncthing is out entirely, so the dormant namespace was removed rather than left
holding an open NodePort and 50 GiB of NVMe.

Closing `32200` also removes one of the tailnet-reachable ports called out as a
guest-access concern in the compendium's `IDEA-1053` / `IDEA-1079` analysis.

## Compendium cross-references

- `PROJ-1011` (personal notes / task memory OS) — **cancelled** 2026-07-25; Syncthing was its goal 3, and `TASK-1086` (Syncthing deployment) was cancelled having never delivered
- `BUG-1026` (Syncthing lost `.obsidian` themes/snippets, missing `.stfolder` marker) — closed **wontfix**: the defect was never fixed, the exposure left with the tool

## Restoring

Re-apply `kustomization.yaml` to recreate the namespace, Services, Deployment,
and a fresh empty PVC. Device IDs and folder pairings are **not** in these
manifests — they lived in the deleted volume's config and would need to be set up
again from scratch.
