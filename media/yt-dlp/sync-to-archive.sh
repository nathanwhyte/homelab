#!/usr/bin/env bash
# Sync yt-dlp PVC contents to the local /Volumes/Archive/YouTube/ archive.
#
# Direction: cluster -> local, DELTAS ONLY. Files on local that are not
# on the cluster are LEFT ALONE (the local archive is the historical
# record; the new PVC is a sparse overlay populated by the 6 CronJobs).
#
# Method: spin up a wemby-pinned alpine pod, tar up /downloads/ minus
# the throwaway ad-hoc/ subdir, kubectl cp the tarball to a local tmp
# dir, then rsync from tmp -> /Volumes/Archive/YouTube/. rsync's
# --ignore-existing implements the deltas-only contract: any local
# file with the same path is skipped, even if its mtime/size differ.
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

set -euo pipefail

NS="${NS:-yt-dlp}"
PVC="${PVC:-media}"
REMOTE_ROOT="${REMOTE_ROOT:-/downloads}"
LOCAL="${LOCAL:-/Volumes/Archive/YouTube}"
EXCLUDE_DIRS="${EXCLUDE_DIRS:-ad-hoc}"
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

command -v kubectl >/dev/null 2>&1 || {
	echo "missing required command: kubectl" >&2
	exit 127
}
command -v rsync >/dev/null 2>&1 || {
	echo "missing required command: rsync" >&2
	exit 127
}

# Build --exclude args for the tar pipeline (one per excluded subdir).
exclude_args=()
for d in $EXCLUDE_DIRS; do
	exclude_args+=(--exclude="$d")
done

# 1. Local destination must exist; create it if missing.
[ -d "$LOCAL" ] || mkdir -p "$LOCAL"
mkdir -p "$TMP_DIR"

# 2. Build pod overrides via printf. Inline-JSON-construction via
#    shell quoting is brittle (the line-continuation with `'"` segments
#    confuses `sh -c` when invoked via bash -c on macOS); a one-shot
#    printf is the path of least surprise.
pod_overrides=$(printf '%s' \
	'{"spec":{"nodeName":"wemby","restartPolicy":"Never",' \
	'"securityContext":{"fsGroup":1000},' \
	'"containers":[{"name":"sync","image":"alpine:3.20",' \
	'"command":["sh","-c","sleep 600"],' \
	'"volumeMounts":[{"name":"media","mountPath":"'"$REMOTE_ROOT"'"}]}],' \
	'"volumes":[{"name":"media",' \
	'"persistentVolumeClaim":{"claimName":"'"$PVC"'"}}]}}')

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

# Wait for the pod to be Running (kubectl run returns before the
# container is ready; kubectl cp would fail against a NotReady pod).
for _ in $(seq 1 60); do
	phase=$(kubectl get pod "$POD_NAME" -n "$NS" \
		-o jsonpath='{.status.phase}' 2>/dev/null || true)
	if [ "$phase" = "Running" ]; then
		break
	fi
	sleep 1
done
if [ "$phase" != "Running" ]; then
	echo "ERROR: pod $POD_NAME did not reach Running (last phase: $phase)" >&2
	exit 1
fi

# 3. Run tar inside the pod to stream the tree to stdout, redirect
#    through kubectl exec to our local file. (kubectl cp uses tar
#    under the hood; piping tar through kubectl exec gives the same
#    effect but lets us pick the exclude list explicitly.)
local_tar="$TMP_DIR/yt-dlp-cluster-$(date +%Y%m%d-%H%M%S).tar"
echo "Streaming $REMOTE_ROOT/ (minus: $EXCLUDE_DIRS) from pod to $local_tar ..."

# tar's -C flag treats the path as relative; we cd into REMOTE_ROOT
# so the archive layout mirrors the destination dir names exactly.
tar_args=(-cf - -C "$REMOTE_ROOT" "${exclude_args[@]}" .)
kubectl exec -n "$NS" "$POD_NAME" -- tar "${tar_args[@]}" \
	>"$local_tar"

# 4. Extract the tar into a sibling staging dir (so rsync sees a
#    real tree, not nested in a tarball).
stage="$TMP_DIR/stage-$$"
mkdir -p "$stage"
tar -xf "$local_tar" -C "$stage"
rm -f "$local_tar"

# 5. rsync from stage -> local. --ignore-existing implements
#    "deltas only, never overwrite or delete local files".
#    --itemize-changes prints one line per action, which we grep
#    to count deltas (>) and skips (.).
rsync_args=(-a --ignore-existing --itemize-changes --human-readable)
if [ "$DELETE_LOCAL" -eq 1 ]; then
	echo "WARNING: --delete set; files on local with no cluster counterpart will be REMOVED"
	rsync_args+=(--delete)
fi

echo "Syncing stage -> $LOCAL ..."
if [ "$APPLY" -eq 0 ]; then
	echo "DRY-RUN: pass --apply to actually copy."
	rsync "${rsync_args[@]}" --dry-run "$stage/" "$LOCAL/" 2>&1 |
		awk '
			/^>f/ { copied++ }
			/^\.d/ { dir++ }
			/^[*][^.]/ { changed++ }
			END {
				printf "DRY-RUN would copy %d new files (skipped %d existing dirs)\n",
					copied+0, dir+0
			}
		'
else
	rsync "${rsync_args[@]}" "$stage/" "$LOCAL/" 2>&1 | tee "$TMP_DIR/last-rsync.log" |
		awk -v log="$TMP_DIR/last-rsync.log" '
			/^>f/ { copied++ }
			/^\.d/ { dir++ }
			/^[*][^.]/ { changed++ }
			END {
				printf "Copied %d new files (skipped %d existing dirs, %d would have-changed)\n",
					copied+0, dir+0, changed+0
			}
		'
fi

# 6. Tidy up: keep the stage dir in TMP_DIR for inspection but
#    delete older tarballs. Latest run's stage is preserved.
find "$TMP_DIR" -maxdepth 1 -name 'yt-dlp-cluster-*.tar' -mmin +60 -delete 2>/dev/null || true

echo
echo "Done. Stage retained at: $stage"
echo "Re-run with --apply to actually copy. Add --delete to also drop"
echo "local files that no longer exist on the cluster."
