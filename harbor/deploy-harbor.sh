#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
HARBOR_DIR="$SCRIPT_DIR"
LONGHORN_DIR="$REPO_ROOT/longhorn"
NAMESPACE="harbor"
RELEASE_NAME="harbor"
CHART_REF="harbor/harbor"
VALUES_FILE="$HARBOR_DIR/harbor-values.yaml"
TIMEOUT="10m"
SKIP_PROBE_PATCH=0
HELM_DIFF=0

usage() {
    cat <<EOF
Usage: $0 [--skip-probe-patch] [--diff]

  --skip-probe-patch  Skip the automatic re-apply of the chart-v1.18.3
                      liveness/readiness probe-timeout patch after helm upgrade.
                      Use this once chart >= 1.19.0 is in place and the
                      upstream timeout no longer needs overriding.
  --diff              Run \`helm diff upgrade\` instead of \`helm upgrade\`.
                      Requires the helm-diff plugin. Exits 0 if the diff
                      matches expectations (or the in-repo file is already
                      in sync), 1 otherwise.
  -h, --help          Show this help.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --skip-probe-patch)
            SKIP_PROBE_PATCH=1
            shift
            ;;
        --diff)
            HELM_DIFF=1
            shift
            ;;
        -h | --help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown flag: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

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
helm repo add harbor https://helm.goharbor.io
helm repo update harbor

echo -e "\nDeploying Harbor container registry..."

if [ ! -f "$VALUES_FILE" ]; then
    echo "harbor-values.yaml file not found!"
    exit 1
fi

if [ ! -f "$HARBOR_DIR/namespace.yaml" ]; then
    echo "namespace.yaml file not found!"
    exit 1
fi

if [ ! -f "$HARBOR_DIR/letsencrypt-issuer.yaml" ]; then
    echo "letsencrypt-issuer.yaml file not found!"
    exit 1
fi

if [ ! -f "$HARBOR_DIR/harbor-middleware.yaml" ]; then
    echo "harbor-middleware.yaml file not found!"
    exit 1
fi

if [ ! -f "$HARBOR_DIR/rwo-pvcs.yaml" ]; then
    echo "rwo-pvcs.yaml file not found!"
    exit 1
fi

kubectl apply -f "$LONGHORN_DIR/storage.yaml"
kubectl apply -f "$HARBOR_DIR/namespace.yaml"
kubectl apply -f "$HARBOR_DIR/letsencrypt-issuer.yaml"
kubectl apply -f "$HARBOR_DIR/harbor-middleware.yaml"
kubectl apply -f "$HARBOR_DIR/rwo-pvcs.yaml"

# helm-diff preflight: warn (but do not fail) if the plugin is missing or the
# diff is non-empty. Non-zero diffs are normal during intentional upgrades,
# so this is informational, not a hard gate.
if [ "$HELM_DIFF" -eq 1 ]; then
    if ! helm plugin list 2>/dev/null | grep -q '^diff\s'; then
        echo "ERROR: --diff requested but the helm-diff plugin is not installed." >&2
        echo "  Install with: helm plugin install https://github.com/databus23/helm-diff" >&2
        exit 1
    fi
    echo -e "\nRunning helm diff upgrade (informational)..."
    helm diff upgrade "$RELEASE_NAME" "$CHART_REF" \
        --namespace "$NAMESPACE" \
        -f "$VALUES_FILE" || true
    exit 0
fi

helm upgrade --install "$RELEASE_NAME" \
    "$CHART_REF" \
    --namespace "$NAMESPACE" \
    --create-namespace \
    --wait \
    --timeout "$TIMEOUT" \
    -f "$VALUES_FILE"

# Post-hook: re-apply the chart-v1.18.3 probe-timeout patch on harbor-core.
# This compensates for the chart hardcoding probe timeoutSeconds=1 (or
# omitting it) without values support. Remove once chart >= 1.19.0 is in
# place and the upstream timeout is sane; pass --skip-probe-patch to opt out
# in the meantime (e.g. for testing).
if [ "$SKIP_PROBE_PATCH" -eq 0 ]; then
    echo -e "\nApplying probe-timeout patch to harbor-core..."
    kubectl patch deployment harbor-core -n "$NAMESPACE" --type='json' -p='[{"op":"add","path":"/spec/template/spec/containers/0/livenessProbe/timeoutSeconds","value":5},{"op":"add","path":"/spec/template/spec/containers/0/readinessProbe/timeoutSeconds","value":5}]'
else
    echo -e "\nSkipping probe-timeout patch (--skip-probe-patch)."
fi

echo -e "\nDone! Visit:"
echo "  Web UI: https://registry.nathanwhyte.dev"
echo "  Default credentials: admin / <CHANGE_ME>"
echo "  Docker login: docker login registry.nathanwhyte.dev"

echo -e "\nCheck status:"
echo "  kubectl get pods -n $NAMESPACE"
echo "  kubectl get ingress -n $NAMESPACE"
echo "  kubectl get certificate -n $NAMESPACE"
