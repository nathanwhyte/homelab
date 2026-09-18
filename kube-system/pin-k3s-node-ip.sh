#!/usr/bin/env bash
set -euo pipefail
# Pin this node's k3s node-ip to its stable LAN IPv4 address (BUG-1152).
# Run this script ON the node, with sudo available.
#   ./pin-k3s-node-ip.sh [--ip 192.168.1.9] [--dry-run]
#
# Why: without `node-ip`, k3s detects the node's addresses once at startup and
# registers whatever it finds — including the DHCPv6 address from the ISP's
# delegated prefix. When the prefix rotates, the host drops that address while
# k3s keeps announcing it, and kubelet logs
#   "Failed to set some node status fields" err="failed to validate
#    secondaryNodeIP: node IP \"2605:…\" not found in the host's network interfaces"
# every ~10s until the k3s unit restarts. The cluster is IPv4-only
# (podCIDR 10.42.0.0/24), so the registered IPv6 address serves nothing.
#
# Restart order across the cluster: AGENTS FIRST (wemby, manu), SERVER LAST
# (timmy) — restarting the server briefly interrupts the API.
#
# Idempotent: re-running with the same IP reports "already pinned" and does not
# restart k3s. A different IP rewrites the key and restarts.

ip_addr=""
dry_run=0
while [ $# -gt 0 ]; do
  case "$1" in
    --ip) ip_addr="$2"; shift 2 ;;
    --dry-run) dry_run=1; shift ;;
    *) echo "usage: $0 [--ip <ipv4>] [--dry-run]" >&2; exit 2 ;;
  esac
done

# --- which unit runs here ---------------------------------------------------
unit=""
for u in k3s k3s-agent; do
  if systemctl cat "$u" >/dev/null 2>&1; then unit="$u"; break; fi
done
[ -n "$unit" ] || { echo "neither k3s nor k3s-agent is installed on $(hostname)" >&2; exit 1; }

# --- the address to pin -----------------------------------------------------
if [ -z "$ip_addr" ]; then
  # The LAN address this host uses to reach the LAN gateway range, not a
  # tailnet or container address.
  ip_addr="$(ip -4 -o addr show scope global |
    awk '{print $4}' | cut -d/ -f1 | grep -E '^192\.168\.1\.' | head -1)"
fi
case "$ip_addr" in
  192.168.1.*) ;;
  *) echo "refusing to pin '$ip_addr' — expected a 192.168.1.x LAN address; pass --ip explicitly" >&2; exit 1 ;;
esac
ip -4 -o addr show scope global | grep -q " ${ip_addr}/" ||
  { echo "$ip_addr is not on any interface of $(hostname)" >&2; exit 1; }

config=/etc/rancher/k3s/config.yaml
echo ">> $(hostname): unit=$unit ip=$ip_addr config=$config"

current=""
if [ -f "$config" ]; then
  current="$(grep -E '^node-ip:' "$config" | head -1 | sed -e 's/^node-ip:[[:space:]]*//' -e 's/"//g' || true)"
fi
if [ "$current" = "$ip_addr" ]; then
  echo ">> already pinned to $ip_addr — nothing to do"
  exit 0
fi

if [ "$dry_run" = 1 ]; then
  echo ">> would set node-ip: $ip_addr in $config (currently '${current:-unset}') and restart $unit"
  exit 0
fi

# --- edit the config --------------------------------------------------------
sudo mkdir -p "$(dirname "$config")"
if [ -f "$config" ]; then
  backup="$config.bak.$(date +%Y%m%d%H%M%S)"
  sudo cp "$config" "$backup"
  echo ">> backed up $config to $backup"
  if [ -n "$current" ]; then
    sudo sed -i "s|^node-ip:.*|node-ip: $ip_addr|" "$config"
  else
    printf 'node-ip: %s\n' "$ip_addr" | sudo tee -a "$config" >/dev/null
  fi
else
  printf 'node-ip: %s\n' "$ip_addr" | sudo tee "$config" >/dev/null
fi
echo ">> $config now reads:"
sudo cat "$config" | sed 's/^/     /'

# --- restart and verify -----------------------------------------------------
echo ">> restarting $unit (the API is briefly unavailable if this is the server)"
sudo systemctl restart "$unit"

for _ in $(seq 1 60); do
  if systemctl is-active --quiet "$unit"; then break; fi
  sleep 2
done
systemctl is-active --quiet "$unit" || { echo "$unit did not come back active" >&2; exit 1; }

echo ">> waiting for the node object to report Ready"
for _ in $(seq 1 60); do
  if sudo k3s kubectl get node "$(hostname)" \
    -o jsonpath='{range .status.conditions[?(@.type=="Ready")]}{.status}{end}' 2>/dev/null | grep -q True; then
    break
  fi
  sleep 5
done

echo ">> registered addresses:"
sudo k3s kubectl get node "$(hostname)" \
  -o jsonpath='{range .status.addresses[*]}     {.type}={.address}{"\n"}{end}' 2>/dev/null ||
  echo "     (no kubeconfig on an agent — check from the server: kubectl get node $(hostname) -o wide)"

sleep 30
noise="$(journalctl -u "$unit" --since "1 min ago" --no-pager | grep -c secondaryNodeIP || true)"
echo ">> secondaryNodeIP errors in the last minute: $noise (expect 0)"
