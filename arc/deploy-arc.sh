#!/usr/bin/env bash

# Deploy actions-runner-controller + runner scale sets (IDEA-1094).
#
# Two-layer install from OCI charts:
#   1. gha-runner-scale-set-controller  -> release `arc`, ns arc-systems
#   2. gha-runner-scale-set (per repo)  -> release `compendium`, ns arc-runners
#
# Prereqs (one-time, documented in ARC.md):
#   - arc/github-config-secret.yaml applied (copy the .example, fill the PAT)
#   - Harbor project `ci` exists and the runner image is pushed
#     (images/runner/build-push.sh)

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# Controller and scale-set chart versions should match (patch mismatch is
# tolerated since 0.12, but there's no reason to skew them here).
CHART_VERSION="${ARC_CHART_VERSION:-0.14.2}"
CONTROLLER_CHART="oci://ghcr.io/actions/actions-runner-controller-charts/gha-runner-scale-set-controller"
SCALE_SET_CHART="oci://ghcr.io/actions/actions-runner-controller-charts/gha-runner-scale-set"
TIMEOUT="5m"
HELM_DIFF=0

usage() {
	cat <<EOF
Usage: $0 [--diff]

  --diff       Run \`helm diff upgrade\` for both releases instead of applying.
               Requires the helm-diff plugin.
  -h, --help   Show this help.

Chart version is pinned via \$ARC_CHART_VERSION (default: $CHART_VERSION).
EOF
}

while [ $# -gt 0 ]; do
	case "$1" in
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

for tool in kubectl helm; do
	if [ ! -x "$(command -v "$tool")" ]; then
		echo "$tool not installed." >&2
		exit 1
	fi
done

if ! kubectl cluster-info >/dev/null 2>&1; then
	echo "kubectl not connected to a cluster." >&2
	exit 1
fi

kubectl apply -f "$SCRIPT_DIR/namespaces.yaml"

# The scale set cannot register without its GitHub credential; fail fast with
# instructions rather than letting the listener crashloop.
if ! kubectl get secret github-config-secret -n arc-runners >/dev/null 2>&1; then
	cat >&2 <<EOF
ERROR: secret arc-runners/github-config-secret not found.

Copy arc/github-config-secret.yaml.example to arc/github-config-secret.yaml,
fill in a fine-grained PAT (Administration: Read and write on the target
private repos), then:

  kubectl apply -f arc/github-config-secret.yaml

The filled copy is gitignored (**/*secret*.yaml) — do not commit it.
EOF
	exit 2
fi

helm_apply() {
	local release="$1" chart="$2" namespace="$3" values="$4"
	if [ "$HELM_DIFF" -eq 1 ]; then
		if ! helm plugin list 2>/dev/null | grep -q '^diff\s'; then
			echo "ERROR: --diff requested but the helm-diff plugin is not installed." >&2
			echo "  Install with: helm plugin install https://github.com/databus23/helm-diff" >&2
			exit 1
		fi
		echo -e "\nhelm diff upgrade $release (informational)..."
		helm diff upgrade "$release" "$chart" \
			--version "$CHART_VERSION" \
			--namespace "$namespace" \
			--allow-unreleased \
			-f "$values" || true
	else
		echo -e "\nDeploying $release ($chart @ $CHART_VERSION)..."
		helm upgrade --install "$release" "$chart" \
			--version "$CHART_VERSION" \
			--namespace "$namespace" \
			--wait \
			--timeout "$TIMEOUT" \
			-f "$values"
	fi
}

helm_apply arc "$CONTROLLER_CHART" arc-systems "$SCRIPT_DIR/controller-values.yaml"
helm_apply compendium "$SCALE_SET_CHART" arc-runners "$SCRIPT_DIR/runner-scale-set-compendium-values.yaml"

if [ "$HELM_DIFF" -eq 0 ]; then
	echo -e "\nDone. Check status:"
	echo "  kubectl get pods -n arc-systems"
	echo "  kubectl get pods -n arc-runners        # listener idles here; runner pods appear per job"
	echo "  gh api repos/nathanwhyte/compendium/actions/runners --jq '.runners[].name'"
	echo -e "\nWorkflows target the runners with: runs-on: homelab-arc"
fi
