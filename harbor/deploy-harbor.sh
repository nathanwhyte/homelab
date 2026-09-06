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
DRY_RUN=0
HELM_DEPLOY="$REPO_ROOT/scripts/helm-deploy.py"
ADMIN_PASSWORD="${HARBOR_ADMIN_PASSWORD:-}"

usage() {
	cat <<EOF
Usage: $0 [--skip-probe-patch] [--diff | --dry-run] [--admin-password <pw>]

  --dry-run             Server-side Helm simulation only; no manifest applies or probe patch.

  --skip-probe-patch    Skip the automatic re-apply of the chart-v1.18.3
                        liveness/readiness probe-timeout patch after helm upgrade.
                        Use this once chart >= 1.19.0 is in place and the
                        upstream timeout no longer needs overriding.
  --diff                Run \`helm diff upgrade\` instead of \`helm upgrade\`.
                        Requires the helm-diff plugin. No manifest applies;
                        propagates plugin errors. Review the diff yourself.
  --admin-password <pw> Pass a real password to the chart's
                        \`harborAdminPassword\` value via \`--set\`. Keeps the
                        secret out of git. If omitted, falls back to
                        \$HARBOR_ADMIN_PASSWORD in the environment; if both
                        are empty, the chart uses its default (the literal
                        string "<CHANGE_ME>").
  -h, --help            Show this help.
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
	--admin-password)
		[ $# -ge 2 ] || {
			echo "--admin-password requires a value" >&2
			exit 2
		}
		ADMIN_PASSWORD="$2"
		shift 2
		;;
	-h | --help)
		usage
		exit 0
		;;
	--dry-run)
		DRY_RUN=1
		shift
		;;
	*)
		echo "Unknown flag: $1" >&2
		usage >&2
		exit 2
		;;
	esac
done

if [ "$DRY_RUN" -eq 1 ] && [ "$HELM_DIFF" -eq 1 ]; then
	echo "Choose --dry-run or --diff" >&2
	exit 2
fi

# Both preflight modes exit before namespace/storage applies or probe patches.
if [ "$DRY_RUN" -eq 1 ] || [ "$HELM_DIFF" -eq 1 ]; then
	HELM_PREFLIGHT_ARGS=(-f "$VALUES_FILE")
	if [ -n "$ADMIN_PASSWORD" ]; then
		HELM_PREFLIGHT_ARGS+=(--set "harborAdminPassword=$ADMIN_PASSWORD")
	fi
	if [ "$DRY_RUN" -eq 1 ]; then
		HELM_PREFLIGHT_ARGS+=(--dry-run)
	else
		HELM_PREFLIGHT_ARGS+=(--diff)
	fi
	python3 "$HELM_DEPLOY" "$RELEASE_NAME" "$NAMESPACE" "$CHART_REF" "${HELM_PREFLIGHT_ARGS[@]}"
	exit 0
fi

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

# Hard gate: --admin-password (or $HARBOR_ADMIN_PASSWORD) is required for any
# non-preflight run. The Harbor chart's harborAdminPassword value only writes
# the harbor_user DB row on first install; on every subsequent upgrade the
# chart only re-renders the harbor-core K8s Secret. If we run without an
# override, the chart re-applies harbor-values.yaml's literal "<CHANGE_ME>"
# placeholder and silently overwrites whatever the operator rotated it to.
# See harbor/HARBOR-RUNBOOK.md § "Auth: admin password" for the algorithm,
# reset procedure, and the "DB and Secret can drift" gotcha this gate exists
# to prevent. The --diff and --dry-run preflight paths are intentionally
# exempt (they should work against unmodified values without a password);
# both exit above, so only real deploys reach this gate.
if [ -z "$ADMIN_PASSWORD" ]; then
	cat >&2 <<EOF
ERROR: --admin-password (or \$HARBOR_ADMIN_PASSWORD) is required.

The Harbor chart's harborAdminPassword value only writes the harbor_user
DB row on first install; on every subsequent upgrade the chart only
re-renders the harbor-core K8s Secret. If this script is run without
--admin-password after a manual DB rotation, the chart will silently
overwrite the Secret back to the value in harbor-values.yaml (still the
literal placeholder "<CHANGE_ME>") and you'll be locked out.

To rotate: pick a new password, run the DB UPDATE procedure in
harbor/HARBOR-RUNBOOK.md § "Auth: admin password", then re-run this
script with the same password:

  ./harbor/deploy-harbor.sh --admin-password '\$NEW_PW'

(--diff preflight still works without a password.)
EOF
	exit 2
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

HELM_SET_ARGS=()
if [ -n "$ADMIN_PASSWORD" ]; then
	HELM_SET_ARGS+=(--set "harborAdminPassword=$ADMIN_PASSWORD")
fi

python3 "$HELM_DEPLOY" "$RELEASE_NAME" "$NAMESPACE" \
	"$CHART_REF" \
	--create-namespace \
	--wait \
	--timeout "$TIMEOUT" \
	-f "$VALUES_FILE" \
	"${HELM_SET_ARGS[@]}"

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
if [ -n "$ADMIN_PASSWORD" ]; then
	echo "  Admin user: admin (password was passed via --admin-password / \$HARBOR_ADMIN_PASSWORD — not echoed for safety)"
else
	echo "  Default credentials: admin / <CHANGE_ME>  (set --admin-password or \$HARBOR_ADMIN_PASSWORD before running)"
fi
echo "  Docker login: docker login registry.nathanwhyte.dev"

echo -e "\nCheck status:"
echo "  kubectl get pods -n $NAMESPACE"
echo "  kubectl get ingress -n $NAMESPACE"
echo "  kubectl get certificate -n $NAMESPACE"
