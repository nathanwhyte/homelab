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

## Auth: admin password

### Current model

- **The K8s Secret `harbor-core` carries `HARBOR_ADMIN_PASSWORD`**, rendered from the chart's `harborAdminPassword` value at install time. The values file (`harbor-values.yaml`) keeps this as the literal placeholder `<CHANGE_ME>`.
- **The chart only writes the DB on first install** — if the `admin` row in `harbor_user` already exists, the `harborAdminPassword` value is ignored on subsequent upgrades. The Secret and the DB can drift.
- **The default install (before this fix) was bricked**: the Secret held `<CHANGE_ME>`, the DB held a PBKDF2-SHA256 hash of an unknown original password, and no one in-cluster knew the plaintext. Reset procedure below.
- **Public pull is anonymous**: Harbor's distribution layer (`/service/token?scope=repository:*:pull`) hands out a valid pull token for any public project without checking credentials — verified 2026-06-11 that wrong-password, no-password, and admin-with-correct-password all receive the same pull-scoped token. The Core API and the push paths are still auth-gated.

### Resetting the admin password (direct DB update)

Required when:
- The K8s Secret has drifted from the DB (the most common failure mode on a long-running cluster)
- The `admin` row in `harbor_user` has an unknown password
- The "Forgot password" UI flow is unusable (the admin email field is empty by default)

**The hash algorithm** (from `goharbor/harbor/src/common/utils/encrypt/encrypt.go`): `pbkdf2_hmac('sha256', plain.encode(), salt_b64_str.encode(), 4096, 16).hex()`. The salt column is the **base64-encoded form of 24 random bytes** (32 chars), and Harbor feeds that b64 string to PBKDF2 — not the raw bytes. Output is 32 hex chars; `password VARCHAR(40)` accommodates this with room to spare.

**Procedure:**

```bash
# 1. Generate a fresh hash + salt (run on the MacBook, not the cluster)
NEW_PW=$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 24)

python3 - <<EOF
import hashlib, base64, secrets
new_salt = base64.b64encode(secrets.token_bytes(24)).decode()
new_hash = hashlib.pbkdf2_hmac('sha256', '$NEW_PW'.encode(), new_salt.encode(), 4096, 16).hex()
print(f"UPDATE harbor_user SET password='{new_hash}', salt='{new_salt}', password_version='sha256' WHERE username='admin';")
EOF

# 2. Save NEW_PW to your password manager. Wipe from the shell after.

# 3. Apply the UPDATE via psql on the DB pod
kubectl exec -n harbor harbor-database-0 -- env PGPASSWORD=changeit psql \
  -U postgres -d registry -c "<paste the printed UPDATE here>"

# 4. Verify auth works
curl -sS -u "admin:$NEW_PW" "https://registry.nathanwhyte.dev/api/v2.0/users/current" | python3 -m json.tool
# Expect: {"admin_role_in_auth":false,"comment":"admin user","realname":"system admin","sysadmin_flag":true,...,"username":"admin"}

# 5. Re-sync the K8s Secret via the deploy script
./harbor/deploy-harbor.sh --admin-password "$NEW_PW"
# (or HARBOR_ADMIN_PASSWORD=$NEW_PW ./harbor/deploy-harbor.sh)
```

**Important gotcha:** the deploy script's `--set harborAdminPassword=...` only writes the Secret — it does **not** touch the DB. Conversely, the `UPDATE` only writes the DB — it does not touch the Secret. You need both. Do the DB `UPDATE` first, then the deploy, in that order, so the Secret never holds a value that diverges from the DB (e.g. if the deploy script's pod restart ever triggers a re-migration that resets the password from the Secret, you'll be locked out again).

### Public pull — implications

Because the distribution layer issues valid pull tokens to anyone for public projects, **any image pushed to a public project on `registry.nathanwhyte.dev` is world-readable**. As of 2026-06-11, all 9 projects (`build-hook`, `coach`, `equal-risk`, `glossary`, `homelab`, `library`, `portfolio`, `robots`, `viking`) are public. This was the **intended** design (see `HARBOR.md` "Project creation: Open... public pull is anonymous, push always requires auth"), but worth being explicit about:

- Don't push secrets, internal configs, or proprietary code to a project that's public
- The credit-coach source repo is private on GitHub; the corresponding Harbor project `coach` is also public — verify that's still your intent before pushing new images
- To make a project private: `curl -u admin:$PW -X PUT "https://registry.nathanwhyte.dev/api/v2.0/projects/<name>" -H "Content-Type: application/json" -d '{"metadata":{"public":"false"}}'`

### Verifying the auth path end-to-end

```bash
# 1. Core API
curl -sS -u "admin:$HARBOR_ADMIN_PASSWORD" "https://registry.nathanwhyte.dev/api/v2.0/users/current"
#   Expect: admin user JSON, NOT a 401 errors array

# 2. Push path (creates a repo as a side effect)
docker login registry.nathanwhyte.dev -u admin -p "$HARBOR_ADMIN_PASSWORD"
docker pull hello-world
docker tag hello-world registry.nathanwhyte.dev/library/hello-world:test
docker push registry.nathanwhyte.dev/library/hello-world:test
#   Expect: digest: sha256:...; image visible in Harbor UI under library/

# 3. harbor-cli (if installed)
harbor-cli login https://registry.nathanwhyte.dev --name admin --password "$HARBOR_ADMIN_PASSWORD"
harbor-cli project list
#   Expect: 9 projects listed
```

## See also

- `harbor/HARBOR.md` — one-page index
- `harbor/HARBOR-CLI.md` — `harbor-cli` workflows
- `harbor/RWO-MIGRATION.md` — May 2026 RWX→RWO migration runbook (frozen)
- `harbor/deploy-harbor.sh` — install / upgrade
- `~/code/personal-compendium/docs/plans/2026-06-10-IDEA-021-harbor-refresh-and-upgrade.md` — 2.15.1 upgrade plan
