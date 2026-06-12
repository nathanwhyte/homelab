#!/usr/bin/env bash
#
# Install + enroll a K3s node onto the Tailscale tailnet (host-level, NOT in-cluster).
#
# Design (PROJ-008 / TASK-053):
#   - Tailscale runs on the node OS, like pihole + unbound — decoupled from K3s so
#     remote SSH/kubectl survives a cluster outage (the moment you need it most).
#   - All 3 nodes join the tailnet with Tailscale SSH enabled (keyless, ACL-gated).
#   - manu + wemby additionally advertise the LAN as HA subnet routers (Tailscale
#     auto-fails-over between them, mirroring the redundant pihole/unbound pair).
#     timmy is a plain tailnet node.
#
# Usage (run ON the node, as a user with sudo):
#   TS_AUTHKEY=tskey-auth-xxxx ./install-node.sh
#   # or omit TS_AUTHKEY to authenticate interactively via the printed URL:
#   ./install-node.sh
#
# Idempotent: re-running re-applies `tailscale up` with the current flags.
set -euo pipefail

# --- config -----------------------------------------------------------------
# Nodes that act as subnet routers (advertise the LAN + cluster CIDRs).
ROUTER_NODES=("manu" "wemby")

# Routes advertised by the router nodes. 192.168.1.0/24 is the must-have (full
# LAN reach incl. pihole/unbound). Cluster CIDRs (10.42.0.0/16, 10.43.0.0/16)
# were dropped 2026-06-12 to reduce blast radius — they're reachable via the LAN
# route anyway, and advertising them risked collision with other tailnets.
ADVERTISE_ROUTES="192.168.1.0/24"
# ----------------------------------------------------------------------------

node="$(hostname -s)"
echo ">> Configuring Tailscale on node: ${node}"

is_router=false
for r in "${ROUTER_NODES[@]}"; do
	if [[ "${node}" == "${r}" ]]; then
		is_router=true
		break
	fi
done

# 1. Install tailscale (official repo script; no-op if already installed).
if ! command -v tailscale >/dev/null 2>&1; then
	echo ">> Installing tailscale..."
	curl -fsSL https://tailscale.com/install.sh | sh
else
	echo ">> tailscale already installed: $(tailscale version | head -1)"
fi

# 2. Build the `tailscale up` argument list.
# NOTE: deliberately NO --accept-routes. All cluster nodes are physically on the
# advertised subnet (192.168.1.0/24); if a node accepts that route it sends LAN
# replies out tailscale0 instead of its NIC, breaking direct SSH/kubectl/ICMP
# from on-LAN clients (return-path blackhole). --accept-routes belongs only on
# OFF-LAN client devices (phone/laptop while traveling).
up_args=(--ssh "--hostname=${node}")
if [[ -n "${TS_AUTHKEY:-}" ]]; then
	up_args+=("--authkey=${TS_AUTHKEY}")
fi

if [[ "${is_router}" == true ]]; then
	echo ">> ${node} is a SUBNET ROUTER — enabling IP forwarding + advertising routes."
	# Subnet routing requires kernel IP forwarding.
	sudo tee /etc/sysctl.d/99-tailscale.conf >/dev/null <<-'EOF'
		net.ipv4.ip_forward = 1
		net.ipv6.conf.all.forwarding = 1
	EOF
	sudo sysctl -p /etc/sysctl.d/99-tailscale.conf
	up_args+=("--advertise-routes=${ADVERTISE_ROUTES}")
else
	echo ">> ${node} is a plain tailnet node (SSH only, no --accept-routes — see line 52 comment)."
fi

# 3. Bring it up.
echo ">> Running: sudo tailscale up ${up_args[*]//--authkey=*/--authkey=***}"
sudo tailscale up "${up_args[@]}"

echo
echo ">> Done. Tailnet status:"
tailscale status || true
echo
if [[ "${is_router}" == true ]]; then
	echo ">> NEXT: approve the advertised routes in the Tailscale admin console"
	echo "   (Machines -> ${node} -> Edit route settings), then verify off-LAN."
	echo "   With two router nodes (manu + wemby), approve the routes on BOTH for HA."
fi
