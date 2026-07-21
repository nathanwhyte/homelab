#!/usr/bin/env bash

DASHBOARD_DIR="$HOME/code/homelab/dashboard"

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

echo "Updating helm repositories..."
helm repo add kubernetes-dashboard https://kubernetes.github.io/dashboard/ 2>/dev/null || true
helm repo update kubernetes-dashboard

echo "Deploying kubernetes-dashboard..."
helm upgrade --install kubernetes-dashboard \
	kubernetes-dashboard/kubernetes-dashboard \
	--create-namespace --namespace kubernetes-dashboard

echo -e "\nApplying kubernetes-dashboard manifests..."

if [ ! -f "$DASHBOARD_DIR/ingress.yaml" ]; then
	echo "ingress.yaml file not found!"
	exit 1
fi

if [ ! -f "$DASHBOARD_DIR/user.yaml" ]; then
	echo "user.yaml file not found!"
	exit 1
fi

if [ ! -f "$DASHBOARD_DIR/middleware.yaml" ]; then
	echo "middleware.yaml file not found!"
	exit 1
fi

if [ ! -f "$DASHBOARD_DIR/serverstransport.yaml" ]; then
	echo "serverstransport.yaml file not found!"
	exit 1
fi

kubectl apply -f "$DASHBOARD_DIR/ingress.yaml" -f "$DASHBOARD_DIR/user.yaml" \
	-f "$DASHBOARD_DIR/middleware.yaml" -f "$DASHBOARD_DIR/serverstransport.yaml"

# The chart's Service must point Traefik at the ServersTransport above, or every
# request 500s on "x509: cannot validate certificate ... no IP SANs" (Kong
# serves TLS with a self-signed cert; Traefik dials it by pod IP). The Service is
# chart-managed and the chart exposes no values hook for this annotation, so it
# is applied here after the upgrade. Idempotent.
echo -e "\nPointing the Kong Service at the ServersTransport..."
kubectl annotate svc kubernetes-dashboard-kong-proxy -n kubernetes-dashboard \
	'traefik.ingress.kubernetes.io/service.serverstransport=kubernetes-dashboard-kong-transport@kubernetescrd' \
	--overwrite

echo -e "\nDone. Reachable over HTTP from the LAN or a Tailscale client, via"
echo "Traefik on 192.168.1.19 (or timmy's tailnet IP):"
echo "  http://k8s.nathanwhyte.dev/#/overview?namespace=kubernetes-dashboard"
echo
echo "NOTE: browsers cannot load that URL. '.dev' is HSTS-preloaded, so they"
echo "force HTTPS, and nothing serves TLS for this host (the Ingress is on the"
echo "'web' entrypoint with no spec.tls). curl works; a browser will not."
echo "See reference/external-routes.md for the options."
