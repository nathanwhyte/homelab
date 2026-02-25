#!/usr/bin/env bash
# Reset Grafana admin password using the Grafana CLI inside the cluster.
# See: https://grafana.com/docs/grafana/latest/administration/cli/#reset-admin-password
#
# Usage:
#   GRAFANA_NEW_PASSWORD='your-new-password' ./reset-admin-password.sh
#   ./reset-admin-password.sh   # will prompt for password (recommended)

set -e

NAMESPACE="${GRAFANA_NAMESPACE:-grafana}"

if [ ! -x "$(command -v kubectl)" ]; then
    echo "kubectl not installed."
    exit 1
fi

if ! kubectl cluster-info > /dev/null 2>&1; then
    echo "kubectl not connected to a cluster."
    exit 1
fi

DEPLOY=$(kubectl get deploy -n "$NAMESPACE" -l app.kubernetes.io/name=grafana -o jsonpath='{.items[0].metadata.name}' 2>/dev/null) || true
if [ -z "$DEPLOY" ]; then
    echo "No Grafana deployment found in namespace $NAMESPACE."
    exit 1
fi

if [ -n "$GRAFANA_NEW_PASSWORD" ]; then
    PASSWORD="$GRAFANA_NEW_PASSWORD"
else
    echo "Enter new admin password (input hidden):"
    read -rs PASSWORD
    echo
    if [ -z "$PASSWORD" ]; then
        echo "Password cannot be empty."
        exit 1
    fi
    echo "Confirm new admin password:"
    read -rs PASSWORD2
    echo
    if [ "$PASSWORD" != "$PASSWORD2" ]; then
        echo "Passwords do not match."
        exit 1
    fi
fi

echo "Resetting admin password for Grafana deployment: $DEPLOY"
kubectl exec -n "$NAMESPACE" "deployment/$DEPLOY" -- \
    grafana cli admin reset-admin-password "$PASSWORD"

echo "Admin password has been reset. You can log in with username 'admin' and the new password."
