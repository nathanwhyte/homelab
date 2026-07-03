#!/usr/bin/env bash
# Sync yt-dlp PVC contents to the local /Volumes/Archive/YouTube/ archive.
#
# Direction: cluster -> local, DELTAS ONLY. Files on local that are not
# on the cluster are LEFT ALONE (the local archive is the historical
# record; the new PVC is a sparse overlay populated by the 6 CronJobs).
#
# Method: spin up a wemby-pinned alpine pod with rsync installed,
# mounting the PVC. Use rsync with kubectl exec as the transport so
# rsync handles its own chunking — no tar streaming, no truncation.
# rsync --ignore-existing implements the deltas-only contract: any
# local file with the same path is skipped.
#
# Idempotent. Safe to re-run on a schedule; only new cluster files
# incur network/disk cost.
#
# Usage:
#   media/yt-dlp/sync-to-archive.sh              # default: dry-run summary
#   media/yt-dlp/sync-to-archive.sh --apply      # actually rsync
#   media/yt-dlp/sync-to-archive.sh --apply --delete  # also drop local
#                                                     # files that no
#                                                     # longer exist on
#                                                     # cluster (DANGER)
#   NS=yt-dlp PVC=media REMOTE_ROOT=/downloads \
#     LOCAL=/Volumes/Archive/YouTube \
#     media/yt-dlp/sync-to-archive.sh --apply
#   LOCAL=~/Music/YouTube ONLY=Study \
#     media/yt-dlp/sync-to-archive.sh --apply
#                                                 # alternative target:
#                                                 # copy just the Study
#                                                 # playlist (audio) to
#                                                 # ~/Music/YouTube/Study
#
# Environment:
#   NS          yt-dlp namespace              (default: yt-dlp)
#   PVC         PVC claim name                 (default: media)
#   REMOTE_ROOT path inside the pod to sync    (default: /downloads)
#   LOCAL       local destination dir          (default: /Volumes/Archive/YouTube)
#   EXCLUDE_DIRS space-separated subdirs under REMOTE_ROOT to skip
#                                           (default: "ad-hoc")
#   POD_NAME    pod name to use                (default: yt-dlp-sync)
#   ONLY        restrict sync to one subdir of REMOTE_ROOT (e.g. "Study");
#               the subdir is preserved under LOCAL (default: empty = all)

set -euo pipefail

NS="${NS:-yt-dlp}"
PVC="${PVC:-media}"
REMOTE_ROOT="${REMOTE_ROOT:-/downloads}"
LOCAL="${LOCAL:-/Volumes/Archive/YouTube}"
EXCLUDE_DIRS="${EXCLUDE_DIRS:-ad-hoc}"
ONLY="${ONLY:-}"
POD_NAME="${POD_NAME:-yt-dlp-sync}"

APPLY=0
DELETE_LOCAL=0
for arg in "$@"; do
	case "$arg" in
	--apply) APPLY=1 ;;
	--delete) DELETE_LOCAL=1 ;;
	-h | --help)
		sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'
		exit 0
		;;
	*)
		echo "unknown flag: $arg" >&2
		exit 64
		;;
	esac
done

for cmd in kubectl rsync jq; do
	command -v "$cmd" >/dev/null 2>&1 || {
		echo "missing required command: $cmd" >&2
		exit 127
	}
done

# 1. Ensure local destination exists.
[ -d "$LOCAL" ] || mkdir -p "$LOCAL"

# 2. Spin up a pod with rsync installed, mounting the PVC.
pod_overrides=$(jq -n \
	--arg root "$REMOTE_ROOT" \
	--arg pvc "$PVC" \
	'{spec: {
		nodeName: "wemby",
		restartPolicy: "Never",
		securityContext: {fsGroup: 1000},
		containers: [{
			name: "sync",
			image: "alpine:3.20",
			command: ["sh", "-c", "apk add --no-cache rsync >/dev/null 2>&1 && sleep 3600"],
			volumeMounts: [{name: "media", mountPath: $root}]
		}],
		volumes: [{
			name: "media",
			persistentVolumeClaim: {claimName: $pvc}
		}]
	}}')

cleanup_pod() {
	kubectl delete pod "$POD_NAME" -n "$NS" \
		--grace-period=0 --force >/dev/null 2>&1 || true
}

if kubectl get pod "$POD_NAME" -n "$NS" >/dev/null 2>&1; then
	cleanup_pod
	kubectl wait --for=delete "pod/$POD_NAME" -n "$NS" --timeout=30s 2>/dev/null || sleep 2
fi
trap cleanup_pod EXIT

echo "Bringing up sync pod $POD_NAME in ns=$NS, mounting $PVC at $REMOTE_ROOT ..."
kubectl run "$POD_NAME" -n "$NS" \
	--image=alpine:3.20 --restart=Never \
	--overrides="$pod_overrides" \
	--command -- sh -c "apk add --no-cache rsync >/dev/null 2>&1 && sleep 3600" >/dev/null

kubectl wait --for=condition=Ready "pod/$POD_NAME" -n "$NS" --timeout=60s >/dev/null

# Wait for rsync to be installed (apk runs in the entrypoint).
echo "Waiting for rsync to install in pod ..."
for _ in $(seq 1 30); do
	if kubectl exec -n "$NS" "$POD_NAME" -- which rsync >/dev/null 2>&1; then
		break
	fi
	sleep 1
done
if ! kubectl exec -n "$NS" "$POD_NAME" -- which rsync >/dev/null 2>&1; then
	echo "ERROR: rsync failed to install in pod" >&2
	exit 1
fi

# 3. Build rsync args.
src="$REMOTE_ROOT/"
if [ -n "$ONLY" ]; then
	if ! kubectl exec -n "$NS" "$POD_NAME" -- test -d "$REMOTE_ROOT/$ONLY" 2>/dev/null; then
		echo "ERROR: subdir '$ONLY' not found under $REMOTE_ROOT on the PVC" >&2
		exit 1
	fi
	src="$REMOTE_ROOT/$ONLY"
	# Preserve the subdir name under LOCAL.
	LOCAL="$LOCAL/$ONLY"
	[ -d "$LOCAL" ] || mkdir -p "$LOCAL"
fi

rsync_args=(-av --ignore-existing --human-readable)
for d in $EXCLUDE_DIRS; do
	rsync_args+=(--exclude="$d")
done
if [ "$DELETE_LOCAL" -eq 1 ]; then
	echo "WARNING: --delete set; files on local with no cluster counterpart will be REMOVED"
	rsync_args+=(--delete)
fi
if [ "$APPLY" -eq 0 ]; then
	echo "DRY-RUN: pass --apply to actually copy."
	rsync_args+=(--dry-run)
fi

# 4. rsync using kubectl exec as the remote shell transport.
#    rsync -e expects an ssh-like command that takes "host cmd..."
#    args; this wrapper ignores the host and execs the rest in the pod.
rsh_wrapper=$(mktemp)
cat >"$rsh_wrapper" <<WRAPPER
#!/bin/sh
shift  # discard the host arg
exec kubectl exec -i -n "$NS" "$POD_NAME" -- "\$@"
WRAPPER
chmod +x "$rsh_wrapper"

echo "Syncing $src -> $LOCAL ..."
rsync "${rsync_args[@]}" \
	-e "$rsh_wrapper" \
	pod:"$src/" "$LOCAL/"
rm -f "$rsh_wrapper"

echo
echo "Done."
if [ "$APPLY" -eq 0 ]; then
	echo "Re-run with --apply to actually copy. Add --delete to also drop"
	echo "local files that no longer exist on the cluster."
fi
