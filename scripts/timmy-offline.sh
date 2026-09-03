#!/usr/bin/env bash
# timmy-offline.sh — take timmy out of the cluster cleanly for a planned outage
# (Windows dual-boot session, live-USB disk work) and bring it back.
#
#   timmy-offline.sh down    park cross-node Garage/model-cache volumes, then cordon + drain timmy
#   timmy-offline.sh up      finish the node-maintenance cycle, restore the parked workloads
#   timmy-offline.sh status  what is parked, what is cordoned, Longhorn health
#
# Why this exists on top of node-maintenance.sh: three Longhorn volumes are
# attached on manu/wemby but their ONLY replica lives on timmy's disk
# (garage/data-garage-0, garage/data-garage-2, viking/reranker-model-cache,
# viking/embedder-cuda-model-cache as of 2026-09-02). Powering timmy off while
# those pods run yanks the iSCSI backing device out from under them — the exact
# LMDB-corruption path from BUG-1033. `down` scales those workloads to zero so
# every such volume detaches cleanly before the drain; `up` restores the saved
# replica counts after timmy is Ready and Longhorn is healthy.
#
# timmy is the only control plane, so between `down` and `up` there is no API
# server — manu and wemby keep running what they already have, nothing else.
#
# The power-off itself is deliberately manual (needs a TTY for sudo):
#   ssh -t timmy sudo shutdown -h now
# or, for a Windows session (GRUB entry from /etc/grub.d/40_custom, boots
# Windows exactly once and returns to Ubuntu on the following reboot):
#   ssh -t timmy 'sudo grub-reboot "Windows Boot Manager" && sudo reboot'

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
NODE=timmy
STATE_ROOT=${XDG_STATE_HOME:-"$HOME/.local/state"}
STATE_DIR=$STATE_ROOT/homelab-timmy-offline
SCALE_FILE=$STATE_DIR/parked-scales
DETACH_TIMEOUT_SECONDS=${DETACH_TIMEOUT_SECONDS:-300}
READY_TIMEOUT_SECONDS=${READY_TIMEOUT_SECONDS:-600}

# Workloads whose Longhorn volume lives only on timmy while the pod runs elsewhere,
# plus garage-1 (on timmy) because Garage should stop as a unit, not one node at a time.
# The last two are NOT pinned to timmy: the drain reschedules them onto manu/wemby,
# where Longhorn happily attaches their timmy-only volume over iSCSI (seen 2026-09-02).
# Format: kind/namespace/name
PARKED=(
	statefulset/garage/garage
	deployment/viking/reranker-bge
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
	sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'
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
	park
	wait_cross_node_detached
	if node_is_cordoned; then
		warn "$NODE is already cordoned — re-draining directly instead of via node-maintenance.sh"
		redrain
	else
		log "cordon + drain via node-maintenance.sh (no reboot)"
		"$SCRIPT_DIR/node-maintenance.sh" reboot "$NODE" --no-reboot
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
	garage_check
	log "timmy is back in service."
}

cmd_status() {
	if [[ -s $SCALE_FILE ]]; then
		warn "parked workloads (restore with '$0 up'):"
		sed 's/^/    /' "$SCALE_FILE"
	else
		log "nothing parked"
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

main "$@"
