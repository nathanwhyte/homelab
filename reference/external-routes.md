# External routes + Tailscale reference

Mix of Cloudflare tunnel (`cloudflare/main-tunnel/cloudflared-configmap.yaml`) and
Traefik IngressRoutes. In the Notes column below, `CF` = Cloudflare-tunnel-only,
`Ingress` = Traefik IngressRoute-only, `both` = both. Lives in `reference/` (not
`CLAUDE.md`) so the SessionStart hook stays under the 40k char cap.

## External routes

| Host                                      | Backend                     | Auth                                            | Notes                                                                                                                                                                                                                                                                                                       |
| ----------------------------------------- | --------------------------- | ----------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `context.nathanwhyte.dev`                 | `openviking:1933`           | OV `root_api_key`                               | API + `/mcp` + `/studio/` web console (v0.4.10 built-in; root-key login, no trailing whitespace); both                                                                                                                                                                                                       |
| `llama.nathanwhyte.dev`                   | `ollama-auth-proxy`         | Bearer token                                    | CF                                                                                                                                                                                                                                                                                                          |
| `ssh.nathanwhyte.dev`                     | SSH → timmy:22, user `noot` | Access service token + `homelab-breakglass` key | Break-glass SSH → timmy:22 (works with Tailscale off). Tunnel proxies raw TCP to timmy:22; break-glass pubkey in root-owned `/etc/ssh/auth_keys/%u` (sshd `sshd_config.d/10-breakglass.conf`). Hop from timmy to wemby/manu. Client `~/.ssh/config` sources the service-token env file in its ProxyCommand. |
| `uploads.nathanwhyte.dev`                 | garage:3900                 | —                                               | S3; CF                                                                                                                                                                                                                                                                                                      |
| `logs.nathanwhyte.dev`                    | grafana                     | —                                               | CF                                                                                                                                                                                                                                                                                                          |
| `chat.nathanwhyte.dev`                    | open-webui                  | —                                               | CF                                                                                                                                                                                                                                                                                                          |
| `nathanwhyte.dev` / `www.nathanwhyte.dev` | portfolio                   | —                                               | CF                                                                                                                                                                                                                                                                                                          |
| `risk.nathanwhyte.dev` / `equalriskportfolio.com` / `www.equalriskportfolio.com` | `rails.equal-risk:3000`    | Rails application                              | Dedicated Cloudflare `eqrsk` tunnel; all three hostnames share one ingress rule. Provider routing is recorded in [`cloudflare/namespace-tunnels/eqrsk-routing.yaml`](../cloudflare/namespace-tunnels/eqrsk-routing.yaml) |

> `lamp.nathanwhyte.dev`/`mem0.nathanwhyte.dev`/`api-mem0.nathanwhyte.dev` removed 2026-07-02 — headlamp and the mem0 stack were torn down live (see IMPR-1028). `hermes.nathanwhyte.dev` removed 2026-07-16 — Hermes was retired (see `hermes/RETIRED.md`).

## LAN / Tailnet-only admin UIs (IMPR-1029)

Not tunneled through Cloudflare at all. ⚠️ **Updated 2026-08-24 (PROJ-1018):
they no longer have public DNS records either.** The unproxied A records that
used to point at private `192.168.1.19` were deleted; these names now resolve
only via Pi-hole local records on LAN and Tailscale split DNS on the tailnet.
Reachable, as before, only from the LAN (`192.168.1.0/24`) or a
Tailscale-connected client, via Traefik — but now the *name* is private too, not
just the address it pointed to.

> **Status (2026-07-20): cut over. Repo and live now agree.**
> The live tunnel config was read against the Cloudflare API on 2026-07-20 and
> serves 9 hostnames plus the `http_status:404` catch-all — neither
> `k8s.nathanwhyte.dev` nor `longhorn.nathanwhyte.dev` is among them. The
> earlier note here ("repo has them removed, live cluster does not") described a
> divergence that no longer exists.
>
> The public DNS records for both hosts were **deleted 2026-07-20** (IMPR-1029
> step 3), along with the other stale entries — `hermes`, `mem0`, `api-mem0`,
> `lamp`, `viking`, `ssh-manu`, `ssh-timmy`, `ssh-wemby`, `ollama`. Eleven
> records in total; the zone went from 49 to 38, and the tunnel's catch-all 404
> stopped being reachable for either host.
>
> **`longhorn.nathanwhyte.dev` was subsequently restored** on 2026-07-20
> (homelab `f77a516`) — but as an unproxied `A → 192.168.1.19`, pointing at
> Traefik on the LAN rather than back at the tunnel. It is not a reversal of
> step 3; the tunnel route stays deleted. **`k8s.nathanwhyte.dev` was restored
> the same way on 2026-07-21** — unproxied `A → 192.168.1.19`, `websecure` +
> cert-manager on `dashboard/ingress.yaml`, same pattern as Longhorn.
>
> **There is no `ipAllowList` on either admin-UI route, deliberately.** The two
> middlewares (`k8s-dashboard-lan-only`, `longhorn-lan-only`) were wired to both
> Ingresses on 2026-07-20 and **removed on 2026-07-21** — they filtered nothing,
> and could not be made to on this path (BUG-1057, resolved).
>
> Measured 2026-07-21 by enabling Traefik access logs: a request from
> `192.168.1.8` was logged with `ClientHost: 192.168.1.19` — timmy's own node
> IP. **k3s ServiceLB (klipper-lb) masquerades the client address in the
> `svclb-traefik` pod**, a layer beneath kube-proxy. The node IP matches
> `192.168.1.0/24` in `sourceRange`, so everything passed and the Traefik logs
> contained zero 403s.
>
> `externalTrafficPolicy: Local` **does not fix this** — it governs kube-proxy's
> SNAT, which never gets a say because klipper has already rewritten the source.
> An earlier revision of this note proposed exactly that, and was wrong. Real
> filtering would need MetalLB, or Traefik on `hostNetwork`/`hostPort` to bypass
> klipper.
>
> Removal was chosen over keeping-them-labelled because an attached allowlist
> reads as protection in every subsequent review — inert defense-in-depth is
> worse than none. What actually gates these hosts: neither is publicly
> routable (~~both A records are unproxied and point at the private LAN IP
> `192.168.1.19`, with no tunnel route~~ — **as of 2026-08-24 there are no public
> A records at all; they were deleted, so the names are unresolvable off
> LAN/tailnet as well as unroutable**), plus tailnet membership for the
> `tailscale serve` path, plus Longhorn BasicAuth and the dashboard's own
> ServiceAccount bearer token.
>
> > **Superseded 2026-07-21 (IMPR-1020).** This paragraph originally ended
> > "**Longhorn's UI has no auth of its own** — LAN reachability is the only
> > control on it, which is the residual risk this removal makes explicit rather
> > than creates." That residual was closed the next day: Longhorn now enforces
> > **BasicAuth** (`longhorn-basicauth`) on **both** of its routes, including the
> > `tailscale serve` path, which was repointed at Traefik to make that possible.
> > See the routes table below.
>
> Anyone restoring an allowlist here must bypass klipper first, and must verify
> with a **negative** test — a client outside `sourceRange` actually receiving
> `403`. A passing request proves nothing; that mistake is what let these sit
> inert for a day (see BUG-1057's verification-method note).
>
> The namespace-prefix convention from BUG-1031 (`<namespace>-<name>@kubernetescrd`
> for cross-provider refs) still applies to every other middleware and
> ServersTransport ref in this repo — see `dashboard/serverstransport.yaml`.

| Host                       | Backend                           | Auth                         | Notes                                                                                          |
| -------------------------- | ---------------------------------- | ----------------------------- | ------------------------------------------------------------------------------------------------ |
| `k8s.nathanwhyte.dev`      | `kubernetes-dashboard-kong-proxy` | ServiceAccount bearer token   | Traefik Ingress (`dashboard/ingress.yaml`)                                                       |
| `longhorn.nathanwhyte.dev` | `longhorn-frontend`               | **BasicAuth** (`longhorn-basicauth`) | Traefik Ingress (Helm-managed, `longhorn/longhorn-values.yaml`). Added 2026-07-21 (IMPR-1020) — Longhorn has no native auth and was returning 200 unauthenticated. Credentials in `longhorn-auth-secret`; negative test verified (no creds → 401). The `tailscale serve` route was repointed at Traefik (`longhorn/tailnet-ingress.yaml`) so it enforces too |

### How these are actually reached (corrected 2026-08-25)

**Current state.** `registry`, `k8s`, and `longhorn.nathanwhyte.dev` have **no
public DNS records** — all three were deleted 2026-08-24 (PROJ-1018 Phases 3/4).
They resolve by two paths only:

| Path                | Mechanism                                                          |
| ------------------- | ------------------------------------------------------------------ |
| On LAN              | Pi-hole local records — `FTLCONF_dns_hosts`, see `network/pihole/`  |
| Off-LAN, on tailnet | Tailscale **split DNS** — nine per-host routes (3 names × 3 Pi-holes) |

Neither path is public. A device that is neither on the LAN nor on the tailnet
cannot resolve these names at all, by design.

⚠️ The split-DNS path is **configured but not yet validated from a genuinely
off-LAN device.** Treat off-LAN resolution as unproven until someone runs
`dig +short registry.nathanwhyte.dev` from a phone on cellular.

Global Nameservers remain **unset** and "Override DNS servers" is **off** — this
is split DNS, not a global override, so remote ad-blocking is unaffected
(IDEA-1027 remains unimplemented).

<details>
<summary>Superseded 2026-07-20 text — kept because its lesson still applies</summary>

The paragraph previously here described Tailscale Split DNS as the resolution
mechanism. **It was a plan, not a description, and it was never carried out** —
`tailscale dns status` on 2026-07-20 showed MagicDNS enabled tailnet-wide
(`tailbca2b8.ts.net`) with **no resolvers configured and an empty Split DNS
Routes list**. No resolver existed to point such a route at: no Pi-hole, AdGuard,
dnsmasq or Blocky anywhere, and CoreDNS is ClusterIP-only. This was IMPR-1029
step 5, never completed — and reading as description is why that went unnoticed.

Both halves of that snapshot are now false: Pi-hole runs on all three hosts, and
the split-DNS routes exist. **The lesson survives the facts** — this file
describes what *is*, and a plan written in the present tense is how the original
error happened.

</details>

Current state per host:

| Host | Reachable how | Note |
| --- | --- | --- |
| `longhorn.nathanwhyte.dev` | **By name, over HTTPS.** `https://longhorn.nathanwhyte.dev` | ⚠️ **A record deleted 2026-08-24 (PROJ-1018)** — resolves via Pi-hole local records on LAN, Tailscale split DNS off-LAN. Historical: restored 2026-07-20 (homelab `f77a516`) as unproxied `A → 192.168.1.19`, Traefik on `websecure` with a cert-manager DNS-01 Let's Encrypt cert (`longhorn-tls`). Off-LAN needs `--accept-routes` for manu's `192.168.1.0/24` route |
| `longhorn` — second route | `https://timmy.tailbca2b8.ts.net` | `tailscale serve` on timmy → **Traefik** (`https+insecure://10.43.138.211:443`), tailnet-only. Kept alongside the hostname: needs no subnet route and no `--accept-routes`. **Repointed 2026-07-21 (IMPR-1020)** — it previously proxied straight to `longhorn-frontend` ClusterIP, which bypassed Traefik entirely and so served Longhorn with **no auth** even after BasicAuth was added to the hostname route. `longhorn/tailnet-ingress.yaml` gives this Host its own Traefik router carrying the same middleware, since `serve` forwards the original Host header and it would otherwise match no rule |
| `k8s.nathanwhyte.dev` | **By name, over HTTPS.** `https://k8s.nathanwhyte.dev` | ⚠️ **A record deleted 2026-08-24 (PROJ-1018)** — resolves via Pi-hole local records on LAN, Tailscale split DNS off-LAN. Historical: restored 2026-07-21, same pattern as Longhorn — unproxied `A → 192.168.1.19`, `dashboard/ingress.yaml` moved to `websecure` with a cert-manager DNS-01 Let's Encrypt cert (`k8s-dashboard-tls`). The `kubernetes-dashboard-kong-proxy:443` backend TLS was already handled by `dashboard/serverstransport.yaml` (Kong's self-signed cert has no IP SANs) |
| k8s dashboard — second route | `https://timmy.tailbca2b8.ts.net:8443` | `tailscale serve` → `kubernetes-dashboard-kong-proxy` ClusterIP via `https+insecure://`, real Let's Encrypt cert (IMPR-1091). Kept alongside the hostname for the same convenience reason (no subnet route needed). **Unlike Longhorn's second route, this one still bypasses Traefik** — deliberately: the dashboard enforces its own ServiceAccount bearer token, so a Traefik middleware adds nothing. Verified 2026-07-21 — the root path serves the SPA at 200, but `/api/v1/*` returns `MSG_LOGIN_UNAUTHORIZED_ERROR` without a token. Do not read the root 200 as "unauthenticated" |

Both `*.nathanwhyte.dev` admin hosts now satisfy the two constraints a `.dev`
domain requires — name resolution and a valid certificate, since HSTS preload
means browsers force HTTPS and cannot be talked out of it:

- **Longhorn** since `f77a516` (2026-07-20): `websecure` + cert-manager.
- **k8s dashboard** since 2026-07-21: same treatment, `dashboard/ingress.yaml`.

`curl` ignores HSTS preload, so it's useful for scripted checks against a host
that doesn't yet have both — but it will return 200 on paths no browser can
take, so verify the cert chain, not just the status code, when troubleshooting.

One caveat observed 2026-07-21 rolling out the dashboard record: a resolver
between the client and Cloudflare's authoritative servers (in this case the LAN
resolver at `192.168.1.1`) can hold a cached `NXDOMAIN` for a name from before
its record existed. The record itself was live on Cloudflare within
seconds — `dig @1.1.1.1` and `dig @8.8.8.8` both answered correctly — but
`192.168.1.1` kept returning `NXDOMAIN` for the SOA negative-cache TTL (~25
min) before self-correcting. No action needed beyond waiting it out (or
flushing that resolver's cache directly, if it's reachable). Tailscale's
`100.100.100.100` was not the cause — `tailscale dns status` confirms no
tailnet resolvers are configured, so it forwards straight through to
`192.168.1.1`.

## Tailscale — private external access (PROJ-008)

Host-level Tailscale on all 3 nodes (WireGuard mesh) for **private admin/network access**
from off-LAN. Complements the Cloudflare Tunnel (public web); the two coexist. Runbook:
`tailscale/README.md`.

| Node  | Tailscale role   | Tailnet IP     | OS user |
| ----- | ---------------- | -------------- | ------- |
| manu  | HA subnet router | 100.114.66.32  | noot    |
| wemby | HA subnet router | 100.118.40.21  | natew   |
| timmy | plain node       | 100.95.215.105 | noot    |

- **Advertised routes**: `192.168.1.0/24` only (cluster CIDRs dropped — reachable via LAN route, advertising risked collision)
- **`--accept-routes`**: belongs only on **off-LAN client devices** (MacBook/phone). Do NOT set on the nodes themselves — they're physically on the advertised subnet and accepting the route causes asymmetric routing (ERR-007)
- **kubectl**: two contexts in `~/.kube/config` — `homelab` (LAN IP 192.168.1.19:6443 via subnet route) and `tailnet` (100.95.215.105:6443 direct; requires `--tls-san` in `/etc/rancher/k3s/config.yaml` on timmy, already done)
- **SSH**: `ssh wemby` / `ssh manu` / `ssh timmy` via `~/.ssh/config` aliases (all point to tailnet IPs). Wemby user is `natew`, not `noot`
- **Tailscale SSH** (`tailscale ssh`): ACL-gated keyless SSH also available; wemby requires `tailscale ssh natew@wemby`
