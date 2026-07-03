#!/usr/bin/env bash
# Sync yt-dlp PVC contents to the local /Volumes/Archive/YouTube/ archive.
#
# Direction: cluster -> local, DELTAS ONLY. Files on local that are not
# on the cluster are LEFT ALONE (the local archive is the historical
# record; the new PVC is a sparse overlay populated by the 6 CronJobs).
#
# Method: spin up a wemby-pinned alpine pod mounting the PVC, build a
# tarball on-pod, copy it out and extract locally, then rsync from
# staging -> local archive. rsync --ignore-existing implements the
# deltas-only contract: any local file with the same path is skipped.
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
#   TMP_DIR     local staging dir              (default: $TMPDIR/yt-dlp-sync)
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
TMP_DIR="${TMP_DIR:-$TMPDIR/yt-dlp-sync}"

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

# Build --exclude args for the tar pipeline (one per excluded subdir).
exclude_args=()
for d in $EXCLUDE_DIRS; do
	exclude_args+=(--exclude="$d")
done

# 1. Ensure local destination exists.
[ -d "$LOCAL" ] || mkdir -p "$LOCAL"
mkdir -p "$TMP_DIR"

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
			command: ["sh", "-c", "sleep 600"],
			resources: {requests: {memory: "256Mi"}, limits: {memory: "512Mi"}},
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
trap cleanup_pod EXIT

echo "Bringing up sync pod $POD_NAME in ns=$NS, mounting $PVC at $REMOTE_ROOT ..."
kubectl run "$POD_NAME" -n "$NS" \
	--image=alpine:3.20 --restart=Never \
	--overrides="$pod_overrides" \
	--command -- sh -c "sleep 600" >/dev/null

kubectl wait --for=condition=Ready "pod/$POD_NAME" -n "$NS" --timeout=60s >/dev/null

# 3. Build tarball inside the pod, then copy it out.
#    Writing the tar on-pod avoids kubectl exec stream truncation on
#    large transfers; the completed file is then streamed reliably.
REMOTE_TAR="$REMOTE_ROOT/.sync-tmp/export.tar"
stage="$TMP_DIR/stage-$$"
mkdir -p "$stage"
if [ -n "$ONLY" ]; then
	if ! kubectl exec -n "$NS" "$POD_NAME" -- test -d "$REMOTE_ROOT/$ONLY" 2>/dev/null; then
		echo "ERROR: subdir '$ONLY' not found under $REMOTE_ROOT on the PVC" >&2
		exit 1
	fi
	echo "Archiving $REMOTE_ROOT/$ONLY/ inside pod ..."
	tar_args=(-cf "$REMOTE_TAR" -C "$REMOTE_ROOT" "${exclude_args[@]}" --exclude=".sync-tmp" "$ONLY")
else
	echo "Archiving $REMOTE_ROOT/ (minus: $EXCLUDE_DIRS) inside pod ..."
	tar_args=(-cf "$REMOTE_TAR" -C "$REMOTE_ROOT" "${exclude_args[@]}" --exclude=".sync-tmp" .)
fi
kubectl exec -n "$NS" "$POD_NAME" -- mkdir -p "$REMOTE_ROOT/.sync-tmp"
kubectl exec -n "$NS" "$POD_NAME" -- tar "${tar_args[@]}"
echo "Copying tarball from pod ..."
kubectl exec -n "$NS" "$POD_NAME" -- cat "$REMOTE_TAR" | tar -xf - -C "$stage"
kubectl exec -n "$NS" "$POD_NAME" -- rm -f "$REMOTE_TAR"

# 4. rsync from stage -> local.
rsync_args=(-a --ignore-existing --itemize-changes --human-readable)
if [ "$DELETE_LOCAL" -eq 1 ]; then
	echo "WARNING: --delete set; files on local with no cluster counterpart will be REMOVED"
	rsync_args+=(--delete)
fi

rsync_summary='
	/^>f/ { copied++ }
	/^\.d/ { dir++ }
	/^[*][^.]/ { changed++ }
'

echo "Syncing stage -> $LOCAL ..."
if [ "$APPLY" -eq 0 ]; then
	echo "DRY-RUN: pass --apply to actually copy."
	rsync "${rsync_args[@]}" --dry-run "$stage/" "$LOCAL/" 2>&1 |
		awk "$rsync_summary"'
			END { printf "DRY-RUN would copy %d new files (skipped %d existing dirs)\n", copied+0, dir+0 }
		'
else
	rsync "${rsync_args[@]}" "$stage/" "$LOCAL/" 2>&1 | tee "$TMP_DIR/last-rsync.log" |
		awk "$rsync_summary"'
			END { printf "Copied %d new files (skipped %d existing dirs, %d would-have-changed)\n", copied+0, dir+0, changed+0 }
		'
fi

# 5. Clean up old staging dirs (keep the current run for inspection).
find "$TMP_DIR" -maxdepth 1 -name 'stage-*' -not -name "stage-$$" -type d -exec rm -rf {} + 2>/dev/null || true

echo
echo "Done. Stage retained at: $stage"
if [ "$APPLY" -eq 0 ]; then
	echo "Re-run with --apply to actually copy. Add --delete to also drop"
	echo "local files that no longer exist on the cluster."
fi
