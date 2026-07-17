# Retired 2026-07-16

The `hermes` namespace was deleted in full: `hermes-agent` and `hermes-jump`
Services, the `hermes-dashboard`/`hermes-dashboard-redirect` Ingresses, the
`hermes-config`/`hermes-jump-entrypoint` ConfigMaps, all Secrets
(`hermes-api-server-key`, `hermes-dashboard-token`, `hermes-dashboard-tls`,
`hermes-jump-ssh-key`, `hermes-linear-key`, `hermes-slack-tokens`,
`slack-secrets`, `github-access-token`, `mem0-api-key`, `openviking-api-key`),
and the `hermes-home` (10Gi) and `hermes-jump-home` (5Gi) PVCs — deleted
without a backup dump (contained real session/config data and jump-pod
scratch files from mid-June, reviewed and knowingly discarded). The
`hermes.nathanwhyte.dev` Cloudflare tunnel route was also removed
(`cloudflare/main-tunnel/cloudflared-configmap.yaml`); the DNS record itself
still needs deleting in the Cloudflare dashboard.

Hermes was never fully deployed live per IMPR-1025 (no `hermes-agent`
Deployment/pods were ever running), but its `hermes` namespace did carry real
infra (Services, PVCs with data, Ingress, Secrets) from earlier development.
Its sole memory provider, mem0, was retired 2026-07-02 ([`../mem0/TORN-DOWN.md`](../mem0/TORN-DOWN.md))
after repeated operational issues; OpenViking's knowledge-base tooling has
since covered the persistent-memory use case Hermes needed, so rather than
switch Hermes to a new memory provider the project itself was retired.

Manifests are retained in this directory for reference. Restoring would
require re-applying the manifests, re-provisioning all secrets from scratch,
and deciding on a replacement memory provider (mem0 is gone; OpenViking KB
tools alone, or a new provider).
