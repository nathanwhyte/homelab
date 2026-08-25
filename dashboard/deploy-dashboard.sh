#!/usr/bin/env bash
set -euo pipefail

# Resolve from this script's own location. The repo is bare with sibling
# worktrees, so a hardcoded ~/code/homelab/dashboard does not exist.
DASHBOARD_DIR="${DASHBOARD_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
NAMESPACE="kubernetes-dashboard"

# The upstream kubernetes/dashboard repo was archived; kubernetes.github.io/dashboard
# now 404s. The chart index moved to the retired namespace, which still carries
# 7.14.0 (the version deployed here). Verified 2026-07-20.
CHART_REPO_URL="https://kubernetes-retired.github.io/dashboard"

if [ ! -x "$(command -v "kubectl")" ]; then
	echo "kubectl not installed."
	exit 1
fi

if ! kubectl cluster-info >/dev/null 2>&1; then
	echo "kubectl not connected to a cluster."
	exit 1
fi

if [ ! -x "$(command -v "helm")" ]; then
	echo "helm not installed."
	exit 1
fi

for f in ingress.yaml user.yaml middleware.yaml serverstransport.yaml; do
	if [ ! -f "$DASHBOARD_DIR/$f" ]; then
		echo "$f not found in $DASHBOARD_DIR!"
		exit 1
	fi
done

echo "Updating helm repositories..."
# Not suppressed: a dead repo URL must fail here rather than let the upgrade
# silently proceed against a stale cached index.
helm repo add kubernetes-dashboard "$CHART_REPO_URL" --force-update
helm repo update kubernetes-dashboard

echo -e "\nDeploying kubernetes-dashboard..."
# The Kong proxy Service needs the ServersTransport annotation, or every request
# 500s on "x509: cannot validate certificate ... no IP SANs" — Kong serves TLS
# with a self-signed cert and Traefik dials it by pod IP.
#
# Set through chart values (kong.proxy.annotations, available in 7.14.0) so it
# lands in the Helm-owned Service. A post-upgrade `kubectl annotate` would be
# stripped by any `helm upgrade` run outside this script, silently restoring the
# 500 until the script was re-run.
#
# The ref is <namespace>-<name>@kubernetescrd — that is how Traefik's
# kubernetescrd provider namespaces CRDs, so `kong-transport` in namespace
# `kubernetes-dashboard` is addressed as `kubernetes-dashboard-kong-transport`.
# It is NOT a bare metadata.name.
helm upgrade --install kubernetes-dashboard \
	kubernetes-dashboard/kubernetes-dashboard \
	--create-namespace --namespace "$NAMESPACE" \
	--set "kong.proxy.annotations.traefik\.ingress\.kubernetes\.io/service\.serverstransport=kubernetes-dashboard-kong-transport@kubernetescrd"

echo -e "\nApplying kubernetes-dashboard manifests..."
kubectl apply -f "$DASHBOARD_DIR/ingress.yaml" -f "$DASHBOARD_DIR/user.yaml" \
	-f "$DASHBOARD_DIR/middleware.yaml" -f "$DASHBOARD_DIR/serverstransport.yaml"

# `kubectl apply` never deletes resources dropped from an input file, so a
# cluster last deployed before 2026-07-20 still carries this IngressRouteTCP.
# It declared HostSNI() on `websecure` with no tls: block, which Traefik rejects
# outright — it routed nothing and only emitted repeating "invalid rule" errors.
# Idempotent; a no-op on clusters that never had it.
kubectl delete ingressroutetcp kubernetes-dashboard-tcp -n "$NAMESPACE" \
	--ignore-not-found

echo -e "\nDone. Reachable over HTTPS from the LAN or a Tailscale client, via"
echo "Traefik on 192.168.1.19 (or timmy's tailnet IP):"
echo "  https://k8s.nathanwhyte.dev/#/overview?namespace=kubernetes-dashboard"
echo
echo "NOTE (2026-08-24, PROJ-1018): the public k8s.nathanwhyte.dev A record was"
echo "DELETED. The name now resolves only via the Pi-hole local records in"
echo "network/pihole/ on LAN, and Tailscale split DNS off-LAN. Nothing in"
echo "Cloudflare needs creating; this script does not manage DNS."
echo "cert-manager issues the TLS cert via DNS-01, so the host does not need to"
echo "be publicly reachable for that to succeed."
