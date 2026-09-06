# Garage

S3-compatible object storage running on the K3s cluster. Based on [Garage](https://garagehq.deuxfleurs.fr/).

## At a glance

| Property | Value |
|---|---|
| Endpoint | `http://garage.garage.svc:3900` (cluster-internal); S3 API at `:3900`, admin API at `:3901` |
| Namespace | `garage` |
| Deploy | Helm via `garage/deploy-garage.sh` using `garage/garage-values.yaml` |
|| Storage | 3× data (100Gi, longhorn-ssd) + 3× meta (1Gi, longhorn-nvme) Longhorn volumes |
| Layout | 3-zone replication (one zone per node) |
| Key consumers | OpenViking AGFS (`agfs.backend: s3` → Garage bucket `openviking-agfs`), Longhorn backup target (R2 gateway) |

## Manager

`garage/manager/` contains a custom Python manager pod (`garage-manager.py`) for bucket and key management via the Garage admin API.

- Deploy: `garage/manager/garage-manager.yaml`
- Config: `garage/manager/garage-manager-config.yaml`

## Cloudflare tunnel

The former `garage/cloudflared.yaml` connector is retired and is not applied.
Garage's active S3 endpoint is cluster-internal at `garage.garage.svc:3900`;
Longhorn's external backup path uses its R2 gateway. The retained file is only a
historical reference and must not be used to recreate a tunnel.
