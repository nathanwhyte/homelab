#!/usr/bin/env bash
# Run a compendium→OV sync from inside the cluster — the SINGLE sync writer
# for the compendium OV namespace (BUG-1034 phase 2).
#
#   compendium/cluster-sync.sh [--follow] [--heal] [-- <compendium-sync.py sync args>]
#
# With no args, runs the canonical manual sync:
#   sync --changed --yes --state-file /state/compendium-sync-state.json --no-wait --deadline-seconds 2400
# deriving the work list from git history since the last-synced commit on the
# state PVC. The runner only sees pushed origin/main — push first.
#
# Flags:
#   --follow    Stream the Job's logs and wait for it to complete.
#   --heal      DEPRECATED and read-only (IMPR-1062 Phase 4): healing now lives
#               INSIDE the Job — correlated wedge detection plus a guarded,
#               cooldown-breakered restart of deploy/openviking via the
#               compendium-sync ServiceAccount (sync-rbac.yaml). This flag
#               never restarts anything; it waits for the Job and reports the
#               Job's own heal decision from its logs. A second restart
#               controller outside the Job's breaker is not acceptable
#               (IMPR-1062 plan review P1).
#
# Examples:
#   compendium/cluster-sync.sh                   # canonical: dispatch and move on
#   compendium/cluster-sync.sh --follow
#   compendium/cluster-sync.sh -- --include-active --no-wait "bugs/dipdash/BUG-004-*.md"
#
# Bootstraps (idempotent): compendium namespace + state PVC + sync RBAC (the
# in-Job heal's SA/Role/RoleBinding), secret compendium-git-token (copied from
# hermes/github-access-token), secret openviking-api-key (copied from
# viking/openviking-api-key — Secrets don't cross namespaces).
set -euo pipefail

OV_NAMESPACE=viking
OV_DEPLOY=openviking

case "${1:-}" in
-h | --help)
	# Print the header docstring (usage + flags + examples) without dispatching a Job.
	sed -n '2,31p' "$0" | sed 's/^# \{0,1\}//'
	exit 0
	;;
esac

cd "$(dirname "$0")"

FOLLOW=0
HEAL=0
while [ "$#" -gt 0 ]; do
	case "$1" in
	--follow)
		FOLLOW=1
		shift
		;;
	--heal | --heal=*)
		HEAL=1
		echo "note: --heal is deprecated and read-only — healing runs inside the Job (IMPR-1062 Phase 4); this waits and reports only" >&2
		shift
		;;
	--)
		shift
		break
		;;
	*) break ;;
	esac
done

kubectl apply -f namespace.yaml -f state-pvc.yaml -f sync-rbac.yaml >/dev/null

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

if [ "$#" -gt 0 ]; then
	export SYNC_ARGS="$*"
else
	# IMPR-1062 Phase 3: --deadline-seconds 2400 gives the journal a global
	# monotonic budget that sits UNDER the Job's activeDeadlineSeconds (3600),
	# leaving 1200s for scheduling and container preflight before the Python
	# deadline starts. Matches the script's own default; passed explicitly so the
	# Job's budget is visible at the dispatch site.
	export SYNC_ARGS="--changed --yes --state-file /state/compendium-sync-state.json --no-wait --deadline-seconds 2400"
fi

dispatch() {
	JOB_NAME="compendium-sync-$(date +%s)"
	export JOB_NAME
	# shellcheck disable=SC2016  # envsubst needs the literal variable names
	envsubst '${JOB_NAME} ${SYNC_ARGS}' <compendium-sync-job.template.yaml | kubectl apply -f -
	echo "job: ${JOB_NAME} (args: ${SYNC_ARGS})"
}

wait_pod_ready() {
	# kubectl wait errors out if the pod doesn't exist yet — poll for creation first.
	for _ in $(seq 1 60); do
		kubectl get pod -l job-name="${JOB_NAME}" -n compendium --no-headers 2>/dev/null | grep -q . && break
		sleep 2
	done
	# A fast Job may already be Completed (never "ready") — don't fail on the timeout.
	kubectl wait --for=condition=ready pod -l job-name="${JOB_NAME}" -n compendium --timeout=300s >/dev/null 2>&1 || true
}

# Normalized terminal status of ${JOB_NAME}: "complete" | "failed" | "" (still running).
# k3s surfaces SuccessCriteriaMet/FailureTarget before Complete/Failed — treat either as terminal.
job_status() {
	local types
	types=$(kubectl get job "${JOB_NAME}" -n compendium \
		-o jsonpath='{range .status.conditions[?(@.status=="True")]}{.type} {end}' 2>/dev/null || true)
	case " ${types} " in
	*" Complete "* | *" SuccessCriteriaMet "*) echo complete ;;
	*" Failed "* | *" FailureTarget "*) echo failed ;;
	*) echo "" ;;
	esac
}

wait_terminal() {
	local s
	while :; do
		s=$(job_status)
		[ -n "$s" ] && {
			echo "$s"
			return
		}
		sleep 5
	done
}

# Read-only heal report (IMPR-1062 Phase 4): surface the Job's OWN wedge-check
# and heal decisions from its logs. This script never restarts OpenViking —
# the Job holds the single restart breaker; a wedge that the Job could not
# clear is resumed by a rerun, not by a second controller here.
heal_report() {
	local logs
	if ! logs=$(kubectl logs "job/${JOB_NAME}" -n compendium 2>/dev/null); then
		echo "heal report: job logs unavailable" >&2
		return 0
	fi
	if grep -E '^(heal:|wedge check:)' <<<"$logs"; then
		return 0
	fi
	echo "heal report: no wedge-check/heal lines in the job log"
}

dispatch

# Dispatch-and-return unless we need to observe the outcome.
if [ "$FOLLOW" = 0 ] && [ "$HEAL" = 0 ]; then
	exit 0
fi

wait_pod_ready
if [ "$FOLLOW" = 1 ]; then
	kubectl logs -f "job/${JOB_NAME}" -n compendium || true
fi

status=$(wait_terminal)
if [ "$HEAL" = 1 ]; then
	heal_report
fi
if [ "$status" = complete ]; then
	echo "sync completed: ${JOB_NAME}"
	exit 0
fi

echo "job ${JOB_NAME} did not complete cleanly (status: ${status:-unknown}) — inspect: kubectl -n compendium logs job/${JOB_NAME}" >&2
echo "a parked journal resumes with a rerun (or -- --retry-parked); wedge healing is owned by the Job (check its 'heal:' log lines) — do NOT restart ${OV_DEPLOY} in ${OV_NAMESPACE} by hand unless the Job's breaker said it declined" >&2
exit 1
