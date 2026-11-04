# Garage

S3-compatible object storage running on the K3s cluster. Based on [Garage](https://garagehq.deuxfleurs.fr/).

## At a glance

| Property | Value |
|---|---|
| Endpoint | `http://garage.garage.svc.cluster.local:3900`, **cluster-only** (no ingress or tunnel route since 2026-11-02, IMPR-1457); S3 API at `:3900`, web endpoint at `:3902` (no route), admin API at `:3903` on the `garage-metrics` service; RPC binds `:3901` pod to pod, with no Service |
| Namespace | `garage` |
| Deploy | Helm via `garage/deploy-garage.sh` using `garage/garage-values.yaml`; chart from a gitignored clone at `garage/garage/`, pinned to tag `v2.5.0` (chart 0.10.4) by `GARAGE_CHART_REF` |
| Storage | 3× data (50Gi, `longhorn-garage-data`) + 3× meta (5Gi, `longhorn-nvme`) Longhorn volumes |
| Layout | 3-zone replication (one zone per node), `replication_factor = 3` |
| Key consumers | OpenViking AGFS (`agfs.backend: s3` → bucket `openviking-agfs`), the k3s datastore backup CronJob (bucket `k3s-backups`). Buckets `credit-coach-dev`/`-prod` also exist; their current consumers are unconfirmed. The unused `filestore` bucket was deleted 2026-11-02. The equal-risk Postgres backup goes to R2, not Garage |

The admin API token lives in the `garage-admin-token` Secret (key `admin-token`), injected as `GARAGE_ADMIN_TOKEN` through `environment` in `garage-values.yaml`; `garage.toml` carries none. To rotate it: `kubectl -n garage create secret generic garage-admin-token --from-literal=admin-token="$(openssl rand -base64 32)" --dry-run=client -o yaml | kubectl apply -f -`, then restart the pods one at a time (each restart can still fail an S3 write: a terminating pod keeps receiving requests until its endpoint removal reaches kube-proxy, and the chart has no `preStop` setting to bridge that window; the `/health` readiness probe only keeps traffic off pods that have not rejoined). Prometheus scrapes `/metrics` on `:3903` without a token; no `metrics_token` is set.

Buckets and keys are created by hand (`kubectl -n garage exec garage-0 -c garage -- /garage bucket create …`, `key create`, `bucket allow`); the image is distroless, so there is no shell.

## Chart pin and upgrades

`deploy-garage.sh` clones `https://git.deuxfleurs.fr/Deuxfleurs/garage` at
`GARAGE_CHART_REF` when `garage/garage/` is missing, and refuses to deploy or
dry-run when the clone's tag (`git describe --tags --exact-match`) differs; the
message gives the `fetch`/`checkout` fix. `GARAGE_CHART_DIR` overrides the chart
and skips the pin.

A chart version change is refused by default. To upgrade, change
`GARAGE_CHART_REF` and check out that tag in the clone, then:

```bash
garage/deploy-garage.sh --upgrade-chart --dry-run   # server dry run + diff + plan; changes nothing
garage/deploy-garage.sh --upgrade-chart             # hold at partition 3, upgrade, roll 2, 1, 0
```

The roll waits, per ordinal, for a new pod UID, Ready, and `/garage status` on
every pod showing all 3 nodes under HEALTHY NODES. On a timeout it stops with
the partition where it is and prints the command to resume. Design notes:
`scripts/helm-deploy.md`.

## Manager

`garage/manager/` contains a custom Python manager pod (`garage-manager.py`), a boto3 S3 client that lists, uploads, downloads and deletes objects. It does not manage buckets or keys. Its Deployment is not running as of 2026-11-02.

- Deploy: `garage/manager/garage-manager.yaml`
- Config: `garage/manager/garage-manager-config.yaml`

## Cloudflare tunnel

The former `garage/cloudflared.yaml` connector is retired and is not applied.
Garage's active S3 endpoint is cluster-internal at `garage.garage.svc:3900`;
Longhorn's external backup path uses its R2 gateway. The retained file is only a
historical reference and must not be used to recreate a tunnel.
