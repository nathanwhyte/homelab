#!/usr/bin/env bash
# macbook-backup-check — say so when the MacBook backup stops working.
#
# WHY THIS EXISTS (BUG-1212)
#   The mirror failed every source for 47 days and nothing said so. The only
#   monitor, a Hermes cron job, had been paused since 2026-07-03, and the
#   attempt to replace it through the cluster could not work at all: pop's
#   Alloy never reached the cluster's remote-write NodePorts, so the metric
#   that would have carried this never left the machine (mac/RETIRED.md).
#   A round trip through a transport that is itself unmonitored was the wrong
#   shape. This watches the marker locally, on pop, where the backup runs and
#   where the answer already is.
#
# WHAT IT READS
#   ~/Library/Application Support/macbook-backup/last-success is stamped by
#   bin/macbook-backup.sh only when EVERY source uploaded cleanly, so its age
#   answers "is the backup working" rather than "did the job start". A missing
#   marker counts as stale — that is the BUG-1086 shape, where the job itself
#   is gone and there is nothing to fail.
#
# BEHAVIOUR
#   Healthy: silent, exits 0. Stale: one macOS notification, then at most one
#   a day while it stays stale. Either way one line in
#   ~/Library/Logs/macbook-backup-check.log. Always exits 0, so launchd does
#   not treat a stale backup as a reason to retry-loop.
#
# ENVIRONMENT — every value overridable, `:-` semantics
#   MACBOOK_BACKUP_MARKER     marker to read (default: the pop path below)
#   MACBOOK_BACKUP_MAX_AGE_S  staleness threshold in seconds (default 86400)
#   MACBOOK_BACKUP_STATE_DIR  where the once-a-day state file lives
#   MACBOOK_BACKUP_LOG        log path
set -uo pipefail

# -n|--dry-run: log what would happen and never raise the notification, so the
# stale path can be exercised (by hand or by a test) without alarming anyone.
DRY_RUN=0
case "${1:-}" in
-n | --dry-run) DRY_RUN=1 ;;
"") ;;
*)
	echo "usage: check-macbook-backup.sh [-n|--dry-run]" >&2
	exit 2
	;;
esac

MARKER="${MACBOOK_BACKUP_MARKER:-$HOME/Library/Application Support/macbook-backup/last-success}"
MAX_AGE="${MACBOOK_BACKUP_MAX_AGE_S:-86400}"
STATE_DIR="${MACBOOK_BACKUP_STATE_DIR:-$HOME/Library/Application Support/macbook-backup}"
STATE="$STATE_DIR/check-last-notified"
LOG="${MACBOOK_BACKUP_LOG:-$HOME/Library/Logs/macbook-backup-check.log}"

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "macbook-backup-check: $*" >>"$LOG"; }

now=$(date +%s)
age=""
if [[ -f "$MARKER" ]]; then
	age=$((now - $(stat -f %m "$MARKER")))
	reason="last clean run was $((age / 3600))h ago"
else
	reason="no last-success marker exists"
fi

# --- healthy -------------------------------------------------------------
if [[ -n "$age" ]] && ((age < MAX_AGE)); then
	log "ok: $reason"
	# Clearing the state means the next staleness notifies at once rather than
	# inheriting a stale "already told them" timestamp.
	rm -f "$STATE"
	exit 0
fi

# --- stale, or never ran -------------------------------------------------
if [[ -f "$STATE" ]]; then
	since=$((now - $(stat -f %m "$STATE")))
	if ((since < MAX_AGE)); then
		log "stale: $reason (already notified $((since / 3600))h ago)"
		exit 0
	fi
fi

log "STALE: $reason -- notifying"
mkdir -p "$STATE_DIR" 2>/dev/null || true
if ((DRY_RUN == 1)); then
	log "dry-run: would notify and stamp $STATE"
	exit 0
fi
/usr/bin/osascript \
	-e "display notification \"$reason\" with title \"macbook-backup is stale\"" \
	>/dev/null 2>&1 || log "warn: osascript notification failed"
touch "$STATE"
exit 0
