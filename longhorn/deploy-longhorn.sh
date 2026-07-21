#!/usr/bin/env bash
set -euo pipefail

LONGHORN_DIR="$(cd "$(dirname "$0")" && pwd)"
NAMESPACE="longhorn-system"
CHART_VERSION="1.12.0"

if ! command -v kubectl &>/dev/null; then
	echo "kubectl not installed."
	exit 1
fi

if ! kubectl cluster-info &>/dev/null; then
	echo "kubectl not connected to a cluster."
	exit 1
fi

if ! command -v helm &>/dev/null; then
	echo "helm not installed."
	exit 1
fi

echo "Deploying Longhorn v${CHART_VERSION} via Helm..."

# Ensure repo is available
helm repo add longhorn https://charts.longhorn.io 2>/dev/null || true
helm repo update longhorn

# Install or upgrade Longhorn
helm upgrade --install longhorn longhorn/longhorn \
	--namespace "$NAMESPACE" --create-namespace \
	--version "$CHART_VERSION" \
	-f "$LONGHORN_DIR/longhorn-values.yaml"

echo -e "\nWaiting for Longhorn Manager to be ready..."
kubectl wait --for=condition=ready pod -l app=longhorn-manager \
	-n "$NAMESPACE" --timeout=300s || true

echo -e "\nApplying custom storage classes..."
kubectl apply -f "$LONGHORN_DIR/storage.yaml"

if [ -f "$LONGHORN_DIR/r2-backup-target.secret.yaml" ]; then
	echo -e "\nApplying R2 backup target secret..."
	kubectl apply -f "$LONGHORN_DIR/r2-backup-target.secret.yaml"
else
	echo -e "\nR2 backup target secret not found."
	echo "  Backups will stay unavailable until the secret exists."
	echo "  Copy longhorn/r2-backup-target.secret.yaml.example to"
	echo "  longhorn/r2-backup-target.secret.yaml and fill in real R2 credentials,"
	echo "  or create the secret manually in $NAMESPACE."
fi

# The LAN-only ipAllowList middleware that used to be applied here was removed
# 2026-07-21 (BUG-1057). It filtered nothing — klipper-lb masquerades the client
# address before Traefik sees it, so the allowlist matched a node IP on every
# request. Access control lives at the tailnet/Cloudflare Access layer instead.

echo -e "\nDone!"
echo "  Web UI (LAN / tailnet-routed, via Traefik on 192.168.1.19): https://longhorn.nathanwhyte.dev"
echo "  NOTE: this UI is UNAUTHENTICATED. There is no BasicAuth middleware and no"
echo "        network-layer allowlist (BUG-1057 — an ipAllowList cannot filter on"
echo "        this path). The secret 'longhorn-auth-secret' exists but is wired to"
echo "        nothing; verified 2026-07-21, an unauthenticated GET returns 200."
echo "        LAN/tailnet reachability is the only control. The host is not"
echo "        publicly routable (unproxied A record -> private 192.168.1.19)."
echo "  Backup target: s3://longhorn-backups@auto/cluster/homelab-k3s/longhorn/"

echo -e "\nCheck status:"
echo "  kubectl get pods -n $NAMESPACE"
echo "  kubectl get storageclass | grep longhorn"
echo "  helm status longhorn -n $NAMESPACE"

echo -e "\nStorage classes:"
echo "  longhorn       (default, 1 replica)"
echo "  longhorn-hdd   (HDD, ethernet nodes, 1 replica)"
echo "  longhorn-ssd   (SSD, 1 replica)"
echo "  longhorn-nvme  (NVMe, 1 replica)"
echo "  longhorn-ethernet (ethernet nodes, 1 replica)"
echo "  longhorn-db    (SSD, 3 replicas)"
echo "  longhorn-harbor (HDD, ethernet, 2 replicas, NFS)"
