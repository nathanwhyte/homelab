#!/usr/bin/env bash
# timmy-offline.sh — take timmy out of the cluster cleanly for a planned outage
# (Windows dual-boot session, live-USB disk work) and bring it back.
#
#   timmy-offline.sh down    silence alerts, suspend dependent CronJobs, park cross-node volumes,
#                            spin down memory-heavy services, cordon + drain
#   timmy-offline.sh up      finish the node-maintenance cycle, restore the parked + spun-down workloads,
#                            resume CronJobs, run a catch-up k3s backup, expire the silence
#   timmy-offline.sh status  what is parked/suspended/silenced, what is cordoned, Longhorn health
#
# Why this exists on top of node-maintenance.sh: three Longhorn volumes are
# attached on manu/wemby but their ONLY replica lives on timmy's disk
# (garage/data-garage-0, garage/data-garage-2,
# viking/embedder-cuda-model-cache as of 2026-09-18; viking/reranker-model-cache
# went with the reranker's retirement). Powering timmy off while
# those pods run yanks the iSCSI backing device out from under them — the exact
# LMDB-corruption path from BUG-1033. `down` scales those workloads to zero so
# every such volume detaches cleanly before the drain; `up` restores the saved
# replica counts after timmy is Ready and Longhorn is healthy.
#
# `down` also passes --spin-down to node-maintenance.sh, which scales the
# memory-heavy services (viking/openviking, viking/ov-vectordb) to 0 *before*
# that script's memory-headroom preflight runs. Without it the preflight weighs
# timmy's evicted pods against manu/wemby's free memory with nothing freed and
# blocks the drain outright, so the documented `down` path cannot drain timmy on
# a loaded cluster — BUG-1105's spin-down ordering fix only helps a caller that
# opts in. `up` restores the recorded replica counts along with the parked
# workloads, via node-maintenance.sh finish -> restore_memory_services.
#
# timmy is the only control plane, so between `down` and `up` there is no API
# server — manu and wemby keep running what they already have, nothing else.
#
# `down` also quiets the outage it is about to cause (BUG-1197, 2026-10-02).
# Without this a planned Windows session paged Slack ~50 times: `park` itself
# trips GarageQuorumLost, every CronJob that needs timmy, Garage or OpenViking
# fails while it is out, and each failed Job keeps firing KubeJobFailed hourly
# until its ttl reaps it — k3s-datastore-backup has no ttl by design (IMPR-1126),
# so its failure paged until deleted by hand. Two controls:
#   - an Alertmanager silence for the whole window, recorded by ID so `up`
#     expires exactly that one. It has a hard end (SILENCE_DURATION, default 24h)
#     so a forgotten `up` cannot mute the cluster indefinitely.
#   - QUIESCED_CRONJOBS are suspended; `up` resumes only the ones `down`
#     suspended (a CronJob that was already suspended stays suspended), then
#     runs one catch-up k3s datastore backup so the window leaves no gap.
# Prometheus and Alertmanager are pinned to timmy, so nothing alerts during the
# window anyway; the silence matters for the burst on return, when stale state
# from before the shutdown is evaluated all at once.
#
# The power-off itself is deliberately manual (needs a TTY for sudo):
#   ssh -t timmy sudo shutdown -h now
# or, for a Windows session (os-prober entry — Windows shares the Ubuntu ESP on
# nvme0n1p1; boots Windows exactly once, the following reboot returns to Ubuntu):
#   ssh -t timmy 'sudo grub-reboot "Windows Boot Manager (on /dev/nvme0n1p1)" && sudo reboot'

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
NODE=timmy
STATE_ROOT=${XDG_STATE_HOME:-"$HOME/.local/state"}
STATE_DIR=$STATE_ROOT/homelab-timmy-offline
SCALE_FILE=$STATE_DIR/parked-scales
DETACH_TIMEOUT_SECONDS=${DETACH_TIMEOUT_SECONDS:-300}
READY_TIMEOUT_SECONDS=${READY_TIMEOUT_SECONDS:-600}
KUBECTL=${KUBECTL:-kubectl}
SUSPEND_FILE=$STATE_DIR/suspended-cronjobs
SILENCE_FILE=$STATE_DIR/silence-id
SILENCE_DURATION=${SILENCE_DURATION:-24h}
AM_NAMESPACE=grafana
AM_POD=alertmanager-prom-alertmanager-0

# CronJobs that cannot succeed while timmy is out. Format: namespace/name
QUIESCED_CRONJOBS=(
	kube-system/k3s-datastore-backup # nodeSelector timmy; reads timmy's datastore
	compendium/compendium-sync       # needs viking/openviking, spun down by `down`
	viking/ovlock-janitor            # needs OpenViking and Garage, both down
)

# Workloads whose Longhorn volume lives only on timmy while the pod runs elsewhere,
# plus garage-1 (on timmy) because Garage should stop as a unit, not one node at a time.
# The last two are NOT pinned to timmy: the drain reschedules them onto manu/wemby,
# where Longhorn happily attaches their timmy-only volume over iSCSI (seen 2026-09-02).
# Format: kind/namespace/name
PARKED=(
	statefulset/garage/garage
	deployment/viking/embedder-qwen-cuda
	deployment/omnipendium/omnipendium-db
	deployment/llama/cloud-llm-counter
)

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARN:\033[0m %s\n' "$*"; }
die() {
	printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2
	exit 1
}

usage() {
	sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
	exit 1
}

# --- Longhorn helpers -----------------------------------------------------------

# Volumes attached on another node whose replicas are all on $NODE. Any pod still
# using one of these when timmy powers off loses its disk mid-write.
cross_node_orphans() {
	kubectl get volumes.longhorn.io,replicas.longhorn.io -n longhorn-system -o json |
		jq -r --arg n "$NODE" '.items as $all
			| ($all | map(select(.kind == "Replica")) | group_by(.spec.volumeName)
				| map({key: .[0].spec.volumeName, value: (map(.spec.nodeID) | unique)}) | from_entries) as $nodes
			| $all[] | select(.kind == "Volume")
			| select(.status.state == "attached" and .status.currentNodeID != $n)
			| select(($nodes[.metadata.name] // []) == [$n])
			| "\(.status.kubernetesStatus.namespace)/\(.status.kubernetesStatus.pvcName)\tattached on \(.status.currentNodeID)"'
}

wait_cross_node_detached() {
	local deadline left
	deadline=$((SECONDS + DETACH_TIMEOUT_SECONDS))
	log "waiting for timmy-only volumes attached elsewhere to detach..."
	while ((SECONDS < deadline)); do
		left=$(cross_node_orphans) || die "cannot read Longhorn state"
		[[ -z $left ]] && return 0
		printf '%s\n' "$left" | sed 's/^/    still attached: /'
		sleep 10
	done
	die "volumes still attached after ${DETACH_TIMEOUT_SECONDS}s; not safe to power off"
}

# --- Parking --------------------------------------------------------------------

current_replicas() {
	local kind=$1 ns=$2 name=$3
	kubectl -n "$ns" get "$kind" "$name" -o jsonpath='{.spec.replicas}'
}

park() {
	local entry kind ns name replicas
	mkdir -p "$STATE_DIR"
	if [[ -s $SCALE_FILE ]]; then
		# Re-run of `down` (timmy came back without `up`, e.g. an aborted live-USB
		# session): keep the original saved counts, just re-assert zero.
		warn "parked state already exists — re-asserting 0 replicas, keeping saved counts"
		for entry in "${PARKED[@]}"; do
			IFS=/ read -r kind ns name <<<"$entry"
			grep -q "^$entry	" "$SCALE_FILE" || die "[$entry] is in PARKED but not in $SCALE_FILE; add its replica count by hand"
			replicas=$(current_replicas "$kind" "$ns" "$name") || die "cannot read $entry"
			[[ $replicas == 0 ]] && continue
			log "[$entry] scale $replicas -> 0 (saved count unchanged)"
			kubectl -n "$ns" scale "$kind" "$name" --replicas=0 >/dev/null
		done
		return 0
	fi
	: >"$SCALE_FILE"
	for entry in "${PARKED[@]}"; do
		IFS=/ read -r kind ns name <<<"$entry"
		replicas=$(current_replicas "$kind" "$ns" "$name") || die "cannot read $entry"
		printf '%s\t%s\n' "$entry" "$replicas" >>"$SCALE_FILE"
		if [[ $replicas == 0 ]]; then
			log "[$entry] already at 0"
			continue
		fi
		log "[$entry] scale $replicas -> 0"
		kubectl -n "$ns" scale "$kind" "$name" --replicas=0 >/dev/null
	done
}

node_is_cordoned() {
	[[ $(kubectl get node "$NODE" -o jsonpath='{.spec.unschedulable}') == true ]]
}

redrain() {
	# Second pass: node-maintenance.sh refuses a node that is already cordoned, so
	# drain directly. The instance-manager PDB failure is expected (last-replica
	# volumes); anything else left behind is a real problem.
	local leftovers
	kubectl drain "$NODE" --ignore-daemonsets --delete-emptydir-data --timeout=5m ||
		warn "drain incomplete — assessing what is left on $NODE"
	leftovers=$(kubectl get pods -A -o json --field-selector "spec.nodeName=$NODE" |
		jq -r '.items[] | select((.status.phase // "Unknown") as $p | $p != "Succeeded" and $p != "Failed")
			| select((.metadata.ownerReferences // []) | any(.kind == "DaemonSet") | not)
			| select((.metadata.namespace == "longhorn-system" and (.metadata.name | startswith("instance-manager"))) | not)
			| "\(.metadata.namespace)/\(.metadata.name) (\(.status.phase // "unknown"))"')
	[[ -n $leftovers ]] && die "non-Longhorn pods still on $NODE after drain: $leftovers"
	# Refresh the boot ID node-maintenance.sh `finish` will compare against.
	local boot_id
	boot_id=$(ssh -o ConnectTimeout=5 "$NODE" 'cat /proc/sys/kernel/random/boot_id') || die "cannot read boot ID from $NODE"
	mkdir -p "$STATE_ROOT/homelab-node-maintenance"
	printf '%s\n' "$boot_id" >"$STATE_ROOT/homelab-node-maintenance/$NODE.boot-id"
	log "[$NODE] saved current boot ID for 'up': $boot_id"
}

unpark() {
	local entry kind ns name replicas
	[[ -s $SCALE_FILE ]] || die "no parked state at $SCALE_FILE; nothing to restore"
	while IFS=$'\t' read -r entry replicas; do
		IFS=/ read -r kind ns name <<<"$entry"
		log "[$entry] scale 0 -> $replicas"
		kubectl -n "$ns" scale "$kind" "$name" --replicas="$replicas" >/dev/null
	done <"$SCALE_FILE"
	rm -f "$SCALE_FILE"
}

wait_parked_ready() {
	local entry kind ns name replicas deadline ready
	deadline=$((SECONDS + READY_TIMEOUT_SECONDS))
	for entry in "${PARKED[@]}"; do
		IFS=/ read -r kind ns name <<<"$entry"
		replicas=$(current_replicas "$kind" "$ns" "$name")
		[[ $replicas == 0 ]] && continue
		log "[$entry] waiting for $replicas ready..."
		while ((SECONDS < deadline)); do
			ready=$(kubectl -n "$ns" get "$kind" "$name" -o jsonpath='{.status.readyReplicas}' 2>/dev/null || true)
			[[ ${ready:-0} == "$replicas" ]] && break
			sleep 10
		done
		[[ ${ready:-0} == "$replicas" ]] || warn "[$entry] only ${ready:-0}/$replicas ready after ${READY_TIMEOUT_SECONDS}s"
	done
}

# --- Quieting the outage ---------------------------------------------------------

amtool_exec() {
	"$KUBECTL" -n "$AM_NAMESPACE" exec "$AM_POD" -c alertmanager -- \
		amtool --alertmanager.url=http://localhost:9093 "$@"
}

# Best effort: a silence that cannot be created warns rather than blocking the
# outage, because the alternative is a noisy channel, not an unsafe power-off.
silence_alerts() {
	local id
	mkdir -p "$STATE_DIR"
	if [[ -s $SILENCE_FILE ]]; then
		log "alert silence already recorded ($(<"$SILENCE_FILE")) — not adding another"
		return 0
	fi
	if ! id=$(amtool_exec silence add --duration="$SILENCE_DURATION" \
		--author=timmy-offline.sh \
		--comment="planned timmy outage (timmy-offline.sh down); expired by 'up'" \
		'alertname=~".+"' 2>&1); then
		warn "could not create an Alertmanager silence: $id"
		warn "the outage will page Slack; continuing"
		return 0
	fi
	id=${id//[[:space:]]/}
	if [[ ! $id =~ ^[0-9a-f-]{36}$ ]]; then
		warn "unexpected amtool output, not recording a silence ID: $id"
		return 0
	fi
	printf '%s\n' "$id" >"$SILENCE_FILE"
	log "silenced all alerts for up to $SILENCE_DURATION (silence $id)"
}

expire_silence() {
	local id
	[[ -s $SILENCE_FILE ]] || {
		log "no recorded alert silence"
		return 0
	}
	id=$(<"$SILENCE_FILE")
	if amtool_exec silence expire "$id" >/dev/null 2>&1; then
		log "expired alert silence $id"
		rm -f "$SILENCE_FILE"
	else
		warn "could not expire silence $id — it ends on its own after $SILENCE_DURATION; retry: $0 up"
	fi
}

# Record only the CronJobs this run suspended, so `up` never resumes one that
# was suspended on purpose before the outage.
suspend_cronjobs() {
	local entry ns name current
	mkdir -p "$STATE_DIR"
	touch "$SUSPEND_FILE"
	for entry in "${QUIESCED_CRONJOBS[@]}"; do
		ns=${entry%%/*}
		name=${entry#*/}
		if grep -qxF "$entry" "$SUSPEND_FILE"; then
			current=true # suspended by an earlier `down`; re-assert below
		else
			current=$("$KUBECTL" -n "$ns" get cronjob "$name" -o jsonpath='{.spec.suspend}') ||
				die "cannot read cronjob $entry"
			if [[ $current == true ]]; then
				log "[$entry] already suspended — leaving it for its owner"
				continue
			fi
			printf '%s\n' "$entry" >>"$SUSPEND_FILE"
		fi
		log "[$entry] suspend"
		"$KUBECTL" -n "$ns" patch cronjob "$name" --type=merge -p '{"spec":{"suspend":true}}' >/dev/null ||
			die "cannot suspend cronjob $entry"
	done
}

resume_cronjobs() {
	local entry ns name
	[[ -s $SUSPEND_FILE ]] || {
		log "no CronJobs suspended by down"
		rm -f "$SUSPEND_FILE"
		return 0
	}
	while IFS= read -r entry; do
		[[ -n $entry ]] || continue
		ns=${entry%%/*}
		name=${entry#*/}
		log "[$entry] resume"
		"$KUBECTL" -n "$ns" patch cronjob "$name" --type=merge -p '{"spec":{"suspend":false}}' >/dev/null ||
			die "cannot resume cronjob $entry (state kept in $SUSPEND_FILE)"
	done <"$SUSPEND_FILE"
	rm -f "$SUSPEND_FILE"
}

# The nightly datastore backup is pinned to timmy, so a window spanning 03:30
# skips it. One extra same-day run is harmless: it overwrites state-<date>.db.
catchup_backup() {
	local job
	job=k3s-datastore-backup-catchup-$(date +%Y%m%d%H%M)
	if "$KUBECTL" -n kube-system create job "$job" --from=cronjob/k3s-datastore-backup >/dev/null; then
		log "started catch-up backup job kube-system/$job"
	else
		warn "could not start a catch-up backup; run: kubectl -n kube-system create job <name> --from=cronjob/k3s-datastore-backup"
	fi
}

garage_check() {
	log "garage status"
	kubectl -n garage exec garage-0 -c garage -- /garage status 2>/dev/null |
		sed -n '/HEALTHY NODES/,$p' |
		sed 's/^/    /'
}

# --- Commands -------------------------------------------------------------------

cmd_down() {
	log "preflight: Longhorn must be healthy before parking anything"
	"$SCRIPT_DIR/node-maintenance.sh" status >/dev/null
	# Before park: parking Garage is what trips GarageQuorumLost.
	silence_alerts
	suspend_cronjobs
	park
	wait_cross_node_detached
	if node_is_cordoned; then
		warn "$NODE is already cordoned — re-draining directly instead of via node-maintenance.sh"
		redrain
	else
		log "cordon + drain via node-maintenance.sh (no reboot, pre-drain spin-down)"
		# --spin-down is required here, not optional: it frees headroom for the
		# pods evicted from timmy before the memory-headroom preflight runs.
		"$SCRIPT_DIR/node-maintenance.sh" reboot "$NODE" --no-reboot --spin-down
	fi
	# The drain itself can create new orphans: any unpinned pod evicted from timmy
	# lands on another node and re-attaches its timmy-only volume from there. Check
	# again after the drain and fail closed — the pod's owner must be added to PARKED.
	log "post-drain: re-checking for volumes attached elsewhere with their only replica on $NODE"
	local orphans
	orphans=$(cross_node_orphans) || die "cannot read Longhorn state"
	if [[ -n $orphans ]]; then
		printf '%s\n' "$orphans" | sed 's/^/    /'
		die "NOT safe to power off — scale the owning workloads to 0 and add them to PARKED, then re-check with '$0 status'"
	fi
	echo
	log "timmy is parked, cordoned and drained. Power off when ready:"
	log "    ssh -t $NODE sudo shutdown -h now"
	log "When it is back on Ubuntu:  $0 up"
}

cmd_up() {
	"$SCRIPT_DIR/node-maintenance.sh" finish "$NODE"
	unpark
	wait_parked_ready
	resume_cronjobs
	catchup_backup
	garage_check
	# Last: anything still firing after recovery is real and should page now.
	expire_silence
	log "timmy is back in service."
}

cmd_status() {
	if [[ -s $SCALE_FILE ]]; then
		warn "parked workloads (restore with '$0 up'):"
		sed 's/^/    /' "$SCALE_FILE"
	else
		log "nothing parked"
	fi
	if [[ -s $SUSPEND_FILE ]]; then
		warn "CronJobs suspended by down (resumed by '$0 up'):"
		sed 's/^/    /' "$SUSPEND_FILE"
	fi
	if [[ -s $SILENCE_FILE ]]; then
		warn "alert silence active (expired by '$0 up'): $(<"$SILENCE_FILE")"
	fi
	local orphans
	orphans=$(cross_node_orphans) || die "cannot read Longhorn state"
	if [[ -n $orphans ]]; then
		warn "volumes attached elsewhere whose only replica is on $NODE:"
		printf '%s\n' "$orphans" | sed 's/^/    /'
	else
		log "no cross-node volume depends solely on $NODE"
	fi
	"$SCRIPT_DIR/node-maintenance.sh" status
}

main() {
	case ${1:-} in
	down) cmd_down ;;
	up) cmd_up ;;
	status) cmd_status ;;
	*) usage ;;
	esac
}

# Sourcing (tests/test-timmy-offline-quiet.sh) defines functions only.
if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
	main "$@"
fi
