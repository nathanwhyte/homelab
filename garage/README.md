# Garage

S3-compatible object storage running on the K3s cluster. Based on [Garage](https://garagehq.deuxfleurs.fr/).

## At a glance

| Property | Value |
|---|---|
| Endpoint | `http://garage.garage.svc:3900` (cluster-internal); S3 API at `:3900`, admin API at `:3901` |
| Namespace | `garage` |
| Deploy | Helm via `garage/deploy-garage.sh` using `garage/garage-values.yaml` |
| Storage | 3× data (100Gi, longhorn-harbor) + 3× meta (1Gi, longhorn-harbor) Longhorn volumes |
| Layout | 3-zone replication (one zone per node) |
| Key consumers | OpenViking AGFS (`agfs.backend: s3` → Garage bucket `openviking-agfs`), Longhorn backup target (R2 gateway) |

## Manager

`garage/manager/` contains a custom Python manager pod (`garage-manager.py`) for bucket and key management via the Garage admin API.

- Deploy: `garage/manager/garage-manager.yaml`
- Config: `garage/manager/garage-manager-config.yaml`

## Cloudflare tunnel

`garage/cloudflared.yaml` — Cloudflare Tunnel configuration for external S3 access (used by Longhorn backup to R2).