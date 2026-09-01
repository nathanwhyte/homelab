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
#
# NOTE: this script INSTALLS and re-applies values at one pinned chart
# version. It deliberately refuses in-place chart VERSION bumps: Helm does not
# upgrade CRDs, so ARC upgrades require the uninstall/reinstall procedure in
# ARC.md § "Chart upgrades".

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# Controller and scale-set chart versions must match (ARC.md § Chart upgrades).
CHART_VERSION="${ARC_CHART_VERSION:-0.14.2}"
CONTROLLER_CHART="oci://ghcr.io/actions/actions-runner-controller-charts/gha-runner-scale-set-controller"
SCALE_SET_CHART="oci://ghcr.io/actions/actions-runner-controller-charts/gha-runner-scale-set"
TIMEOUT="5m"
HELM_DIFF=0

usage() {
	cat <<EOF
Usage: $0 [--diff]

  --diff       Read-only preflight: run \`helm diff upgrade\` for both releases
               without applying anything (no namespaces, no secret checks).
               Requires the helm-diff plugin.
  -h, --help   Show this help.

Chart version is pinned via \$ARC_CHART_VERSION (default: $CHART_VERSION).
Version BUMPS are refused — follow ARC.md § "Chart upgrades" instead.
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

helm_diff() {
	local release="$1" chart="$2" namespace="$3" values="$4"
	echo -e "\nhelm diff upgrade $release ($chart @ $CHART_VERSION)..."
	# No `|| true`: a render failure here is a real preflight finding and must
	# surface (set -e aborts the script).
	helm diff upgrade "$release" "$chart" \
		--version "$CHART_VERSION" \
		--namespace "$namespace" \
		--allow-unreleased \
		-f "$values"
}

helm_apply() {
	local release="$1" chart="$2" namespace="$3" values="$4"
	echo -e "\nDeploying $release ($chart @ $CHART_VERSION)..."
	helm upgrade --install "$release" "$chart" \
		--version "$CHART_VERSION" \
		--namespace "$namespace" \
		--wait \
		--timeout "$TIMEOUT" \
		-f "$values"
}

# --- Read-only diff mode: no kubectl applies, no secret gate. -----------------
if [ "$HELM_DIFF" -eq 1 ]; then
	if ! helm plugin list 2>/dev/null | grep -q '^diff\s'; then
		echo "ERROR: --diff requested but the helm-diff plugin is not installed." >&2
		echo "  Install with: helm plugin install https://github.com/databus23/helm-diff" >&2
		exit 1
	fi
	helm_diff arc "$CONTROLLER_CHART" arc-systems "$SCRIPT_DIR/controller-values.yaml"
	helm_diff compendium "$SCALE_SET_CHART" arc-runners "$SCRIPT_DIR/runner-scale-set-compendium-values.yaml"
	exit 0
fi

# --- Version-skew guard: refuse in-place chart version bumps. -----------------
# Helm does not upgrade ARC's CRDs, so `helm upgrade` across chart versions
# leaves the controller and CRDs skewed. Upgrades = uninstall scale sets, then
# the controller, then reinstall at the new version (ARC.md § Chart upgrades).
INSTALLED_VERSION="$(helm get metadata arc -n arc-systems 2>/dev/null | awk '/^VERSION:/ {print $2}' || true)"
if [ -n "$INSTALLED_VERSION" ] && [ "$INSTALLED_VERSION" != "$CHART_VERSION" ]; then
	cat >&2 <<EOF
ERROR: controller release 'arc' is installed at chart $INSTALLED_VERSION but
this run targets $CHART_VERSION. ARC cannot be upgraded in place (Helm does
not upgrade CRDs). Follow ARC.md § "Chart upgrades":

  1. helm uninstall <every scale set> -n arc-runners
  2. helm uninstall arc -n arc-systems
  3. re-run this script at the new version

(To re-apply values at the INSTALLED version instead, set
ARC_CHART_VERSION=$INSTALLED_VERSION.)
EOF
	exit 2
fi

kubectl apply -f "$SCRIPT_DIR/namespaces.yaml"
kubectl apply -f "$SCRIPT_DIR/network-policies.yaml"

# --- Credential preflight: shape, not just existence. -------------------------
# The scale set cannot register without a usable GitHub credential; fail fast
# with instructions rather than letting the listener crashloop on an empty or
# placeholder token. This script's secret format supports PATs only (a GitHub
# App credential uses different keys — see ARC.md).
GITHUB_TOKEN_B64="$(kubectl get secret github-config-secret -n arc-runners -o jsonpath='{.data.github_token}' 2>/dev/null || true)"
GITHUB_TOKEN="$(printf '%s' "$GITHUB_TOKEN_B64" | base64 -d 2>/dev/null || true)"
if [ -z "$GITHUB_TOKEN" ]; then
	cat >&2 <<EOF
ERROR: secret arc-runners/github-config-secret is missing or has an empty
github_token key.

Copy arc/github-config-secret.yaml.example to arc/github-config-secret.yaml,
fill in a fine-grained PAT (Administration: Read and write on the target
private repos), then:

  kubectl apply -f arc/github-config-secret.yaml

The filled copy is gitignored (**/*secret*.yaml) — do not commit it.
EOF
	exit 2
fi
case "$GITHUB_TOKEN" in
"<FINE-GRAINED-PAT>")
	echo "ERROR: github_token still holds the .example placeholder — fill in a real PAT." >&2
	exit 2
	;;
github_pat_* | ghp_*) ;;
*)
	echo "ERROR: github_token doesn't look like a GitHub PAT (expected github_pat_* or ghp_*)." >&2
	echo "For a GitHub App credential, use the app secret keys instead — see ARC.md." >&2
	exit 2
	;;
esac
unset GITHUB_TOKEN GITHUB_TOKEN_B64

helm_apply arc "$CONTROLLER_CHART" arc-systems "$SCRIPT_DIR/controller-values.yaml"
helm_apply compendium "$SCALE_SET_CHART" arc-runners "$SCRIPT_DIR/runner-scale-set-compendium-values.yaml"

echo -e "\nDone. Check status:"
echo "  kubectl get pods -n arc-systems        # controller + per-scale-set listener pods"
echo "  kubectl get pods -n arc-runners        # empty until a job runs; ephemeral runner pods appear per job"
echo "  gh api repos/nathanwhyte/compendium/actions/runners --jq '.runners[].name'"
echo -e "\nWorkflows target the runners with: runs-on: homelab-arc-compendium"
