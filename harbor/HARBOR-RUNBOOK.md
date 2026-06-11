# Harbor Runbook

Day-2 operations for the Harbor deployment. Assumes the install / upgrade path in `harbor/deploy-harbor.sh` and the values in `harbor/harbor-values.yaml`.

## Health checks

```bash
# All 8 components healthy? (expect 1/1 Running, 0 restarts older than 10m)
kubectl get pods -n harbor

# Health endpoint (no auth)
curl -fsS https://registry.nathanwhyte.dev/api/v2.0/health
# {"status":"healthy"}

# Component-wise pod listing
for c in core portal registry jobservice database redis trivy exporter; do
  printf '%-12s ' "$c"
  kubectl get pods -n harbor -l component=$c -o jsonpath='{.items[0].status.phase}{"  "}{.items[0].spec.containers[0].image}{"\n"}'
done
```

Expected image column: `goharbor/<component>:<v2.15.1>` for app components, `goharbor/harbor-db:v2.14.3` and `goharbor/redis-photon:v2.14.3` for the DB/Redis. If the app components are at any other version, the chart was upgraded without updating `harbor-values.yaml` — see "DB/Redis stuck on a pinned version" below.

## Tail logs

```bash
# Single component
kubectl logs -n harbor -l component=core --tail=100 -f

# Multiple, side-by-side
kubectl logs -n harbor -l component=core --tail=50 -f &
kubectl logs -n harbor -l component=jobservice --tail=50 -f &

# Database (PostgreSQL)
kubectl logs -n harbor -l component=database --tail=100

# Trivy (vulnerability scanner — verbose during DB updates)
kubectl logs -n harbor -l component=trivy --tail=200
```

## TLS / cert-manager

The `harbor-tls` Secret is auto-managed by cert-manager via DNS-01 with Cloudflare. Rotation is automatic; only investigate if a renewal fails.

```bash
# Cert status (expect READY=True, age <60d)
kubectl get certificate -n harbor

# Force re-check (will show current renewal time + errors)
kubectl describe certificate harbor-tls -n harbor

# cert-manager itself (if a renewal is failing)
kubectl logs -n cert-manager -l app=cert-manager --tail=200

# Confirm Cloudflare API token still works
kubectl get secret cloudflare-api-token -n cert-manager 2>/dev/null \
  && echo "token present" \
  || echo "token missing — see ~/code/homelab/cert-manager/"
```

## RWO volume gotcha

All Harbor stateful components use **single replicas + RWO volumes** (Longhorn `longhorn-harbor`, `longhorn-hdd`, `longhorn-ssd`). The Helm `updateStrategy` is `Recreate` — old pods terminate before replacements start, so each component has 2-5 min of downtime during a `helm upgrade`. Plan upgrades accordingly; there is no shared state to migrate.

If a pod is stuck in `Pending` after a node failure, check that the PVC is still bound:

```bash
kubectl get pvc -n harbor
# All 3 should be Bound:
# - harbor-registry-rwo      (50Gi, longhorn-harbor)
# - harbor-jobservice-rwo    (1Gi, longhorn-harbor)
# - harbor-database          (5Gi, longhorn-hdd)
# (Redis and Trivy are chart-managed; PVCs created by Helm.)

# If a PVC is Pending after a node died:
kubectl describe pvc <name> -n harbor   # look at Events
# Longhorn auto-replicates; usually a few minutes to reschedule.
```

The May 2026 RWX→RWO migration that produced the pre-created `harbor-registry-rwo` and `harbor-jobservice-rwo` claims is documented in `harbor/RWO-MIGRATION.md`. The migration manifests in `harbor/rwo-migrators.yaml` are **frozen paper trail** — do not re-apply.

## Probe-timeout patch (chart 1.18.3 AND 1.19.1)

Charts `harbor-1.18.3` and `harbor-1.19.1` both hardcode the `harbor-core` liveness/readiness probe `timeoutSeconds` to `1` (with no values support — verified by reading `helm show values harbor/harbor --version 1.19.1`). After every `helm upgrade`, the timeout reverts and the core pod starts flapping during warm-up.

`harbor/deploy-harbor.sh` re-applies the patch automatically post-upgrade. To verify it's set:

```bash
kubectl get deployment harbor-core -n harbor \
  -o jsonpath='{.spec.template.spec.containers[0].livenessProbe.timeoutSeconds}{" / "}{.spec.template.spec.containers[0].readinessProbe.timeoutSeconds}{"\n"}'
# Expect: 5 / 5
```

To re-apply manually if needed:

```bash
kubectl patch deployment harbor-core -n harbor --type='json' -p='[{"op":"add","path":"/spec/template/spec/containers/0/livenessProbe/timeoutSeconds","value":5},{"op":"add","path":"/spec/template/spec/containers/0/readinessProbe/timeoutSeconds","value":5}]'
```

When the cluster moves to chart >= 1.20.0, re-validate whether the patch is still needed. If upstream fixed it, drop the patch and the post-hook (`--skip-probe-patch` as the escape hatch during the transition).

## Trivy DB

Trivy downloads the vulnerability database on first scan after a restart. ~500 MB, ~5 min on a warm connection. After that, incremental updates are quick.

- First scan after a `helm upgrade` will be slow. Trigger it explicitly so you can predict the window:

  ```bash
  # Push a small test image, then in the web UI: library/hello-world:test → Scan
  docker pull hello-world
  docker tag hello-world registry.nathanwhyte.dev/library/hello-world:test
  docker push registry.nathanwhyte.dev/library/hello-world:test
  # Then click "Scan" in the web UI.
  ```

- If scans are failing, check the Trivy pod has internet access (`kubectl logs -n harbor -l component=trivy`).
- The Trivy DB lives in a 5Gi Longhorn PVC on `longhorn-ssd`. If the chart 1.19 upgrade splits the trivy adapter into its own component (rumored for 2.15+), this PVC may need to be re-attached manually — check release notes.

## Garbage collection

GC is run from the web UI: **Administration → Garbage Collection → New**. Schedule it, or click "Run Now". The registry is read-only during execution.

CLI equivalent via `harbor-cli`: `harbor-cli gc list` / `harbor-cli gc create` — see `harbor/HARBOR-CLI.md`.

## Database maintenance

```bash
# Get into the PostgreSQL pod
kubectl exec -it -n harbor deployment/harbor-database -- bash

# Inside the pod
psql -U postgres -d registry

# Useful queries
SELECT pg_size_pretty(pg_database_size('registry'));   -- current DB size
\dt                                                 -- list tables
VACUUM ANALYZE;                                     -- periodic maintenance

# Logical backup (run from outside the pod, ~30s)
kubectl exec -n harbor deployment/harbor-database -- \
  env PGPASSWORD=$(kubectl get secret -n harbor harbor-database -o jsonpath='{.data.password}' | base64 -d) \
  pg_dump -U postgres registry > /tmp/harbor-db-$(date +%Y%m%d).sql
```

For a full disaster-recovery backup, snapshot all 3 Longhorn PVCs (`harbor-registry-rwo`, `harbor-jobservice-rwo`, `harbor-database`) via the Longhorn UI or `longhornctl snapshot create <pvc>`.

## Upgrading

See `harbor/deploy-harbor.sh` for the upgrade command. **As of 2026-06-10, the cluster is on chart 1.19.1 / Harbor 2.15.1** (app components) with DB and Redis pinned at 2.14.3. The full refresh-and-upgrade plan that produced this state is at `~/code/personal-compendium/docs/plans/2026-06-10-IDEA-021-harbor-refresh-and-upgrade.md`. Key points for the next upgrade:

- Read the latest chart release notes and the latest app release notes **before** upgrading.
- Take a `pg_dump` and a Longhorn snapshot of the 3 RWO PVCs.
- Run `helm diff upgrade` (use `deploy-harbor.sh --diff` if `helm-diff` is installed) to confirm the only changes are image-tag bumps.
- Expect 5-10 min total wall time, 2-5 min of registry downtime.

## DB/Redis stuck on a pinned version

The `database.internal.image.tag` and `redis.internal.image.tag` keys in `harbor-values.yaml` are explicit overrides — Helm's chart `appVersion` does **not** coerce them on upgrade. If `kubectl get pods -n harbor` shows `harbor-db` or `redis-photon` at a version older than the rest of the app components, the values file is pinning them.

As of 2026-06-10, the in-repo values file intentionally pins them at v2.14.3 (one minor behind the v2.15.1 app) for stability. To roll them forward:

1. Edit `harbor/harbor-values.yaml` — bump `database.internal.image.tag` and `redis.internal.image.tag` to the running Harbor app version (e.g. `v2.15.1`).
2. Re-run `harbor/deploy-harbor.sh` (no flag needed).
3. Confirm: `kubectl get pods -n harbor -o jsonpath='{.items[*].spec.containers[0].image}' | tr ' ' '\n' | sort -u` — all 8 images should be at the new version.

## Common failure modes

| Symptom | Check |
|---|---|
| `harbor-core` CrashLoopBackOff after upgrade | Probe-timeout patch not re-applied (see above) |
| `harbor-registry` stuck Pending | PVC `harbor-registry-rwo` not Bound; check Longhorn replica status |
| Push fails with 413 / "request entity too large" | Traefik middleware `harbor-no-limit` missing; `kubectl apply -f harbor/harbor-middleware.yaml` |
| Cert not issuing | `kubectl describe certificate harbor-tls -n harbor`; cert-manager logs; Cloudflare API token present in `cert-manager` namespace |
| Trivy scans failing | `kubectl logs -n harbor -l component=trivy`; check internet egress |
| Image pull from another host fails with x509 | Cert rotation; check `kubectl get certificate -n harbor` |
| Helm upgrade hangs | Check that all `existingClaim` references still resolve to Bound PVCs |

## See also

- `harbor/HARBOR.md` — one-page index
- `harbor/HARBOR-CLI.md` — `harbor-cli` workflows
- `harbor/RWO-MIGRATION.md` — May 2026 RWX→RWO migration runbook (frozen)
- `harbor/deploy-harbor.sh` — install / upgrade
- `~/code/personal-compendium/docs/plans/2026-06-10-IDEA-021-harbor-refresh-and-upgrade.md` — 2.15.1 upgrade plan
