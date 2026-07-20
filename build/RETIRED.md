# build-hook CI — RETIRED 2026-07-20

The webhook-driven image builder in the `build` namespace was torn down on
2026-07-20. Manifests are retained in this directory; nothing is applied.

## What it was

`hook` (Rust, [nathanwhyte/build-hook](https://github.com/nathanwhyte/build-hook))
received authenticated webhook calls on port 3000, built images with `buildkitd`
over buildx, pushed them to `registry.nathanwhyte.dev`, and restarted the target
Deployments. Five projects were configured in the `hook-config` ConfigMap:

| Project      | Source repo                        | Images built                          | Redeployed            |
| ------------ | ---------------------------------- | ------------------------------------- | --------------------- |
| Build Hook   | `nathanwhyte/build-hook`           | `build-hook/api:latest`               | `build/hook`          |
| Credit Coach | `nathanwhyte/credit-coach`         | `coach/coach`, `coach/scrub`          | `coach/*`             |
| Equal Risk   | `nathanwhyte/equal-risk-portfolio` | `equal-risk/rails`, `equal-risk/math` | `equal-risk/*`        |
| Portfolio    | `nathanwhyte/nathanwhyte.dev`      | `portfolio/portfolio:latest`          | `portfolio/portfolio` |
| Glossary     | `nathanwhyte/glossary`             | `glossary/glossary:latest`            | `glossary/glossary`   |

## Why it was retired

It had already been non-functional for roughly four months.

The `build` Cloudflare tunnel (`a11f180d`) was deleted on 2026-03-28 during the
consolidation into the cluster-wide `homelab` tunnel (see INFO-1120). Nothing
repointed the webhook endpoint, so the `tunnel` Deployment sat in a retry loop
logging `Unauthorized: Tunnel not found` and no inbound webhook could reach
`hook`. Pushes to all five repos stopped building or deploying anything at that
point, silently.

Discovered 2026-07-20 while auditing tunnel drift (IMPR-1028). Given the outage
had gone unnoticed that long, the pipeline was retired rather than restored.

## Consequences

- The five projects build and deploy **manually** now. `portfolio` (serving
  `nathanwhyte.dev`/`www`) and `equal-risk` (serving `risk.nathanwhyte.dev`) are
  live public services still running images built before 2026-03-28.
- Any GitHub webhooks still configured on those repos point at a dead endpoint
  and should be removed repo-side. Not done as part of this teardown.
- `buildkitd` (`buildkitd.yaml`) was torn down with the rest — it existed only to
  serve `hook`.

## What was left in place

| Item                                             | Why                                                   |
| ------------------------------------------------ | ----------------------------------------------------- |
| Namespace `build`                                | Holds the secrets below; deleting it destroys them    |
| Secret `github-access-token`                     | PAT — regenerable, but not recoverable from this repo |
| Secret `bearer-tokens-secret`                    | Inbound webhook auth tokens                           |
| Secret `registry-credentials`                    | Harbor dockerconfigjson                               |
| ConfigMap `hook-config`                          | The five-project config, mirrored in the table above  |
| ServiceAccount/Role/RoleBinding `buildx-builder` | Cheap to keep, needed on restore                      |

Secret `hook-cloudflared-token` was **deleted** — its tunnel no longer exists, so
the token is worthless.

hostPath directories `/data/build-cache` and `/data/build-context` remain on
whichever node last ran the pod. Deleting the workloads does not reclaim them;
clean up by hand if the disk is wanted.

## Restore

1. `kubectl apply -f buildkitd.yaml -f build-hook.yaml`
2. Do **not** recreate a dedicated tunnel. Add an ingress rule to the
   cluster-wide tunnel in `../cloudflare/main-tunnel/cloudflared-configmap.yaml`:

   ```yaml
   - hostname: hook.nathanwhyte.dev
     service: http://hook.build.svc.cluster.local:3000
   ```

   then create the proxied CNAME to `936478c5-…​.cfargotunnel.com` and drop the
   `tunnel` Deployment from `build-hook.yaml` — it is retained only as a record.

3. Repoint the GitHub webhooks at the new URL and confirm a delivery succeeds.
4. Pin `build-hook/api` to a digest (IMPR-1023) — it ran `:latest` throughout.

Review the bearer-token auth on `hook` before re-exposing it publicly; it was
never audited.

## See also

- INFO-1120 — Cloudflare tunnel topology and consolidation history
- IMPR-1028 — repo-vs-live drift reconciliation, which surfaced this
- `harbor/HARBOR.md` — the registry these images were pushed to
