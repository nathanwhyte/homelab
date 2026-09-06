#!/usr/bin/env bash
set -euo pipefail

AMD_GPU_DIR="${AMD_GPU_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"

if [ ! -x "$(command -v "kubectl")" ]; then
    echo "kubectl not installed."
    exit 1
fi

if ! kubectl cluster-info > /dev/null 2>&1; then
    echo "kubectl not connected to a cluster."
    exit 1
fi

echo -e "\nDeploying AMD GPU device plugin..."

# Check that timmy has the gpu.vendor=amd label
if ! kubectl get node timmy --show-labels 2>/dev/null | grep -q "gpu.vendor=amd"; then
    echo "WARNING: Node 'timmy' does not have label gpu.vendor=amd"
    echo "Run the following to label nodes:"
    echo "  kubectl label node timmy gpu=true gpu.vendor=amd gpu.model=rx9070xt"
    echo "  kubectl label node manu gpu.vendor=nvidia"
    echo "  kubectl label node wemby gpu.vendor=nvidia"
    echo ""
    echo "Continuing anyway (DaemonSet will wait for matching nodes)..."
fi

kubectl apply -f "$AMD_GPU_DIR/amd-device-plugin.yaml"

echo -e "\nWaiting for AMD GPU device plugin rollout..."
kubectl rollout status daemonset/amd-gpu-device-plugin -n kube-system --timeout=120s || true

echo -e "\nDeploying AMD GPU metrics exporter..."
kubectl apply -f "$AMD_GPU_DIR/amdgpu-exporter.yaml"
kubectl rollout status daemonset/amdgpu-exporter -n kube-system --timeout=120s || true

echo -e "\nDeploying AMD GPU Grafana dashboard..."
kubectl apply -f "$AMD_GPU_DIR/amdgpu-dashboard.yaml"

echo -e "\nDone!"
echo "Verify GPU resource is registered:"
echo "  kubectl describe node timmy | grep amd.com/gpu"
echo ""
echo "Verify metrics exporter:"
echo "  kubectl exec -n kube-system ds/amdgpu-exporter -- wget -qO- http://localhost:9101/metrics"
echo ""
echo "Run smoke test:"
echo "  kubectl apply -f $AMD_GPU_DIR/rocm-test-pod.yaml"
echo "  kubectl wait --for=condition=Ready pod/rocm-smi-test --timeout=120s"
echo "  kubectl logs rocm-smi-test"
echo "  kubectl delete pod rocm-smi-test"
