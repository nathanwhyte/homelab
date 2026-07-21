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

# BasicAuth middleware + the tailnet-hostname Ingress (IMPR-1020).
#
# An ipAllowList middleware lived here until 2026-07-21 and filtered nothing —
# klipper-lb masquerades the client address before Traefik sees it, so it matched
# a node IP on every request (BUG-1057). It was replaced with BasicAuth, which
# checks the Authorization header and is therefore immune to that rewrite.
#
# tailnet-ingress.yaml gives timmy's tailnet hostname its own router carrying the
# same middleware. Without it, the `tailscale serve` route reaches Traefik with a
# Host header that matches no rule and 404s — and before it existed, that route
# bypassed Traefik entirely and served Longhorn with no auth at all.
echo -e "\nApplying Longhorn UI BasicAuth middleware and tailnet Ingress..."
kubectl apply -f "$LONGHORN_DIR/middleware.yaml"
kubectl apply -f "$LONGHORN_DIR/tailnet-ingress.yaml"

echo -e "\nDone!"
echo "  Web UI (LAN / tailnet-routed, via Traefik on 192.168.1.19): https://longhorn.nathanwhyte.dev"
echo "  Auth: BasicAuth via the 'longhorn-basicauth' middleware (IMPR-1020)."
echo "        Credentials live in secret 'longhorn-auth-secret' (user: admin)."
echo "        Longhorn has no native auth, so this middleware is the only thing"
echo "        between a client and a console that can delete volumes."
echo "        Rotate: see longhorn/longhorn-auth-secret.yaml.example."
echo "        Verify with the NEGATIVE test (no creds must return 401):"
echo "          curl -so /dev/null -w '%{http_code}' https://longhorn.nathanwhyte.dev/"
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
