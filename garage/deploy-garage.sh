#!/usr/bin/env bash
set -euo pipefail

# Derive from the script's own location, matching deploy-grafana.sh. The previous
# hardcoded "$HOME/code/homelab/garage" broke when the repo became bare with
# sibling worktrees — that path no longer exists, so the script exited before
# reaching any of its work.
GARAGE_DIR="${GARAGE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
NAMESPACE="garage"
# The chart is a gitignored clone of the Garage repo, pinned to the tag that
# matches the deployed chart (0.10.4). Change the ref only in a chart upgrade.
GARAGE_CHART_REF="v2.5.0"
GARAGE_CHART_REPO="${GARAGE_CHART_REPO:-https://git.deuxfleurs.fr/Deuxfleurs/garage}"
CLONE_DIR="$GARAGE_DIR/garage"
CHART_DIR="${GARAGE_CHART_DIR:-$CLONE_DIR/script/helm/garage}"
SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/scripts"
HELM_DEPLOY="$SCRIPTS_DIR/helm-deploy.py"
ROLL="$SCRIPTS_DIR/sts-partition-roll.py"

# Clone at the pinned ref when missing; refuse when the clone sits on any other
# tag. GARAGE_CHART_DIR points at a chart of the caller's own and skips the pin.
ensure_chart_ref() {
	[[ -z "${GARAGE_CHART_DIR:-}" ]] || return 0
	if [[ ! -d "$CLONE_DIR" ]]; then
		echo "Cloning Garage $GARAGE_CHART_REF into $CLONE_DIR..."
		git clone --branch "$GARAGE_CHART_REF" "$GARAGE_CHART_REPO" "$CLONE_DIR" || exit 1
	fi
	local actual
	actual="$(git -C "$CLONE_DIR" describe --tags --exact-match 2>/dev/null || echo "no tag")"
	if [[ "$actual" != "$GARAGE_CHART_REF" ]]; then
		echo "Garage chart clone is at $actual but this script pins $GARAGE_CHART_REF." >&2
		echo "Fix: git -C $CLONE_DIR fetch origin tag $GARAGE_CHART_REF && git -C $CLONE_DIR checkout $GARAGE_CHART_REF" >&2
		exit 1
	fi
}

# A StatefulSet's volumeClaimTemplates are immutable, so a values change to a
# persistence size or class makes the Helm upgrade fail partway through. Compare
# the rendered templates with the live StatefulSet first and stop with the fix.
# The fix deletes only the StatefulSet object: its pods and PVCs keep running,
# and the next Helm run recreates it and adopts them. This script never runs it.
check_claim_templates() {
	local live rendered tool
	for tool in jq yq; do
		command -v "$tool" >/dev/null || {
			echo "$tool not installed (needed to compare volumeClaimTemplates)." >&2
			exit 1
		}
	done
	live="$(kubectl -n "$NAMESPACE" get statefulset garage -o json 2>/dev/null |
		jq -c '[.spec.volumeClaimTemplates[] | {name: .metadata.name, class: .spec.storageClassName, size: .spec.resources.requests.storage}]')" || live=""
	# No live StatefulSet: a first install, nothing to compare.
	[[ -n "$live" ]] || return 0
	rendered="$(helm template garage "$CHART_DIR" -n "$NAMESPACE" -f "$GARAGE_DIR/garage-values.yaml" \
		--show-only templates/workload.yaml | yq -o=json '.' |
		jq -c 'select(.kind == "StatefulSet") | [.spec.volumeClaimTemplates[] | {name: .metadata.name, class: .spec.storageClassName, size: .spec.resources.requests.storage}]')"
	if [[ "$rendered" != "$live" ]]; then
		echo "garage-values.yaml changes the StatefulSet's volumeClaimTemplates, which Kubernetes refuses to update." >&2
		echo "  live:     $live" >&2
		echo "  rendered: $rendered" >&2
		# Only the plain path recreates the StatefulSet: --upgrade-chart's hold step
		# reads the live StatefulSet first and fails when it is gone, and the plain
		# path refuses a chart-version change (helm-deploy.py). So a chart upgrade
		# and a template change go in two runs, the upgrade first.
		echo "Fix: kubectl -n $NAMESPACE delete statefulset garage --cascade=orphan, then rerun this script without --upgrade-chart." >&2
		echo "If this run is also a chart-version upgrade, do not delete yet: first run --upgrade-chart with" >&2
		echo "the claim templates set back to the live values above, then change them and use the fix above." >&2
		echo "Pods and PVCs keep running; existing PVCs keep their size (expand them live)." >&2
		exit 1
	fi
}

DRY_RUN=0
UPGRADE=0
for arg in "$@"; do
	case "$arg" in
	--dry-run) DRY_RUN=1 ;;
	--upgrade-chart) UPGRADE=1 ;;
	*)
		echo "Usage: $0 [--upgrade-chart] [--dry-run]" >&2
		exit 2
		;;
	esac
done

# --upgrade-chart: a deliberate chart version change. helm-deploy.py runs the
# server dry run and diff, holds the StatefulSet at partition 3, and upgrades;
# the roll step then releases one pod at a time. With --dry-run nothing changes.
HELM_FLAGS=()
if [[ $UPGRADE -eq 1 ]]; then
	HELM_FLAGS=(--upgrade-chart --hold-statefulset garage)
fi

if [[ $DRY_RUN -eq 1 ]]; then
	ensure_chart_ref
	[[ -d "$CHART_DIR" ]] || {
		echo "Helm chart not found at $CHART_DIR" >&2
		exit 1
	}
	check_claim_templates
	python3 "$HELM_DEPLOY" garage "$NAMESPACE" "$CHART_DIR" --dry-run ${HELM_FLAGS[@]+"${HELM_FLAGS[@]}"} -f "$GARAGE_DIR/garage-values.yaml"
	exit 0
fi

ensure_chart_ref

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

echo "Deploying Garage S3-compatible storage..."

if [ ! -d "$CHART_DIR" ]; then
	echo "Helm chart not found at $CHART_DIR"
	exit 1
fi

if [ ! -f "$GARAGE_DIR/garage-values.yaml" ]; then
	echo "garage-values.yaml file not found!"
	exit 1
fi

check_claim_templates

# A chart upgrade changes only the Helm release and the StatefulSet partition:
# it runs before the StorageClass apply below, which a release that already
# exists does not need.
if [[ $UPGRADE -eq 1 ]]; then
	python3 "$HELM_DEPLOY" garage "$NAMESPACE" "$CHART_DIR" ${HELM_FLAGS[@]+"${HELM_FLAGS[@]}"} \
		-f "$GARAGE_DIR/garage-values.yaml"
	# The health gate needs all 3 nodes HEALTHY on every pod before the next release.
	python3 "$ROLL" "$NAMESPACE" garage \
		--gate-container garage --gate-command "/garage status" --expect-nodes 3
	echo -e "\nChart upgrade complete. Manager manifests were not re-applied."
	exit 0
fi

# The StorageClass must exist BEFORE the Helm release: the data
# volumeClaimTemplate requests `longhorn-garage-data`, and a PVC naming a class
# that does not exist stays Pending indefinitely rather than failing loudly.
if [ ! -f "$GARAGE_DIR/storageclass-longhorn-garage-data.yaml" ]; then
	echo "storageclass-longhorn-garage-data.yaml file not found!"
	exit 1
fi

echo -e "\nApplying longhorn-garage-data StorageClass..."
kubectl apply -f "$GARAGE_DIR/storageclass-longhorn-garage-data.yaml"

python3 "$HELM_DEPLOY" garage "$NAMESPACE" \
	"$CHART_DIR" \
	--create-namespace \
	-f "$GARAGE_DIR/garage-values.yaml"

echo -e "\nDeploying Garage Manager..."

if [ ! -f "$GARAGE_DIR/manager/garage-manager-config.yaml" ]; then
	echo "garage-manager-config.yaml file not found!"
	exit 1
fi

if [ ! -f "$GARAGE_DIR/manager/garage-manager.yaml" ]; then
	echo "garage-manager.yaml file not found!"
	exit 1
fi

if [ ! -f "$GARAGE_DIR/manager/garage-manager.secret.yaml" ]; then
	echo "garage-manager.secret.yaml file not found!"
	echo "Create this file and apply it, or manually create the garage manager secret."
else
	kubectl apply -f "$GARAGE_DIR/manager/garage-manager.secret.yaml"
fi

kubectl apply -f "$GARAGE_DIR/manager/garage-manager-config.yaml"
kubectl apply -f "$GARAGE_DIR/manager/garage-manager.yaml"

echo -e "\nDone! Visit:"
echo "  S3 API (cluster-only): http://garage.garage.svc.cluster.local:3900"

echo -e "\nCheck status:"
echo "  kubectl get pods -n $NAMESPACE"
echo "  kubectl get statefulset -n $NAMESPACE"
echo "  kubectl get pvc -n $NAMESPACE"
echo "  kubectl get ingress -n $NAMESPACE"
