# Longhorn volume inventory

The human-readable twin of [`replica-policy.yaml`](replica-policy.yaml)
(IMPR-1478). The policy file is the authority for replica counts, selectors and
backup posture, and `uv run longhorn/scripts/replica-policy.py --check` compares
it with the cluster; this page adds the size, current placement and the reason a
detached volume is kept. Read live 2026-11-08, Longhorn 1.12.1.

Backup posture: **daily** is the `backup-daily` RecurringJob (Phase 4, not yet
live), **manual** is out of every RecurringJob and backed up by hand on request,
**none** is never backed up by Longhorn.

## Attached

| Namespace/PVC | Class | Replicas | Nodes | Used | Backup |
| --- | --- | --- | --- | --- | --- |
| `coach/postgres-data` | longhorn-db | 2 | manu, wemby | <1 GiB | daily |
| `compendium/compendium-sync-state-v2` | longhorn-compendium-sync | 2 | manu, timmy | <1 GiB | daily |
| `copyparty/copyparty-files` | longhorn-hdd-2x | 2 | manu, wemby | 162 GiB | daily |
| `equal-risk/pgdata-postgres-0` | longhorn-db | 3 | manu, timmy, wemby | <1 GiB | daily |
| `garage/data-garage-0` | longhorn-garage-data | 1 | manu | 4 GiB | daily |
| `garage/data-garage-1` | longhorn-garage-data | 1 | timmy | 4 GiB | daily |
| `garage/data-garage-2` | longhorn-garage-data | 1 | manu | 4 GiB | daily |
| `garage/meta-garage-0` | longhorn-nvme | 2 | timmy, wemby | 2 GiB | daily |
| `garage/meta-garage-1` | longhorn-nvme | 2 | timmy, wemby | 3 GiB | daily |
| `garage/meta-garage-2` | longhorn-nvme | 2 | timmy, wemby | 3 GiB | daily |
| `grafana/alertmanager-prom-alertmanager-db-…-0` | longhorn-observability | 1 | timmy | <1 GiB | manual |
| `grafana/grafana-storage` | longhorn-nvme | 2 | manu, timmy | <1 GiB | daily |
| `grafana/prometheus-prom-prometheus-db-…-0` | longhorn-observability | 1 | timmy | 23 GiB | manual |
| `grafana/storage-loki-0` | longhorn-observability | 1 | timmy | 9 GiB | manual |
| `harbor/data-harbor-redis-0` | longhorn-harbor | 2 | manu, wemby | <1 GiB | daily |
| `harbor/data-harbor-trivy-0` | longhorn-harbor | 2 | manu, wemby | <1 GiB | daily |
| `harbor/database-data-harbor-database-0` | longhorn-harbor | 2 | manu, wemby | <1 GiB | daily |
| `harbor/harbor-jobservice-rwo` | longhorn-harbor | 2 | manu, wemby | <1 GiB | daily |
| `harbor/harbor-registry-rwo` | longhorn-harbor | 2 | manu, wemby | 7 GiB | daily |
| `llama/cloud-llm-counter-data` | longhorn | 2 | manu, timmy | <1 GiB | daily |
| `omnipendium/omnipendium-db-data` | longhorn-omnipendium | 2 | manu, timmy | <1 GiB | daily |
| `openwebui/open-webui` | longhorn-ssd | 2 | manu, timmy | 1 GiB | daily |
| `viking/embedder-cuda-model-cache` | longhorn-ssd | 1 | manu | 5 GiB | none |
| `viking/litellm-redis-data` | longhorn-ssd | 1 | manu | <1 GiB | none |
| `viking/openviking-data` | longhorn-ssd | 2 | manu, timmy | <1 GiB | daily |
| `viking/openviking-test-data` | longhorn-ssd | 1 | manu | <1 GiB | none |
| `viking/ov-vectordb-data` | longhorn-ssd | 2 | manu, timmy | 14 GiB | daily |
| `viking/ov-vectordb-test-data` | longhorn-ssd | 1 | manu | <1 GiB | none |
| `yt-dlp/media` | longhorn-hdd-wemby | 1 | wemby | 138 GiB | none (own weekly rclone to R2) |

Large volumes keep one Longhorn replica and take their off-node copy from R2
(decision 2026-11-08): a second replica streams the whole volume across the LAN
on every rebuild.

## Detached, kept

Every detached volume must have a row here (IMPR-1478 acceptance criterion).

| Namespace/PVC | Used | Owner | Why it is kept | Review |
| --- | --- | --- | --- | --- |
| `viking/embedder-qwen-model-cache` | 4 GiB | `embedder-qwen` Deployment (0 replicas) | ROCm rollback embedder on timmy | 2026-12, or when `viking/manifests` drops the Deployment |
| `viking/llama-cuda-model-cache` | 9 GiB | `llamacpp-cuda-ov` Deployment (0 replicas) | still scaled by `viking/tools/ov-vlm.sh`; capacity-auto-resolver's allowlist names it | with the Deployment |
| `viking/embedder-eg2-model-cache` | <1 GiB | `embedder-eg2-cuda` Deployment (0 replicas) | TASK-1218 probe | with the Deployment |
| `compendium/compendium-sync-state` | <1 GiB | compendium sync | legacy v1 state; `compendium/state-migration.md` keeps it until one planned node-failover test | after that test |
| `glossary/postgres-data-postgres-0` | <1 GiB | glossary (Deployment and Postgres StatefulSet at 0) | the glossary database itself | when glossary is retired or restarted |

## Removed 2026-11-08 (IMPR-1478 Phase 2)

| Object | Final R2 backup | Note |
| --- | --- | --- |
| `equal-risk/postgres-data-postgres-0` (RWX) | `impr1478-final-equal-risk-rwx` | replaced by `pgdata-postgres-0` on 2026-10-28 (IMPR-1440) |
| `grafana/grafana-storage-rwx` (RWX) | `impr1478-final-grafana-storage-rwx` | replaced by `grafana-storage` on 2026-10-28 (IMPR-1440) |
| `viking/wemby-model-cache` | `impr1478-final-wemby-model-cache` | `embedder-llamacpp` deleted 2026-07-04 |
| `llama/llama-model-cache` (125 GiB, 101 used) | none, by decision | unmounted model cache; its backup attempt saturated the LAN and disturbed Garage, so it was deleted without one. `llama/pvc.yaml` removed |
| RecurringJob `syncthing-data-snap-daily` | — | its PVC was deleted 2026-07-25 |
| Orphan `orphan-932684c7…` | — | engine-instance orphan on wemby |
| Replica `pvc-53e7e2ef…-r-dfc15f66` | — | failed leftover on `coach/postgres-data` (timmy) |

Final backups are restorable from the Longhorn UI's Backup page, or through a
StorageClass whose `fromBackup` parameter is the backup's URL, for as long as R2
holds them; manual backups have no retention.
