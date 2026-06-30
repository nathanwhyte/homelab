# External routes + Tailscale reference

Mix of Cloudflare tunnel (`cloudflare/main-tunnel/cloudflared-configmap.yaml`) and
Traefik IngressRoutes. In the Notes column below, `CF` = Cloudflare-tunnel-only,
`Ingress` = Traefik IngressRoute-only, `both` = both. Lives in `reference/` (not
`CLAUDE.md`) so the SessionStart hook stays under the 40k char cap.

## External routes

| Host                                      | Backend                     | Auth                                            | Notes                                                                                                                                                                                                                                                                                                       |
| ----------------------------------------- | --------------------------- | ----------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `context.nathanwhyte.dev`                 | `openviking:1933`           | OV `root_api_key`                               | API + `/mcp`; both                                                                                                                                                                                                                                                                                          |
| `hermes.nathanwhyte.dev`                  | `hermes-agent:9119`         | session token                                   | Dashboard; both                                                                                                                                                                                                                                                                                             |
| `viking.nathanwhyte.dev`                  | `ov-console:8020`           | OV `root_api_key`                               | OV console; Ingress (see `viking/manifests/ov-console-ingress.yaml`)                                                                                                                                                                                                                                        |
| `llama.nathanwhyte.dev`                   | `ollama-auth-proxy`         | Bearer token                                    | CF                                                                                                                                                                                                                                                                                                          |
| `api-mem0.nathanwhyte.dev`                | `mem0-server:8080`          | `ADMIN_API_KEY`                                 | Mem0 API; CF (dashboard's browser-side client uses this as `NEXT_PUBLIC_API_URL`)                                                                                                                                                                                                                           |
| `ssh.nathanwhyte.dev`                     | SSH → timmy:22, user `noot` | Access service token + `homelab-breakglass` key | Break-glass SSH → timmy:22 (works with Tailscale off). Tunnel proxies raw TCP to timmy:22; break-glass pubkey in root-owned `/etc/ssh/auth_keys/%u` (sshd `sshd_config.d/10-breakglass.conf`). Hop from timmy to wemby/manu. Client `~/.ssh/config` sources the service-token env file in its ProxyCommand. |
| `mem0.nathanwhyte.dev`                    | `mem0-dashboard:3000`       | —                                               | Mem0 dashboard; CF                                                                                                                                                                                                                                                                                          |
| `k8s.nathanwhyte.dev`                     | k8s-dashboard               | —                                               | CF                                                                                                                                                                                                                                                                                                          |
| `lamp.nathanwhyte.dev`                    | headlamp                    | —                                               | CF                                                                                                                                                                                                                                                                                                          |
| `uploads.nathanwhyte.dev`                 | garage:3900                 | —                                               | S3; CF                                                                                                                                                                                                                                                                                                      |
| `logs.nathanwhyte.dev`                    | grafana                     | —                                               | CF                                                                                                                                                                                                                                                                                                          |
| `longhorn.nathanwhyte.dev`                | longhorn-frontend           | —                                               | CF                                                                                                                                                                                                                                                                                                          |
| `chat.nathanwhyte.dev`                    | open-webui                  | —                                               | CF                                                                                                                                                                                                                                                                                                          |
| `nathanwhyte.dev` / `www.nathanwhyte.dev` | portfolio                   | —                                               | CF                                                                                                                                                                                                                                                                                                          |

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
