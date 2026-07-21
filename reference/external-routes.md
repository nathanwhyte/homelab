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

> `lamp.nathanwhyte.dev`/`mem0.nathanwhyte.dev`/`api-mem0.nathanwhyte.dev` removed 2026-07-02 — headlamp and the mem0 stack were torn down live (see IMPR-1028). `hermes.nathanwhyte.dev` removed 2026-07-16 — Hermes was retired (see `hermes/RETIRED.md`).

## LAN / Tailnet-only admin UIs (IMPR-1029)

Not tunneled through Cloudflare at all — no public hostname exists. Reachable
only from the LAN (`192.168.1.0/24`) or a Tailscale-connected client, via
Traefik on 192.168.1.19.

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
> records in total; the zone went from 49 to 38. Neither host resolves publicly
> any more, so the tunnel's catch-all 404 is no longer reachable for them.
>
> The `ipAllowList` middlewares (`k8s-dashboard-lan-only`, `longhorn-lan-only`)
> are **wired to both Ingresses since 2026-07-20** (BUG-1031 resolved: the
> "middleware ... does not exist" failures were caused by referencing
> middlewares without the namespace prefix — cross-provider refs from Ingress
> annotations must use `<namespace>-<name>@kubernetescrd`, e.g.
> `longhorn-system-longhorn-lan-only@kubernetescrd`. The provider was never
> broken). Both hosts now enforce LAN-only source filtering
> (192.168.1.0/24 + pod/service CIDRs), verified live with curl on both routes.

| Host                       | Backend                           | Auth                         | Notes                                                                                          |
| -------------------------- | ---------------------------------- | ----------------------------- | ------------------------------------------------------------------------------------------------ |
| `k8s.nathanwhyte.dev`      | `kubernetes-dashboard-kong-proxy` | ServiceAccount bearer token   | Traefik Ingress (`dashboard/ingress.yaml`)                                                       |
| `longhorn.nathanwhyte.dev` | `longhorn-frontend`               | —                             | Traefik Ingress (Helm-managed, `longhorn/longhorn-values.yaml`)                                  |

### How these are actually reached (corrected 2026-07-20)

The paragraph previously here described Tailscale Split DNS as the resolution
mechanism. **It was a plan, not a description, and it was never carried out** —
`tailscale dns status` on 2026-07-20 shows MagicDNS enabled tailnet-wide
(`tailbca2b8.ts.net`) with **no resolvers configured and an empty Split DNS
Routes list**. No resolver exists to point such a route at: no Pi-hole, AdGuard,
dnsmasq or Blocky anywhere, and CoreDNS is ClusterIP-only. This is IMPR-1029
step 5, never completed — and reading as description is why that went unnoticed.

Current state per host:

| Host | Reachable how | Note |
| --- | --- | --- |
| `longhorn.nathanwhyte.dev` | **Not by name.** Use `https://timmy.tailbca2b8.ts.net` | `tailscale serve` on timmy → `longhorn-frontend` ClusterIP, real Let's Encrypt cert, tailnet-only (IMPR-1091) |
| `k8s.nathanwhyte.dev` | **Over HTTP only** — `http://k8s.nathanwhyte.dev` from the LAN or a Tailscale client | Returned 500 until 2026-07-20: Traefik could not verify Kong's self-signed backend cert (no IP SANs). Fixed with a `ServersTransport` (`dashboard/serverstransport.yaml`); now 200. **Not browser-reachable** — see the HSTS note below |

Two constraints worth knowing before reaching for `/etc/hosts`:

- **`.dev` is HSTS-preloaded**, so browsers force HTTPS and cannot be talked out
  of it. Both Ingresses declare `router.entrypoints: web` with no `spec.tls`, so
  port 443 answers with Traefik's default self-signed cert →
  `ERR_CERT_AUTHORITY_INVALID`. A hosts entry alone does not make either host
  browser-reachable.
- **`curl` ignores HSTS preload**, so it returns 200 on paths no browser can
  take. Verify the cert chain, not just the status code.

Options if the `*.nathanwhyte.dev` names are wanted back: cert-manager on the
Ingresses (a Ready `letsencrypt-prod` ClusterIssuer and DNS-01 already exist), or
a resolver plus a real Split DNS route. Neither is done. Background:
`docs/research/2026-07-20-homelab-tunnel-and-ingress-drift-audit.md` in the
compendium.

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
