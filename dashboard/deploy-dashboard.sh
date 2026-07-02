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

kubectl apply -f "$DASHBOARD_DIR/ingress.yaml" -f "$DASHBOARD_DIR/user.yaml" -f "$DASHBOARD_DIR/middleware.yaml"

echo -e "\nDone! Visit (Tailscale/LAN only, via Traefik on 192.168.1.19):"
echo "  https://k8s.nathanwhyte.dev/#/overview?namespace=kubernetes-dashboard"
