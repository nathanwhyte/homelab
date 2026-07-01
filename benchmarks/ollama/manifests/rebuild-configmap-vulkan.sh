#!/usr/bin/env bash
# Rebuild the llama namespace ConfigMap for Vulkan backend benchmarks.
set -euo pipefail

NS="${1:-llama}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
OLLAMA_DIR="$ROOT/benchmarks/ollama"

cd "$OLLAMA_DIR"

kubectl create configmap bench-scripts-vulkan \
	--namespace="$NS" \
	--from-file=concurrency-bench.py=tools/concurrency-bench.py \
	--from-file=metrics.py=../lib/metrics.py \
	--from-file=output.py=../lib/output.py \
	--from-file=prompts.py=../lib/prompts.py \
	--from-file=tool_calls.py=../lib/tool_calls.py \
	--from-file=cluster-vulkan-default.toml=configs/cluster-vulkan-default.toml \
	--from-file=cluster-vulkan-agentic.toml=configs/cluster-vulkan-agentic.toml \
	--from-file=cluster-vulkan-q5km-default.toml=configs/cluster-vulkan-q5km-default.toml \
	--from-file=cluster-vulkan-q6k-default.toml=configs/cluster-vulkan-q6k-default.toml \
	--from-file=cluster-vulkan-q5km-agentic.toml=configs/cluster-vulkan-q5km-agentic.toml \
	--from-file=cluster-vulkan-q6k-agentic.toml=configs/cluster-vulkan-q6k-agentic.toml \
	--dry-run=client -o yaml \
	>manifests/benchmark-configmap-vulkan.yaml

kubectl apply -f manifests/benchmark-configmap-vulkan.yaml
