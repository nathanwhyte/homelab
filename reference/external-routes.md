# External routes + Tailscale reference

Mix of Cloudflare tunnel (`cloudflare/main-tunnel/cloudflared-configmap.yaml`) and
Traefik IngressRoutes. In the Notes column below, `CF` = Cloudflare-tunnel-only,
`Ingress` = Traefik IngressRoute-only, `both` = both. Lives in `reference/` (not
`CLAUDE.md`) so the SessionStart hook stays under the 40k char cap.

## External routes

| Host                                      | Backend                     | Auth                                            | Notes                                                                                                                                                                                                                                                                                                       |
| ----------------------------------------- | --------------------------- | ----------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `context.nathanwhyte.dev`                 | `openviking:1933`           | OV `root_api_key`                               | API + `/mcp` + `/studio/` web console (v0.4.10 built-in; root-key login, no trailing whitespace); both                                                                                                                                                                                                       |
| `hermes.nathanwhyte.dev`                  | `hermes-agent:9119`         | session token                                   | Dashboard; both                                                                                                                                                                                                                                                                                             |
| `llama.nathanwhyte.dev`                   | `ollama-auth-proxy`         | Bearer token                                    | CF                                                                                                                                                                                                                                                                                                          |
| `ssh.nathanwhyte.dev`                     | SSH → timmy:22, user `noot` | Access service token + `homelab-breakglass` key | Break-glass SSH → timmy:22 (works with Tailscale off). Tunnel proxies raw TCP to timmy:22; break-glass pubkey in root-owned `/etc/ssh/auth_keys/%u` (sshd `sshd_config.d/10-breakglass.conf`). Hop from timmy to wemby/manu. Client `~/.ssh/config` sources the service-token env file in its ProxyCommand. |
| `uploads.nathanwhyte.dev`                 | garage:3900                 | —                                               | S3; CF                                                                                                                                                                                                                                                                                                      |
| `logs.nathanwhyte.dev`                    | grafana                     | —                                               | CF                                                                                                                                                                                                                                                                                                          |
| `chat.nathanwhyte.dev`                    | open-webui                  | —                                               | CF                                                                                                                                                                                                                                                                                                          |
| `nathanwhyte.dev` / `www.nathanwhyte.dev` | portfolio                   | —                                               | CF                                                                                                                                                                                                                                                                                                          |

> `lamp.nathanwhyte.dev`/`mem0.nathanwhyte.dev`/`api-mem0.nathanwhyte.dev` removed 2026-07-02 — headlamp and the mem0 stack were torn down live (see IMPR-1028).

## LAN / Tailnet-only admin UIs (IMPR-1029)

Not tunneled through Cloudflare at all — no public hostname exists. Reachable
only from the LAN (`192.168.1.0/24`) or a Tailscale-connected client, via
Traefik on 192.168.1.19.

> **Status (2026-07-02): still on the Cloudflare tunnel, not cut over yet.**
> The `ipAllowList` middlewares (`k8s-dashboard-lan-only`, `longhorn-lan-only`)
> exist as CRD objects but are commented out of both Ingresses — Traefik's
> `kubernetescrd` provider currently fails to resolve *any* Middleware ref
> cluster-wide ("middleware ... does not exist"), confirmed against
> pre-existing middlewares too (`hermes-lan-only`,
> `viking-openviking-basicauth` — the latter since removed 2026-07-04 with the
> API-key-only OV auth decision) after a full Traefik restart. Wiring the
> annotation back in during this session took `longhorn.nathanwhyte.dev`'s
> Traefik route down (404) even for LAN clients — reverted live. The public
> `k8s.nathanwhyte.dev`/`longhorn.nathanwhyte.dev` tunnel entries stay in
> `cloudflare/main-tunnel/cloudflared-configmap.yaml` (repo has them removed,
> live cluster does not) until this Traefik bug is fixed and the LAN-only
> path is verified end-to-end.

| Host                       | Backend                           | Auth                         | Notes                                                                                          |
| -------------------------- | ---------------------------------- | ----------------------------- | ------------------------------------------------------------------------------------------------ |
| `k8s.nathanwhyte.dev`      | `kubernetes-dashboard-kong-proxy` | ServiceAccount bearer token   | Traefik Ingress (`dashboard/ingress.yaml`)                                                       |
| `longhorn.nathanwhyte.dev` | `longhorn-frontend`               | —                             | Traefik Ingress (Helm-managed, `longhorn/longhorn-values.yaml`)                                  |

DNS: resolved via **Tailscale Split DNS** — a nameserver rule in the Tailscale
admin console (`Split DNS` for `nathanwhyte.dev`, or per-host if the tunnel's
other public hosts on the same zone need to keep resolving publicly) pointing
to 192.168.1.19. Applies tailnet-wide, on or off LAN, no per-device config.
Manual step: set up in the Tailscale admin console (not repo-managed).

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
