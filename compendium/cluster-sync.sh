#!/usr/bin/env bash
# Run a compendium→OV sync from inside the cluster — the SINGLE sync writer
# for the compendium OV namespace (BUG-1034 phase 2).
#
#   compendium/cluster-sync.sh [--follow] [--heal[=N]] [-- <compendium-sync.py sync args>]
#
# With no args, runs the canonical manual sync:
#   sync --changed --yes --state-file /state/compendium-sync-state.json --no-wait
# deriving the work list from git history since the last-synced commit on the
# state PVC. The runner only sees pushed origin/main — push first.
#
# Flags:
#   --follow    Stream the Job's logs and wait for it to complete.
#   --heal[=N]  Wait for the Job; if it fails on the OpenViking orphaned-lock
#               signature (CONFLICT: Resource is busy / LockAcquisitionError —
#               BUG-1039: a killed sync pod leaves stale in-process tree locks in
#               the long-lived openviking process), restart deploy/openviking to
#               drop those locks and re-dispatch. Bounded to N restarts (default
#               2). Restarts ONLY on the lock signature — never on other failures.
#               Implies waiting for the Job; composes with --follow.
#
# Examples:
#   compendium/cluster-sync.sh --follow
#   compendium/cluster-sync.sh --heal
#   compendium/cluster-sync.sh --follow --heal=3
#   compendium/cluster-sync.sh -- --include-active --no-wait "bugs/dipdash/BUG-004-*.md"
#
# Bootstraps (idempotent): compendium namespace + state PVC, secret
# compendium-git-token (copied from hermes/github-access-token), secret
# openviking-api-key (copied from viking/openviking-api-key — Secrets don't
# cross namespaces).
set -euo pipefail

OV_NAMESPACE=viking
OV_DEPLOY=openviking

case "${1:-}" in
-h | --help)
	# Print the header docstring (usage + flags + examples) without dispatching a Job.
	sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'
	exit 0
	;;
esac

cd "$(dirname "$0")"

FOLLOW=0
HEAL=0
HEAL_MAX=2
while [ "$#" -gt 0 ]; do
	case "$1" in
	--follow)
		FOLLOW=1
		shift
		;;
	--heal)
		HEAL=1
		shift
		;;
	--heal=*)
		HEAL=1
		HEAL_MAX="${1#--heal=}"
		shift
		;;
	--)
		shift
		break
		;;
	*) break ;;
	esac
done

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

if [ "$#" -gt 0 ]; then
	export SYNC_ARGS="$*"
else
	export SYNC_ARGS="--changed --yes --state-file /state/compendium-sync-state.json --no-wait"
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

# True when ${JOB_NAME}'s logs carry the BUG-1039 orphaned-lock signature.
lock_failure() {
	kubectl logs "job/${JOB_NAME}" -n compendium 2>/dev/null |
		grep -qiE 'Resource is busy|LockAcquisitionError|Failed to acquire tree lock'
}

restart_ov() {
	echo "orphaned-lock failure detected (BUG-1039) — restarting ${OV_DEPLOY} in ${OV_NAMESPACE} to drop stale in-process tree locks" >&2
	kubectl rollout restart "deploy/${OV_DEPLOY}" -n "${OV_NAMESPACE}"
	kubectl rollout status "deploy/${OV_DEPLOY}" -n "${OV_NAMESPACE}" --timeout=180s
}

heal_attempts=0
while :; do
	dispatch

	# Fire-and-forget unless we need to observe the outcome.
	if [ "$FOLLOW" = 0 ] && [ "$HEAL" = 0 ]; then
		break
	fi

	wait_pod_ready
	if [ "$FOLLOW" = 1 ]; then
		kubectl logs -f "job/${JOB_NAME}" -n compendium || true
	fi

	status=$(wait_terminal)
	if [ "$status" = complete ]; then
		echo "sync completed: ${JOB_NAME}"
		break
	fi

	# Failed. Self-heal ONLY on the orphaned-lock signature, within the restart budget.
	if [ "$HEAL" = 1 ] && [ "$heal_attempts" -lt "$HEAL_MAX" ] && lock_failure; then
		heal_attempts=$((heal_attempts + 1))
		echo "heal ${heal_attempts}/${HEAL_MAX}: ${JOB_NAME} failed on orphaned OV locks — restarting and retrying" >&2
		restart_ov
		continue
	fi

	echo "job ${JOB_NAME} did not complete cleanly (status: ${status:-unknown}) — inspect: kubectl -n compendium logs job/${JOB_NAME}" >&2
	if [ "$HEAL" = 1 ] && [ "$heal_attempts" -ge "$HEAL_MAX" ]; then
		echo "heal budget (${HEAL_MAX} restart(s)) exhausted — orphaned locks may persist; check ${OV_DEPLOY} in ${OV_NAMESPACE}" >&2
	fi
	exit 1
done
