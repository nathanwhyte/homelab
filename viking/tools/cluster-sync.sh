#!/usr/bin/env bash
# Run a compendium→OV sync from inside the cluster (not bound to a workstation).
#
#   viking/tools/cluster-sync.sh [--follow] -- <compendium-sync.py sync args>
#
# Examples:
#   viking/tools/cluster-sync.sh -- --limit 0 --include-active --no-wait --order interleave
#   viking/tools/cluster-sync.sh --follow -- --include-active --no-wait "bugs/dipdash/BUG-004-*.md"
#
# Requires: secret compendium-git-token (GITHUB_TOKEN) in viking ns — created
# once by copying hermes/github-access-token (see below); openviking-api-key
# must exist (part of the canonical stack).
set -euo pipefail

cd "$(dirname "$0")/.."

FOLLOW=0
if [ "${1:-}" = "--follow" ]; then
	FOLLOW=1
	shift
fi
[ "${1:-}" = "--" ] && shift

if ! kubectl get secret compendium-git-token -n viking >/dev/null 2>&1; then
	echo "creating viking/compendium-git-token from hermes/github-access-token"
	kubectl get secret github-access-token -n hermes -o json |
		python3 -c "import json,sys; s=json.load(sys.stdin); s['metadata']={'name':'compendium-git-token','namespace':'viking'}; print(json.dumps(s))" |
		kubectl apply -f -
fi

export JOB_NAME="compendium-sync-$(date +%s)"
export SYNC_ARGS="$*"
envsubst '${JOB_NAME} ${SYNC_ARGS}' <manifests/compendium-sync-job.template.yaml | kubectl apply -f -
echo "job: ${JOB_NAME} (args: ${SYNC_ARGS})"

if [ "$FOLLOW" = "1" ]; then
	kubectl wait --for=condition=ready pod -l job-name="${JOB_NAME}" -n viking --timeout=300s
	kubectl logs -f "job/${JOB_NAME}" -n viking
fi
