#!/usr/bin/env bash
set -euo pipefail

# Resolve relative to this script, not to $HOME. The homelab repo is bare with
# linked worktrees, so the checkout lives at ~/code/homelab/<branch>/ — one level
# deeper than the old hardcoded "$HOME/code/homelab/gpu/nvidia", which has been
# failing with "values.yaml not found!" since that conversion. Self-relative also
# means the script works from any worktree and from any cwd.
NVIDIA_GPU_DIR="${NVIDIA_GPU_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
NAMESPACE="gpu-operator"
HELM_DEPLOY="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/scripts/helm-deploy.py"

# Release name is historical: the original March 2026 "gpu-operator" release got
# stuck uninstalling and was replaced by this generated name; the stuck release
# secret was purged 2026-07-06. Live resources are annotated to this name, so a
# plain "gpu-operator" release here would collide with ownership metadata.
# helm-deploy.py matches on chart identity (gpu-operator-<version>) rather than
# release name, so the mismatch between the two is fine.
RELEASE="gpu-operator-1774050554"

# IMPR-1148: no --version here on purpose. This script was the last one deciding
# a chart version from a file. The pin matched live (v26.3.3) when it was checked,
# but nothing kept it that way — deploy-longhorn.sh also matched until it didn't,
# and would have downgraded 32 attached volumes. helm-deploy.py reads the deployed
# chart version and passes it back explicitly, making this an "apply my values"
# tool. Upgrading the chart is a separate, deliberate Helm operation — and on this
# release an especially deliberate one, because values.yaml sets
# driver.enabled: false (BUG-1102) and an unreviewed chart move could reintroduce
# operator-managed drivers.
case "${1:-}" in
--dry-run)
	[[ $# -eq 1 ]] || exit 2
	# Repositories must already be configured — see scripts/helm-deploy.md.
	# Adding them here would make the simulation mutate local Helm state.
	python3 "$HELM_DEPLOY" "$RELEASE" "$NAMESPACE" nvidia/gpu-operator \
		--dry-run -f "$NVIDIA_GPU_DIR/values.yaml"
	exit 0
	;;
"") [[ $# -eq 0 ]] || exit 2 ;;
*)
	echo "Usage: $0 [--dry-run]" >&2
	exit 2
	;;
esac

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

if [ ! -f "$NVIDIA_GPU_DIR/values.yaml" ]; then
	echo "values.yaml not found!"
	exit 1
fi

# Check that GPU nodes have expected labels
for node in manu wemby; do
	if ! kubectl get node "$node" --show-labels 2>/dev/null | grep -q "gpu.vendor=nvidia"; then
		echo "WARNING: Node '$node' does not have label gpu.vendor=nvidia"
		echo "  kubectl label node $node gpu=true gpu.vendor=nvidia"
	fi
done

echo "Updating helm repositories..."
helm repo add nvidia https://helm.ngc.nvidia.com/nvidia
helm repo update nvidia

echo -e "\nDeploying NVIDIA GPU Operator (reusing the installed chart version)..."
python3 "$HELM_DEPLOY" "$RELEASE" "$NAMESPACE" nvidia/gpu-operator \
	--create-namespace \
	-f "$NVIDIA_GPU_DIR/values.yaml"

echo -e "\nWaiting for GPU operator rollout..."
kubectl rollout status deployment/gpu-operator -n "$NAMESPACE" --timeout=180s || true

echo -e "\nDone!"
echo "Verify GPU resources are registered:"
echo "  kubectl describe node manu | grep nvidia.com/gpu"
echo "  kubectl describe node wemby | grep nvidia.com/gpu"
echo ""
echo "Run smoke test:"
# cuda-vectoradd.yaml was referenced here but has never existed in this directory.
# Verify against a real GPU workload instead — it proves the same thing (CUDA
# reaches the driver from inside a container) without a throwaway manifest.
echo "  kubectl -n viking exec deploy/embedder-qwen-cuda -- \\"
echo "    nvidia-smi --query-gpu=name,driver_version,memory.used --format=csv,noheader"
echo ""
echo "Driver is host-installed via dkms (BUG-1102) — the operator no longer manages"
echo "it. If a node reports nvidia.com/gpu: 0, check the HOST first:"
echo "  ssh <node> 'dkms status; nvidia-smi; modinfo nvidia | head -2'"
echo "See gpu/nvidia/host-driver-migration.md."
