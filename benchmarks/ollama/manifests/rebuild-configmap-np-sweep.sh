#!/usr/bin/env bash
# Rebuild the llama namespace ConfigMap for the NUM_PARALLEL sweep.
# Includes the new np{1,3,6,8} default + agentic configs.
set -euo pipefail

NS="${1:-llama}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
OLLAMA_DIR="$ROOT/benchmarks/ollama"

cd "$OLLAMA_DIR"

kubectl create configmap bench-scripts \
	--namespace="$NS" \
	--from-file=concurrency-bench.py=tools/concurrency-bench.py \
	--from-file=metrics.py=../lib/metrics.py \
	--from-file=output.py=../lib/output.py \
	--from-file=prompts.py=../lib/prompts.py \
	--from-file=tool_calls.py=../lib/tool_calls.py \
	--from-file=cluster-agentic.toml=configs/cluster-agentic.toml \
	--from-file=cluster-default.toml=configs/cluster-default.toml \
	--from-file=cluster-np1-agentic.toml=configs/cluster-np1-agentic.toml \
	--from-file=cluster-np1-default.toml=configs/cluster-np1-default.toml \
	--from-file=cluster-np3-agentic.toml=configs/cluster-np3-agentic.toml \
	--from-file=cluster-np3-default.toml=configs/cluster-np3-default.toml \
	--from-file=cluster-np6-agentic.toml=configs/cluster-np6-agentic.toml \
	--from-file=cluster-np6-default.toml=configs/cluster-np6-default.toml \
	--from-file=cluster-np8-agentic.toml=configs/cluster-np8-agentic.toml \
	--from-file=cluster-np8-default.toml=configs/cluster-np8-default.toml \
	--from-file=cluster-qwen35-qat.toml=configs/cluster-qwen35-qat.toml \
	--from-file=cluster-test.toml=configs/cluster-test.toml \
	--dry-run=client -o yaml \
	>manifests/benchmark-configmap.yaml

kubectl apply -f manifests/benchmark-configmap.yaml
