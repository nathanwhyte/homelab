#!/usr/bin/env bash
# Rebuild the llama namespace ConfigMap for Vulkan backend benchmarks.
#
# The ConfigMap embeds a *copy* of the harness sources, so the tracked YAML
# goes stale whenever concurrency-bench.py or benchmarks/lib/* changes, and a
# cluster Vulkan run then silently executes the old harness. Regeneration is
# local (kubectl --dry-run=client needs no cluster), so it is separated from
# the apply:
#
#   ./rebuild-configmap-vulkan.sh              regenerate + apply (needs cluster)
#   ./rebuild-configmap-vulkan.sh --no-apply   regenerate the tracked YAML only
#   ./rebuild-configmap-vulkan.sh --check      fail if the tracked YAML is stale
#
# --check is the preflight: run it in CI or before a cluster benchmark to prove
# the committed artifact matches the current sources.
set -euo pipefail

APPLY=1
CHECK=0
args=()
for a in "$@"; do
	case "$a" in
	--no-apply) APPLY=0 ;;
	--check)
		CHECK=1
		APPLY=0
		;;
	*) args+=("$a") ;;
	esac
done
set -- "${args[@]+"${args[@]}"}"

NS="${1:-llama}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
OLLAMA_DIR="$ROOT/benchmarks/ollama"

cd "$OLLAMA_DIR"

OUT="$(mktemp -t benchmark-configmap-vulkan)"
trap 'rm -f "$OUT"' EXIT

kubectl create configmap bench-scripts-vulkan \
	--namespace="$NS" \
	--from-file=concurrency-bench.py=tools/concurrency-bench.py \
	--from-file=coherence-smoke.py=tools/coherence-smoke.py \
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
	--from-file=cluster-vulkan-nemotron-nano-bf16-default.toml=configs/cluster-vulkan-nemotron-nano-bf16-default.toml \
	--from-file=cluster-vulkan-qwen35-9b-default.toml=configs/cluster-vulkan-qwen35-9b-default.toml \
	--from-file=cluster-vulkan-qwen35-9b-agentic.toml=configs/cluster-vulkan-qwen35-9b-agentic.toml \
	--from-file=cluster-vulkan-nemotron-nano-bf16-agentic.toml=configs/cluster-vulkan-nemotron-nano-bf16-agentic.toml \
	--from-file=cluster-vulkan-nemotron-nano-bf16-agentic-nothink.toml=configs/cluster-vulkan-nemotron-nano-bf16-agentic-nothink.toml \
	--from-file=cluster-vulkan-nemotron-nano-q8-default.toml=configs/cluster-vulkan-nemotron-nano-q8-default.toml \
	--from-file=cluster-vulkan-nemotron-nano-q8-agentic-nothink.toml=configs/cluster-vulkan-nemotron-nano-q8-agentic-nothink.toml \
	--dry-run=client -o yaml \
	>"$OUT"

if [[ $CHECK -eq 1 ]]; then
	if diff -q "$OUT" manifests/benchmark-configmap-vulkan.yaml >/dev/null 2>&1; then
		echo "ConfigMap is current."
		rm -f "$OUT"
		exit 0
	fi
	echo "ERROR: manifests/benchmark-configmap-vulkan.yaml is STALE." >&2
	echo "The embedded harness differs from the current sources; a cluster" >&2
	echo "Vulkan run would execute the old code. Regenerate with:" >&2
	echo "  ./benchmarks/ollama/manifests/rebuild-configmap-vulkan.sh --no-apply" >&2
	diff "$OUT" manifests/benchmark-configmap-vulkan.yaml | head -20 >&2 || true
	rm -f "$OUT"
	exit 1
fi

mv "$OUT" manifests/benchmark-configmap-vulkan.yaml

if [[ $APPLY -eq 1 ]]; then
	kubectl apply -f manifests/benchmark-configmap-vulkan.yaml
else
	echo "Regenerated manifests/benchmark-configmap-vulkan.yaml (not applied)."
fi
