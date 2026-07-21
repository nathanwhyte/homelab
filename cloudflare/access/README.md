# Cloudflare Access for naked admin UIs (IMPR-1020)

> **⚠️ NEVER CARRIED OUT FOR THESE HOSTS, AND NOW INAPPLICABLE TO THEM.**
> Retained as a **pattern for future genuinely-public hosts** — not as a
> description of anything configured. Read this banner before following a step.
>
> - **Neither host below is on the Cloudflare tunnel any more** (IMPR-1029,
>   shipped 2026-07-21). Both were removed from the tunnel and their public
>   records now point at `192.168.1.19`. The setup section's premise — "the
>   hosts already route through the Cloudflare tunnel" — is therefore **false**,
>   and following this runbook for either host would configure an Access app in
>   front of traffic that never reaches Cloudflare.
> - **`longhorn.nathanwhyte.dev` is no longer "no auth at all"** — it enforces
>   Traefik **BasicAuth** as of 2026-07-21 (IMPR-1020, `longhorn/middleware.yaml`).
> - **Exactly one Access application exists account-wide: `SSH`**
>   (`ssh.nathanwhyte.dev`), verified against the API 2026-07-20. No Access app
>   was ever created for any host in the table below.
> - IMPR-1020 shipped **without** adding Cloudflare Access anywhere: every
>   remaining tunneled host was measured to already enforce its own auth, so
>   Access would have been defense-in-depth rather than gap-closing. That remains
>   a reasonable future improvement — see IMPR-1020's residual-risk section.
>
> The steps below are still accurate **as a procedure**, for a host that really
> is tunnel-fronted and really does lack auth.

Decision (2026-07-02): protect the admin hosts that have **no app-level
auth** with Cloudflare Access; leave `logs` (Grafana login), `chat` (Open WebUI
accounts), and `uploads` (S3 keys) on their app-level auth.

Scope reduced same day: `lamp` (headlamp) and `mem0` were torn down entirely
(namespaces deleted, tunnel routes removed) instead of protected.

| Host                       | Backend       | Why (as assessed 2026-07-02 — both since superseded) |
| -------------------------- | ------------- | ---------------------------------------------------- |
| `longhorn.nathanwhyte.dev` | longhorn UI   | no auth at all; volume admin — **now BasicAuth**     |
| `k8s.nathanwhyte.dev`      | k8s dashboard | token login only after load — **now off the tunnel** |

## Setup (Zero Trust dashboard — one app per host)

_Applies to a host that is actually tunnel-fronted; see the banner above — the
two hosts in the table are not._

No infra/manifest change needed **provided the host routes through the
Cloudflare tunnel**, since Access evaluates before the tunnel origin is reached.
Confirm the host has a live ingress rule in
`cloudflare/main-tunnel/cloudflared-configmap.yaml` before starting.

1. [one.dash.cloudflare.com](https://one.dash.cloudflare.com) → Access → Applications → **Add an application** → Self-hosted.
2. Application domain: the host (e.g. `longhorn.nathanwhyte.dev`), no path.
3. Session duration: 24h.
4. Policy `admin-only`: Action **Allow**, Include → Emails → `nathanwhyte35@gmail.com`.
5. Login method: One-time PIN (zero setup) or GitHub SSO if already connected.
6. Repeat for both hosts (or use one app with multiple domains).

## After enabling

- Update the Auth column for the host in `reference/external-routes.md` to
  `CF Access (email OTP)`.
- ~~While in the dashboard: delete the orphaned DNS records for
  `lamp.nathanwhyte.dev`, `mem0.nathanwhyte.dev`, and
  `api-mem0.nathanwhyte.dev`.~~ **Done 2026-07-20** — all three were deleted as
  part of a sweep of 11 stale records (IMPR-1093 / INFO-1120); each deletion was
  gated on two independent proofs and confirmed authoritative. All three are
  NXDOMAIN as of 2026-07-21.
- API-driven alternative (for automation later): `POST
/client/v4/accounts/{account_id}/access/apps` with a token scoped to
  "Access: Apps and Policies — Edit".
