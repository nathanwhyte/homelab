# Cloudflare Access for naked admin UIs (IMPR-1020)

Decision (2026-07-02): protect the admin hosts that have **no app-level
auth** with Cloudflare Access; leave `logs` (Grafana login), `chat` (Open WebUI
accounts), and `uploads` (S3 keys) on their app-level auth.

Scope reduced same day: `lamp` (headlamp) and `mem0` were torn down entirely
(namespaces deleted, tunnel routes removed) instead of protected.

| Host                       | Backend       | Why                          |
| -------------------------- | ------------- | ---------------------------- |
| `longhorn.nathanwhyte.dev` | longhorn UI   | no auth at all; volume admin |
| `k8s.nathanwhyte.dev`      | k8s dashboard | token login only after load  |

## Setup (Zero Trust dashboard — one app per host)

No infra/manifest change needed: the hosts already route through the
Cloudflare tunnel, and Access evaluates before the tunnel origin is reached.

1. [one.dash.cloudflare.com](https://one.dash.cloudflare.com) → Access → Applications → **Add an application** → Self-hosted.
2. Application domain: the host (e.g. `longhorn.nathanwhyte.dev`), no path.
3. Session duration: 24h.
4. Policy `admin-only`: Action **Allow**, Include → Emails → `nathanwhyte35@gmail.com`.
5. Login method: One-time PIN (zero setup) or GitHub SSO if already connected.
6. Repeat for both hosts (or use one app with multiple domains).

## After enabling

- Update the Auth column for these hosts in
  `reference/external-routes.md` from `—` to `CF Access (email OTP)`.
- While in the dashboard: delete the orphaned DNS records for
  `lamp.nathanwhyte.dev`, `mem0.nathanwhyte.dev`, and
  `api-mem0.nathanwhyte.dev` (their tunnel routes and backends are gone).
- API-driven alternative (for automation later): `POST
  /client/v4/accounts/{account_id}/access/apps` with a token scoped to
  "Access: Apps and Policies — Edit".
