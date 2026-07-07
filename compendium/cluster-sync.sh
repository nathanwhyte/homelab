#!/usr/bin/env bash
# Run a compendium→OV sync from inside the cluster — the SINGLE sync writer
# for the compendium OV namespace (BUG-1034 phase 2).
#
#   compendium/cluster-sync.sh [--follow] [-- <compendium-sync.py sync args>]
#
# With no args, runs the canonical manual sync:
#   sync --changed --yes --state-file /state/compendium-sync-state.json --no-wait
# deriving the work list from git history since the last-synced commit on the
# state PVC. The runner only sees pushed origin/main — push first.
#
# Examples:
#   compendium/cluster-sync.sh --follow
#   compendium/cluster-sync.sh --follow -- --changed --yes --state-file /state/compendium-sync-state.json --force-overlap
#   compendium/cluster-sync.sh -- --include-active --no-wait "bugs/dipdash/BUG-004-*.md"
#
# Bootstraps (idempotent): compendium namespace + state PVC, secret
# compendium-git-token (copied from hermes/github-access-token), secret
# openviking-api-key (copied from viking/openviking-api-key — Secrets don't
# cross namespaces).
set -euo pipefail

cd "$(dirname "$0")"

FOLLOW=0
if [ "${1:-}" = "--follow" ]; then
	FOLLOW=1
	shift
fi
[ "${1:-}" = "--" ] && shift

kubectl apply -f namespace.yaml -f state-pvc.yaml >/dev/null

copy_secret() {
	local src_ns="$1" src_name="$2" dst_name="$3"
	if ! kubectl get secret "$dst_name" -n compendium >/dev/null 2>&1; then
		echo "creating compendium/$dst_name from $src_ns/$src_name"
		kubectl get secret "$src_name" -n "$src_ns" -o json |
			python3 -c "import json,sys; s=json.load(sys.stdin); s['metadata']={'name':'$dst_name','namespace':'compendium'}; print(json.dumps(s))" |
			kubectl apply -f -
	fi
}
copy_secret hermes github-access-token compendium-git-token
copy_secret viking openviking-api-key openviking-api-key

JOB_NAME="compendium-sync-$(date +%s)"
export JOB_NAME
if [ "$#" -gt 0 ]; then
	export SYNC_ARGS="$*"
else
	export SYNC_ARGS="--changed --yes --state-file /state/compendium-sync-state.json --no-wait"
fi
# shellcheck disable=SC2016  # envsubst needs the literal variable names
envsubst '${JOB_NAME} ${SYNC_ARGS}' <compendium-sync-job.template.yaml | kubectl apply -f -
echo "job: ${JOB_NAME} (args: ${SYNC_ARGS})"

if [ "$FOLLOW" = "1" ]; then
	kubectl wait --for=condition=ready pod -l job-name="${JOB_NAME}" -n compendium --timeout=300s
	kubectl logs -f "job/${JOB_NAME}" -n compendium
	kubectl wait --for=condition=complete "job/${JOB_NAME}" -n compendium --timeout=60s >/dev/null 2>&1 ||
		{
			echo "job did not complete cleanly — inspect: kubectl -n compendium describe job ${JOB_NAME}"
			exit 1
		}
fi
