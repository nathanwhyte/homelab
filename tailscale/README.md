# Tailscale — external access to the K3s cluster

Private, network-level access to the homelab from outside the LAN: remote
`kubectl`, full LAN reach, and raw SSH to nodes — over a WireGuard mesh.
Complements the Cloudflare Tunnel (`cloudflare/`), which fronts **public web
apps**; Tailscale is for **your private admin/network access**. The two coexist.

Tracked in the personal compendium as **PROJ-008** (tasks TASK-052…057).

## Design — host-level, not in-cluster

Tailscale runs on each node's **OS**, like pihole + unbound — deliberately
decoupled from K3s. The reason you want remote access is to reach the cluster
when something is broken, and an in-cluster router can't help you if K3s (or the
node hosting it) is down. Host-level survives cluster outages, which is exactly
when break-glass access matters.

| Node  | Tailscale role                      | Why                                                                    |
| ----- | ----------------------------------- | ---------------------------------------------------------------------- |
| manu  | tailnet node **+ HA subnet router** | also runs pihole/unbound; advertises the LAN                           |
| wemby | tailnet node **+ HA subnet router** | also runs pihole/unbound; advertises the LAN (failover pair with manu) |
| timmy | plain tailnet node                  | SSH + kubectl reach; no LAN advertising needed                         |

manu and wemby advertise the same routes; Tailscale elects one primary and
auto-fails-over — mirroring the redundant DNS pair already on those hosts.

Each node joining the tailnet directly gives:

- **Raw SSH** — `ssh <user>@<node>` over the tailnet, or keyless **Tailscale SSH**
  (`--ssh`, ACL-gated). No `cloudflared access` wrapper.
- **kubectl** — point kubeconfig at a node's tailnet IP (or LAN IP via the subnet
  route) on `:6443`.

The subnet routers add **full LAN reach** (`192.168.1.0/24`) so tailnet clients
can hit pihole, other devices, and — via `10.42.0.0/16` / `10.43.0.0/16` — cluster
Pod/Service IPs directly.

## Setup

### 1. Create the tailnet + auth key (TASK-052)

1. Create a free tailnet: <https://login.tailscale.com/start>.
2. Generate a **reusable, pre-approved** auth key (Settings → Keys → Generate
   auth key): ✅ Reusable, ✅ Pre-approved. Optionally tag it
   (`tag:k8s-node`) and add a matching ACL entry.
3. (Recommended) In the admin console, set an **autoApprovers** ACL for
   `192.168.1.0/24` so the subnet routes self-approve on re-register:

   ```json
   "autoApprovers": {
     "routes": { "192.168.1.0/24": ["tag:k8s-node"] }
   }
   ```

### 2. Enroll each node (TASK-053)

Run `install-node.sh` **on each node** (it detects the hostname and applies the
right flags — subnet router for manu/wemby, plain node for timmy):

```bash
# copy the repo (or just this script) to the node, then, as a sudo user:
TS_AUTHKEY=tskey-auth-xxxx ./tailscale/install-node.sh
```

The script installs tailscale, enables IP forwarding on the router nodes, and runs
`tailscale up --ssh --accept-routes [--advertise-routes=...]`. It's idempotent.

> The auth key is passed at runtime (env var) — it is **not** stored in the repo.
> `**/*secret*.yaml` and any local key files stay gitignored.

### 3. Approve routes + verify (TASK-054)

In the admin console, approve `192.168.1.0/24` (and the cluster CIDRs if used) on
**both** manu and wemby (Machines → node → Edit route settings) — unless
autoApprovers handles it. Then from an **off-LAN** device on the tailnet
(`--accept-routes` enabled):

```bash
tailscale ping manu          # node reachable
ping 192.168.1.<pihole>      # LAN reachable via the subnet route
```

## Usage

### kubectl over the tailnet (TASK-055)

Two options:

- **Via the subnet route (no cluster change):** target a node's **LAN** IP —
  `https://192.168.1.<node>:6443`. The LAN IP is already a K3s cert SAN.
- **Via the node's tailnet IP/MagicDNS (cleaner long-term):** add the tailnet IP
  or MagicDNS name to the API-server cert SANs with the K3s `--tls-san` flag
  (`/etc/rancher/k3s/config.yaml`), restart k3s so the cert regenerates, then
  point kubeconfig at `https://<node>.<tailnet>.ts.net:6443`.

```bash
kubectl --server https://192.168.1.<node>:6443 get nodes
```

### SSH over the tailnet (TASK-056)

```bash
ssh <user>@<node>            # standard key-based SSH over the tailnet, or
ssh <user>@<node>.<tailnet>.ts.net   # via MagicDNS; Tailscale SSH if ACL-allowed
```

## Files

| File              | Purpose                                                          |
| ----------------- | ---------------------------------------------------------------- |
| `install-node.sh` | Host-level installer/enroller; role-aware (router vs plain node) |
| `README.md`       | This runbook                                                     |

## Alternatives considered

- **Mechanism — Cloudflare Tunnel vs Tailscale.** Cloudflare is an L7
  per-hostname proxy: awkward TCP-over-`cloudflared access` for kubectl, no
  network/LAN-level reach, a wrapper per SSH client. Rejected for this goal.
  Tailscale (WireGuard mesh) gives native kubectl/SSH/subnet routing. Kept
  Cloudflare for public web.
- **Deployment — in-cluster subnet router (Flavor A) vs host-level (Flavor B).**
  Flavor A (one GitOps-managed Deployment) is cleaner for the repo but a SPOF
  that dies with the cluster — useless for break-glass. Flavor B (host-level,
  this) matches the existing pihole/unbound bare-metal model and survives cluster
  outages. Chosen after initially drafting Flavor A.
