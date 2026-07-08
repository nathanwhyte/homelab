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

case "${1:-}" in
-h | --help)
	# Print the header docstring (usage + examples) without dispatching a Job.
	sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
	exit 0
	;;
esac

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

# Refuse to overlap: the compendium OV namespace is single-writer (BUG-1034/BUG-1035).
# A Job with status.active>=1 (running, or pod Pending on volume attach) blocks dispatch.
running=$(kubectl get jobs -n compendium -l app=compendium-sync \
	-o jsonpath='{range .items[?(@.status.active)]}{.metadata.name} ({.status.active} active){"\n"}{end}' 2>/dev/null)
if [ -n "$running" ]; then
	echo "a compendium-sync job is already running — refusing to start a second writer:" >&2
	printf '%s\n' "$running" | sed 's/^/  /' >&2
	echo "re-run once it completes: kubectl -n compendium get jobs -l app=compendium-sync" >&2
	exit 1
fi

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
	# kubectl wait errors out if the pod doesn't exist yet — poll for creation first
	for _ in $(seq 1 60); do
		kubectl get pod -l job-name="${JOB_NAME}" -n compendium --no-headers 2>/dev/null | grep -q . && break
		sleep 2
	done
	kubectl wait --for=condition=ready pod -l job-name="${JOB_NAME}" -n compendium --timeout=300s
	kubectl logs -f "job/${JOB_NAME}" -n compendium
	kubectl wait --for=condition=complete "job/${JOB_NAME}" -n compendium --timeout=60s >/dev/null 2>&1 ||
		{
			echo "job did not complete cleanly — inspect: kubectl -n compendium describe job ${JOB_NAME}"
			exit 1
		}
fi
