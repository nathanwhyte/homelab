#!/usr/bin/env bash
# Stand up the cluster-native overnight benchmark (TASK-1122).
#   ./cluster/apply.sh <github-token>
# Creates: secret bench-git, configmap bench-scripts (scripts + ground truth),
# then applies cluster/overnight.yaml (pgvector + Qwen embedders + runner Job).
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
tok="${1:?usage: apply.sh <github-token>}"

kubectl create namespace bench --dry-run=client -o yaml | kubectl apply -f -
kubectl -n bench create secret generic bench-git \
	--from-literal=token="$tok" --dry-run=client -o yaml | kubectl apply -f -

kubectl -n bench create configmap bench-scripts \
	--from-file="$here/benchmark_embedders.py" \
	--from-file="$here/benchmark_bge_m3.py" \
	--from-file="$here/nomic_st.py" \
	--from-file="$here/export_corpus.py" \
	--from-file="$here/validate_groundtruth.py" \
	--from-file="$here/run-all.sh" \
	--from-file="$here/schema.sql" \
	--from-file="$here/eval_groundtruth_2026-07-04.json" \
	--dry-run=client -o yaml | kubectl apply -f -

kubectl apply -f "$here/cluster/overnight.yaml"
echo "applied. watch: kubectl -n bench get pods -w"
echo "logs:  kubectl -n bench logs job/bench-runner -f"
