# Garage

S3-compatible object storage running on the K3s cluster. Based on [Garage](https://garagehq.deuxfleurs.fr/).

## At a glance

| Property | Value |
|---|---|
| Endpoint | `http://garage.garage.svc.cluster.local:3900`, **cluster-only** (no ingress or tunnel route since 2026-11-02, IMPR-1457); S3 API at `:3900`, web endpoint at `:3902` (no route), admin API at `:3903` on the `garage-metrics` service; RPC binds `:3901` pod to pod, with no Service |
| Namespace | `garage` |
| Deploy | Helm via `garage/deploy-garage.sh` using `garage/garage-values.yaml` |
| Storage | 3× data (50Gi, `longhorn-garage-data`) + 3× meta (5Gi, `longhorn-nvme`) Longhorn volumes |
| Layout | 3-zone replication (one zone per node), `replication_factor = 3` |
| Key consumers | OpenViking AGFS (`agfs.backend: s3` → bucket `openviking-agfs`), the k3s datastore backup CronJob (bucket `k3s-backups`). Buckets `credit-coach-dev`/`-prod` and `filestore` also exist; their current consumers are unconfirmed. The equal-risk Postgres backup goes to R2, not Garage |

The admin API token lives in the `garage-admin-token` Secret (key `admin-token`), injected as `GARAGE_ADMIN_TOKEN` through `environment` in `garage-values.yaml`; `garage.toml` carries none. To rotate it: `kubectl -n garage create secret generic garage-admin-token --from-literal=admin-token="$(openssl rand -base64 32)" --dry-run=client -o yaml | kubectl apply -f -`, then restart the pods one at a time (each restart can fail an in-flight S3 write, since there is no readiness probe). Prometheus scrapes `/metrics` on `:3903` without a token; no `metrics_token` is set.

Buckets and keys are created by hand (`kubectl -n garage exec garage-0 -c garage -- /garage bucket create …`, `key create`, `bucket allow`); the image is distroless, so there is no shell.

## Manager

`garage/manager/` contains a custom Python manager pod (`garage-manager.py`), a boto3 S3 client that lists, uploads, downloads and deletes objects. It does not manage buckets or keys. Its Deployment is not running as of 2026-11-02.

- Deploy: `garage/manager/garage-manager.yaml`
- Config: `garage/manager/garage-manager-config.yaml`

## Cloudflare tunnel

The former `garage/cloudflared.yaml` connector is retired and is not applied.
Garage's active S3 endpoint is cluster-internal at `garage.garage.svc:3900`;
Longhorn's external backup path uses its R2 gateway. The retained file is only a
historical reference and must not be used to recreate a tunnel.
