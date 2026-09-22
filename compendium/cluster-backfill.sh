#!/usr/bin/env bash
# Dispatch an explicit-path compendium→OV backfill Job in the cluster.
#
#   compendium/cluster-backfill.sh <name> <paths-file> [-- <compendium-sync.py sync args>]
#
# <name>        Short slug; the Job is ov-backfill-<name> and its path list the
#               ConfigMap ov-backfill-<name>-paths.
# <paths-file>  Vault-relative paths, one per line (blank lines and # comments
#               are dropped). The runner clones pushed origin/main, so every
#               path must exist there.
#
# Default sync args (the BUG-1173 run's):
#   --include-active --order small-first --batch-size 50 --wait-drain --yes --max-errors 10 --deadline-seconds 32000
# Custom args replace them whole; keep --deadline-seconds under the Job's
# activeDeadlineSeconds (32400).
#
# The Job is created first and the ConfigMap second, carrying an ownerReference
# to the Job — so the Job's ttlSecondsAfterFinished removes both. (The pod waits
# in ContainerCreating for the few seconds until the ConfigMap exists.)
#
# Dispatch-and-return. Follow with:
#   kubectl -n compendium logs -f job/ov-backfill-<name>
#
# Needs the namespace, state claim, RBAC and secrets that cluster-sync.sh
# bootstraps; run that once first on a fresh cluster. Refuses to start while any
# app=compendium-sync Job is active (single writer, BUG-1034/BUG-1035).
set -euo pipefail

usage() { sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'; }
case "${1:-}" in
-h | --help)
	usage
	exit 0
	;;
esac
if [ "$#" -lt 2 ]; then
	usage >&2
	exit 2
fi

NAME="$1"
PATHS_FILE="$2"
shift 2
if [ "${1:-}" = "--" ]; then
	shift
fi

if ! [[ "$NAME" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]]; then
	echo "error: <name> must be a lowercase DNS label (a-z, 0-9, -): $NAME" >&2
	exit 2
fi
if [ ! -f "$PATHS_FILE" ]; then
	echo "error: paths file not found: $PATHS_FILE" >&2
	exit 2
fi

cd "$(dirname "$0")"

JOB_NAME="ov-backfill-${NAME}"
PATHS_CONFIGMAP="${JOB_NAME}-paths"
export JOB_NAME PATHS_CONFIGMAP

if [ "$#" -gt 0 ]; then
	SYNC_ARGS="$*"
else
	SYNC_ARGS="--include-active --order small-first --batch-size 50 --wait-drain --yes --max-errors 10 --deadline-seconds 32000"
fi
export SYNC_ARGS

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
grep -v -e '^[[:space:]]*$' -e '^[[:space:]]*#' "$PATHS_FILE" >"$tmp/paths.txt" || true
count=$(wc -l <"$tmp/paths.txt" | tr -d ' ')
if [ "$count" -eq 0 ]; then
	echo "error: no paths in $PATHS_FILE" >&2
	exit 2
fi
# ConfigMaps cap at 1 MiB; leave headroom for metadata.
if [ "$(wc -c <"$tmp/paths.txt")" -gt 900000 ]; then
	echo "error: path list exceeds 900 KB — split it into several backfills" >&2
	exit 2
fi

for secret in openviking-api-key compendium-git-token; do
	if ! kubectl get secret "$secret" -n compendium >/dev/null 2>&1; then
		echo "error: secret compendium/$secret is missing — run compendium/cluster-sync.sh once to bootstrap" >&2
		exit 1
	fi
done
if ! kubectl get pvc compendium-sync-state-v2 -n compendium >/dev/null 2>&1; then
	echo "error: PVC compendium/compendium-sync-state-v2 is missing — see compendium/state-migration.md" >&2
	exit 1
fi
if kubectl get job "$JOB_NAME" -n compendium >/dev/null 2>&1; then
	echo "error: job compendium/$JOB_NAME already exists — pick another name or delete it first" >&2
	exit 1
fi

running=$(kubectl get jobs -n compendium -l app=compendium-sync \
	-o jsonpath='{range .items[?(@.status.active)]}{.metadata.name} ({.status.active} active){"\n"}{end}' 2>/dev/null)
if [ -n "$running" ]; then
	echo "a compendium-sync job is already running — refusing to start a second writer:" >&2
	printf '%s\n' "$running" | sed 's/^/  /' >&2
	exit 1
fi

# shellcheck disable=SC2016  # envsubst needs the literal variable names
envsubst '${JOB_NAME} ${PATHS_CONFIGMAP} ${SYNC_ARGS}' <compendium-backfill-job.template.yaml |
	kubectl apply -f -
job_uid=$(kubectl get job "$JOB_NAME" -n compendium -o jsonpath='{.metadata.uid}')

# Owned by the Job: TTL/garbage collection removes it with the Job.
if ! kubectl create configmap "$PATHS_CONFIGMAP" -n compendium \
	--from-file=paths.txt="$tmp/paths.txt" --dry-run=client -o json |
	python3 -c "
import json, sys
cm = json.load(sys.stdin)
cm['metadata']['labels'] = {'app': 'compendium-sync', 'purpose': 'backfill'}
cm['metadata']['ownerReferences'] = [{
    'apiVersion': 'batch/v1', 'kind': 'Job', 'name': sys.argv[1], 'uid': sys.argv[2],
}]
print(json.dumps(cm))
" "$JOB_NAME" "$job_uid" | kubectl apply -f -; then
	echo "error: ConfigMap creation failed — deleting job $JOB_NAME so it does not hang in ContainerCreating" >&2
	kubectl delete job "$JOB_NAME" -n compendium --ignore-not-found
	exit 1
fi

echo "job: $JOB_NAME ($count paths; args: $SYNC_ARGS)"
echo "follow: kubectl -n compendium logs -f job/$JOB_NAME"
