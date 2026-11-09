#!/usr/bin/env bash
set -euo pipefail
# Install the kubelet container-log drop-in on this node and restart k3s
# (BUG-1293). Run this script ON the node, from a homelab checkout, with sudo:
#   ./kube-system/install-kubelet-container-logs.sh [--data-dir /var/lib/rancher/k3s] [--dry-run]
#
# k3s starts the kubelet with --config-dir=<data-dir>/agent/etc/kubelet.conf.d
# and rewrites only 00-k3s-defaults.conf there on each start
# (pkg/daemons/agent/agent.go:157-163, agent_linux.go:76-77@v1.36.2+k3s1), so a
# 50- file survives restarts and overrides the defaults (drop-ins merge in
# lexical order). The kubelet reads it only at startup, hence the restart.
# Containers keep running through a k3s restart (KillMode=process).
#
# Restart order across the cluster: AGENTS FIRST (wemby, manu), SERVER LAST
# (timmy) — restarting the server briefly interrupts the API.
#
# Idempotent: when the installed file already matches, it reports "already
# installed" and does not restart k3s.

data_dir=/var/lib/rancher/k3s
dry_run=0
while [ $# -gt 0 ]; do
  case "$1" in
    --data-dir) data_dir="$2"; shift 2 ;;
    --dry-run) dry_run=1; shift ;;
    *) echo "usage: $0 [--data-dir <k3s data dir>] [--dry-run]" >&2; exit 2 ;;
  esac
done

src="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/kubelet-container-logs.conf"
[ -f "$src" ] || { echo "missing $src" >&2; exit 1; }

# --- which unit runs here ---------------------------------------------------
unit=""
for u in k3s k3s-agent; do
  if systemctl cat "$u" >/dev/null 2>&1; then unit="$u"; break; fi
done
[ -n "$unit" ] || { echo "neither k3s nor k3s-agent is installed on $(hostname)" >&2; exit 1; }

conf_dir="$data_dir/agent/etc/kubelet.conf.d"
sudo test -d "$conf_dir" ||
  { echo "$conf_dir does not exist; pass --data-dir if k3s uses another data dir" >&2; exit 1; }
dest="$conf_dir/50-container-logs.conf"
echo ">> $(hostname): unit=$unit dest=$dest"

if sudo test -f "$dest" && sudo cmp -s "$src" "$dest"; then
  echo ">> already installed — nothing to do"
  exit 0
fi

if [ "$dry_run" = 1 ]; then
  echo ">> would install $src as $dest and restart $unit"
  exit 0
fi

# --- install ----------------------------------------------------------------
sudo install -m 0600 -o root -g root "$src" "$dest"
echo ">> installed $dest:"
sudo cat "$dest" | sed 's/^/     /'

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

echo ">> live kubelet log settings:"
sudo k3s kubectl get --raw "/api/v1/nodes/$(hostname)/proxy/configz" 2>/dev/null |
  grep -o '"containerLogMax[A-Za-z]*":[^,]*' | sed 's/^/     /' ||
  echo "     (no kubeconfig on an agent — check from a workstation:" \
    "kubectl get --raw /api/v1/nodes/$(hostname)/proxy/configz | grep -o '\"containerLogMax[A-Za-z]*\":[^,]*')"
echo ">> expect containerLogMaxSize \"50Mi\" and containerLogMaxFiles 5"
