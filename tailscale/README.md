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
can hit pihole, other devices, and cluster services via their LAN IPs. Cluster
CIDRs (`10.42.0.0/16`, `10.43.0.0/16`) were dropped from advertised routes
(2026-06-12) — they're reachable via the LAN route anyway, and advertising them
risked collision with other tailnets.

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
   // Cluster CIDRs were dropped from advertised routes (2026-06-12)
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
`tailscale up --ssh [--advertise-routes=...]`. It's idempotent.

> **Do not `--accept-routes` on the nodes.** All three are physically on
> `192.168.1.0/24`; if a node accepts that advertised route it sends LAN replies
> out `tailscale0` instead of its NIC, which silently breaks direct SSH / kubectl
> / ICMP from on-LAN clients (the underlay UDP still works, so `tailscale ping`
> and tailnet-IP SSH keep working — a confusing half-broken state). `--accept-routes`
> belongs only on **off-LAN client devices** (phone/laptop while traveling) that
> need to reach the LAN through the subnet router.

> The auth key is passed at runtime (env var) — it is **not** stored in the repo.
> `**/*secret*.yaml` and any local key files stay gitignored.

### 3. Approve routes + verify (TASK-054)

In the admin console, approve `192.168.1.0/24` on
**both** manu and wemby (Machines → node → Edit route settings) — unless
autoApprovers handles it. Then from an **off-LAN** device on the tailnet
(`--accept-routes` enabled):

```bash
tailscale ping manu          # node reachable
ping 192.168.1.<pihole>      # LAN reachable via the subnet route
```

## Usage

### kubectl over the tailnet (TASK-055)

Two contexts in `~/.kube/config`:

| Context | Server | Path | Requires |
|---------|--------|------|----------|
| `homelab` | `192.168.1.9:6443` | LAN IP via subnet route | `--accept-routes` on client |
| `tailnet` | `100.118.40.21:6443` | Direct tailnet IP | `--tls-san` on wemby (done) |

```bash
kubectl --context homelab get nodes   # via subnet route
kubectl --context tailnet get nodes   # via tailnet IP
```

The `tailnet` context requires `--tls-san 100.118.40.21` in
`/etc/rancher/k3s/config.yaml` on wemby (already added 2026-06-12)
so the API server cert is valid for the tailnet IP. The CA cert is
stored at `~/.kube/k3s-ca.crt`.

### SSH over the tailnet (TASK-056)

```bash
ssh <user>@<node>            # standard key-based SSH over the tailnet, or
ssh <user>@<node>.<tailnet>.ts.net   # via MagicDNS; Tailscale SSH if ACL-allowed
```

> **wemby user is `natew`, not `noot`.** The `tailscale ssh` CLI defaults to
> your local MacBook username (`noot`), which matches manu and timmy but not
> wemby. Use `tailscale ssh natew@wemby` or `ssh natew@100.118.40.21`.

## Files

| File              | Purpose                                                          |
| ----------------- | ---------------------------------------------------------------- |
| `install-node.sh` | Host-level installer/enroller; role-aware (router vs plain node) |
| `README.md`       | This runbook                                                     |

## Future: Tailscale Kubernetes Operator (complementary)

The [Tailscale Kubernetes Operator](https://tailscale.com/docs/kubernetes-operator) is a Helm-installed, in-cluster option that adds **workload-level** networking — declarative per-service tailnet exposure and egress. It does **not** replace host-level Tailscale (it dies with the cluster, so break-glass access still requires host-level).

| Operator feature | What it does                                                            | Why it's interesting                                              |
| ---------------- | ----------------------------------------------------------------------- | ----------------------------------------------------------------- |
| L7 Ingress       | `Ingress` with `ingressClassName: tailscale` → `*.ts.net` with auto-TLS | Replace some Cloudflare Tunnel routes for private services        |
| L3 Ingress       | Annotate Service `tailscale.com/expose: "true"` → tailnet IP            | Selective exposure instead of advertising entire CIDR             |
| Egress           | `Connector` CR → pods reach tailnet services                            | Cluster pods (e.g. hermes-agent) reaching off-LAN tailnet devices |
| API Server Proxy | `tailscale configure kubeconfig` → identity-based kubectl               | Audit trail via impersonation headers                             |
| Funnel           | Annotate `tailscale.com/funnel: "true"` → public endpoint               | Replace Cloudflare for services that don't need WAF/caching       |

See PROJ-008 (2026-06-13 note) for the full research findings and prioritization.

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
