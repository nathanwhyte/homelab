# Cloudflare Access for naked admin UIs (IMPR-1020)

Decision (2026-07-02): protect the four admin hosts that have **no app-level
auth** with Cloudflare Access; leave `logs` (Grafana login), `chat` (Open WebUI
accounts), and `uploads` (S3 keys) on their app-level auth.

| Host                       | Backend        | Why                          |
| -------------------------- | -------------- | ---------------------------- |
| `longhorn.nathanwhyte.dev` | longhorn UI    | no auth at all; volume admin |
| `k8s.nathanwhyte.dev`      | k8s dashboard  | token login only after load  |
| `lamp.nathanwhyte.dev`     | headlamp       | token login only after load  |
| `mem0.nathanwhyte.dev`     | mem0 dashboard | no auth                      |

## Setup (Zero Trust dashboard — one app per host)

No infra/manifest change needed: the hosts already route through the
Cloudflare tunnel, and Access evaluates before the tunnel origin is reached.

1. [one.dash.cloudflare.com](https://one.dash.cloudflare.com) → Access → Applications → **Add an application** → Self-hosted.
2. Application domain: the host (e.g. `longhorn.nathanwhyte.dev`), no path.
3. Session duration: 24h.
4. Policy `admin-only`: Action **Allow**, Include → Emails → `nathanwhyte35@gmail.com`.
5. Login method: One-time PIN (zero setup) or GitHub SSO if already connected.
6. Repeat for all four hosts (or use one app with multiple domains).

## After enabling

- Update the Auth column for these four hosts in
  `reference/external-routes.md` from `—` to `CF Access (email OTP)`.
- Note for `mem0.nathanwhyte.dev`: the dashboard's browser client calls
  `api-mem0.nathanwhyte.dev` (`ADMIN_API_KEY`-protected) — putting Access in
  front of the API host too would break non-browser callers; leave it on its
  key auth.
- API-driven alternative (for automation later): `POST
  /client/v4/accounts/{account_id}/access/apps` with a token scoped to
  "Access: Apps and Policies — Edit".
