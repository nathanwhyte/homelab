#!/usr/bin/env bash
# node-maintenance.sh — apt updates and serialized reboots for the k3s cluster.
#
# Phase 1 (non-disruptive, any time, nodes updated serially):
#   node-maintenance.sh apt [node ...]      apt update + full-upgrade + autoremove, report reboot-required
#   node-maintenance.sh status              node readiness, reboot-required, Longhorn volume health
#
# Phase 2 (disruptive, one node per invocation — agents first, timmy last):
#   node-maintenance.sh reboot <node>       preflight → cordon → drain → reboot → uncordon → health gate
#   node-maintenance.sh reboot <node> --no-reboot   stop after drain (reboot manually, then: finish <node>)
#   node-maintenance.sh finish <node> [previous-boot-id]
#                                             verify reboot → wait for Ready → uncordon → health gate
#
# Reboot order matters: wemby → manu → timmy. timmy is the only control plane,
# so its reboot takes the API down — run it last and let the wait loop tolerate
# kubectl outages. The Longhorn health gate at the end of each cycle must pass
# before starting the next node, or a later drain can strand the last replica.
#
# TODO(memory preflight): draining a node reschedules its workloads onto the
# others; on 2026-07-20 wemby's drain OOM-froze timmy (Ollama model cache +
# OpenViking already resident). Before draining, spin down memory-heavy
# services that can sit out the window (llama/ollama, viking OV + vectordb),
# and add a preflight comparing the target node's pod memory against
# allocatable-minus-used headroom on the remaining nodes.

set -euo pipefail

NODES=(wemby manu timmy)
DRAIN_TIMEOUT=5m # evictions settle in ~2m; the rest would only be spent retrying pinned instance-managers
REBOOT_TIMEOUT_SECONDS=${REBOOT_TIMEOUT_SECONDS:-600}
READY_TIMEOUT_SECONDS=${READY_TIMEOUT_SECONDS:-600}
SSH_OPTS=(-o ConnectTimeout=5)
STATE_ROOT=${XDG_STATE_HOME:-"$HOME/.local/state"}
MAINTENANCE_STATE_DIR=$STATE_ROOT/homelab-node-maintenance
MAINTENANCE_LOCK_DIR=$MAINTENANCE_STATE_DIR/lock
LOCK_HELD=0

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARN:\033[0m %s\n' "$*"; }
die() {
	printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2
	exit 1
}

usage() {
	sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
	exit 1
}

require_node() {
	local n
	for n in "${NODES[@]}"; do
		[[ $n == "$1" ]] && return 0
	done
	die "unknown node '$1' (expected one of: ${NODES[*]})"
}

# --- Cycle state and serialization --------------------------------------------

cycle_state_file() {
	printf '%s/%s.boot-id\n' "$MAINTENANCE_STATE_DIR" "$1"
}

save_cycle_boot_id() {
	local node=$1 boot_id=$2
	mkdir -p "$MAINTENANCE_STATE_DIR"
	printf '%s\n' "$boot_id" >"$(cycle_state_file "$node")"
}

load_cycle_boot_id() {
	local node=$1 file boot_id
	file=$(cycle_state_file "$node")
	[[ -s $file ]] || die "no saved reboot state for $node; pass its previous boot ID explicitly"
	IFS= read -r boot_id <"$file"
	[[ -n $boot_id ]] || die "saved reboot state for $node is empty"
	printf '%s\n' "$boot_id"
}

clear_cycle_state() {
	rm -f "$(cycle_state_file "$1")"
}

release_maintenance_lock() {
	((LOCK_HELD)) || return 0
	rm -f "$MAINTENANCE_LOCK_DIR/pid"
	rmdir "$MAINTENANCE_LOCK_DIR" 2>/dev/null || true
	LOCK_HELD=0
}

acquire_maintenance_lock() {
	local owner_pid=
	mkdir -p "$MAINTENANCE_STATE_DIR"
	if ! mkdir "$MAINTENANCE_LOCK_DIR" 2>/dev/null; then
		[[ -r $MAINTENANCE_LOCK_DIR/pid ]] && IFS= read -r owner_pid <"$MAINTENANCE_LOCK_DIR/pid"
		if [[ $owner_pid =~ ^[0-9]+$ ]] && kill -0 "$owner_pid" 2>/dev/null; then
			die "another node-maintenance process is active (pid $owner_pid)"
		fi
		warn "removing stale maintenance lock${owner_pid:+ from pid $owner_pid}"
		rm -f "$MAINTENANCE_LOCK_DIR/pid"
		rmdir "$MAINTENANCE_LOCK_DIR" 2>/dev/null || die "cannot remove stale lock: $MAINTENANCE_LOCK_DIR"
		mkdir "$MAINTENANCE_LOCK_DIR" || die "cannot acquire maintenance lock"
	fi
	printf '%s\n' "$$" >"$MAINTENANCE_LOCK_DIR/pid"
	LOCK_HELD=1
	trap release_maintenance_lock EXIT
	trap 'exit 130' INT
	trap 'exit 143' TERM
}

cordoned_nodes() {
	kubectl get nodes -o json |
		jq -r '.items[] | select(.spec.unschedulable == true) | .metadata.name'
}

require_no_cordoned_nodes() {
	local cordoned
	cordoned=$(cordoned_nodes) || die "cannot determine whether another maintenance cycle is active"
	[[ -z $cordoned ]] || die "cordoned node(s) already exist: ${cordoned//$'\n'/, }; finish or back out that cycle first"
}

require_only_target_cordoned() {
	local node=$1 cordoned
	cordoned=$(cordoned_nodes) || die "cannot determine the active maintenance node"
	[[ $cordoned == "$node" ]] || {
		[[ -n $cordoned ]] || die "$node is not cordoned; refusing to run finish"
		die "expected only $node to be cordoned, found: ${cordoned//$'\n'/, }"
	}
}

require_reboot_order() {
	local target=$1 node rc
	for node in "${NODES[@]}"; do
		[[ $node == "$target" ]] && return 0
		if ssh "${SSH_OPTS[@]}" "$node" '[ -f /var/run/reboot-required ]' 2>/dev/null; then
			die "$node still requires reboot; complete nodes in order: ${NODES[*]}"
		else
			rc=$?
			((rc == 1)) || die "cannot verify reboot state on $node; refusing to skip it"
		fi
	done
}

remote_boot_id() {
	ssh "${SSH_OPTS[@]}" "$1" 'cat /proc/sys/kernel/random/boot_id'
}

normalize_boot_id() {
	# Lowercase and strip whitespace so case or formatting differences in a
	# manually supplied ID cannot defeat (or falsely prove) the comparison.
	printf '%s' "$1" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]'
}

is_valid_boot_id() {
	[[ $1 =~ ^[[:xdigit:]]{8}-[[:xdigit:]]{4}-[[:xdigit:]]{4}-[[:xdigit:]]{4}-[[:xdigit:]]{12}$ ]]
}

require_valid_boot_id() {
	is_valid_boot_id "$1" || die "invalid previous boot ID '$1'"
}

wait_for_new_boot() {
	local node=$1 previous_boot_id current_boot_id deadline
	previous_boot_id=$(normalize_boot_id "$2")
	require_valid_boot_id "$previous_boot_id"
	deadline=$((SECONDS + REBOOT_TIMEOUT_SECONDS))
	log "[$node] waiting for reboot proof (boot ID must change)..."
	while ((SECONDS < deadline)); do
		current_boot_id=$(remote_boot_id "$node" 2>/dev/null) || {
			sleep 10
			continue
		}
		current_boot_id=$(normalize_boot_id "$current_boot_id")
		# Malformed output (SSH banners, truncated reads) is not proof — retry.
		if ! is_valid_boot_id "$current_boot_id"; then
			sleep 10
			continue
		fi
		if [[ $current_boot_id != "$previous_boot_id" ]]; then
			log "[$node] reboot verified (boot ID changed)."
			return 0
		fi
		sleep 10
	done
	die "[$node] boot ID did not change within ${REBOOT_TIMEOUT_SECONDS}s; leaving the node cordoned"
}

wait_for_node_ready() {
	local node=$1 ready deadline
	deadline=$((SECONDS + READY_TIMEOUT_SECONDS))
	log "[$node] waiting for k8s Ready..."
	while ((SECONDS < deadline)); do
		# kubectl errors are tolerated here — rebooting timmy takes the API down.
		ready=$(kubectl get node "$node" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null) || true
		[[ $ready == True ]] && return 0
		sleep 10
	done
	die "[$node] did not become Ready within ${READY_TIMEOUT_SECONDS}s; leaving the node cordoned"
}

# --- Longhorn -----------------------------------------------------------------

longhorn_unhealthy() {
	# Prints active volumes that are not stably healthy; empty output = all clear.
	# Detached volumes are inactive and exempt. Everything else — attaching,
	# detaching, faulted, or attached-but-degraded — counts as unhealthy so the
	# gate cannot pass while storage is still in transition.
	kubectl get volumes.longhorn.io -n longhorn-system -o json |
		jq -r '.items[] | select(.status.state != "detached")
			| select((.status.state == "attached" and .status.robustness == "healthy") | not)
			| "\(.metadata.name)\t\(.status.state)/\(.status.robustness)"'
}

wait_longhorn_healthy() {
	log "Waiting for all attached Longhorn volumes to be healthy..."
	local bad
	while true; do
		bad=$(longhorn_unhealthy) || {
			warn "kubectl unavailable, retrying..."
			sleep 10
			continue
		}
		[[ -z $bad ]] && break
		printf '%s\n' "$bad" | sed 's/^/    degraded: /'
		sleep 15
	done
	log "Longhorn volumes healthy."
}

# --- Preflight ----------------------------------------------------------------

pdb_preflight() {
	# Flag pods on this node covered by a PDB that currently allows 0 disruptions.
	# Longhorn's instance-manager PDBs are expected here (drain waits, not hangs);
	# app PDBs (equal-risk, phx, portfolio) will hang the drain until the pod is
	# deleted manually or the PDB is loosened.
	local node=$1 blocked
	blocked=$(kubectl get pdb -A -o json | jq -r --arg node "$node" '
		.items[] | select(.status.disruptionsAllowed == 0) |
		"\(.metadata.namespace)/\(.metadata.name)\t\(.spec.selector.matchLabels | to_entries | map("\(.key)=\(.value)") | join(","))"' |
		while IFS=$'\t' read -r pdb selector; do
			ns=${pdb%%/*}
			pods=$(kubectl get pods -n "$ns" -l "$selector" \
				--field-selector "spec.nodeName=$node" --no-headers 2>/dev/null | awk '{print $1}')
			if [[ -n $pods ]]; then
				printf '%s blocks: %s\n' "$pdb" "$pods"
			fi
		done) || die "PDB preflight failed; refusing to drain $node"
	if [[ -n $blocked ]]; then
		warn "Zero-disruption PDBs cover pods on $node:"
		printf '%s\n' "$blocked" | sed 's/^/    /'
		if printf '%s' "$blocked" | grep -qv 'longhorn-system/instance-manager'; then
			warn "Non-Longhorn entries above will HANG the drain. Delete those pods first"
			warn "(their controllers reschedule them) or loosen the PDB, then re-run."
			read -rp "Continue with drain anyway? [y/N] " ans
			[[ $ans == y* || $ans == Y* ]] || die "aborted"
		fi
	fi
}

# --- Commands -----------------------------------------------------------------

cmd_apt() {
	local targets=("$@")
	((${#targets[@]})) || targets=("${NODES[@]}")
	local node
	for node in "${targets[@]}"; do
		require_node "$node"
		log "[$node] apt update && apt full-upgrade (serial; sudo is interactive)"
		ssh -t "${SSH_OPTS[@]}" "$node" \
			'sudo apt update && sudo apt full-upgrade -y && sudo apt autoremove -y && { [ -f /var/run/reboot-required ] && echo "*** REBOOT REQUIRED ***" || echo "no reboot required"; }'
	done
	log "apt phase done. Reboot serially with: $0 reboot <node>  (order: ${NODES[*]})"
}

cmd_status() {
	kubectl get nodes -o wide
	local cordoned
	cordoned=$(kubectl get nodes --no-headers | grep SchedulingDisabled | cut -d' ' -f1 || true)
	[[ -n $cordoned ]] && warn "cordoned node(s) — leftover from an interrupted cycle? $cordoned"
	echo
	local node remote_status state kernel
	for node in "${NODES[@]}"; do
		if remote_status=$(ssh "${SSH_OPTS[@]}" "$node" \
			'if [ -f /var/run/reboot-required ]; then state=required; else state=current; fi; printf "%s\t%s\n" "$state" "$(uname -r)"' 2>/dev/null); then
			IFS=$'\t' read -r state kernel <<<"$remote_status"
			if [[ $state == required ]]; then
				printf '%-8s reboot required (kernel: %s)\n' "$node" "$kernel"
			else
				printf '%-8s up to date (kernel: %s)\n' "$node" "$kernel"
			fi
		else
			printf '%-8s unreachable (reboot state and kernel unknown)\n' "$node"
		fi
	done
	echo
	local bad
	bad=$(longhorn_unhealthy)
	if [[ -z $bad ]]; then
		log "All attached Longhorn volumes healthy."
	else
		warn "Degraded Longhorn volumes:"
		printf '%s\n' "$bad" | sed 's/^/    /'
	fi
}

cmd_finish() {
	(($# >= 1 && $# <= 2)) || die "usage: $0 finish <node> [previous-boot-id]"
	local node=$1 previous_boot_id=${2:-} saved_boot_id
	require_node "$node"
	require_only_target_cordoned "$node"

	if [[ -z $previous_boot_id ]]; then
		previous_boot_id=$(load_cycle_boot_id "$node")
	elif [[ -s $(cycle_state_file "$node") ]]; then
		saved_boot_id=$(load_cycle_boot_id "$node")
		[[ $saved_boot_id == "$previous_boot_id" ]] || die "provided boot ID does not match the saved cycle state for $node"
	fi
	require_valid_boot_id "$previous_boot_id"

	wait_for_new_boot "$node" "$previous_boot_id"
	log "[$node] SSH is back (kernel: $(ssh "${SSH_OPTS[@]}" "$node" 'uname -r'))"

	wait_for_node_ready "$node"

	log "[$node] uncordon"
	kubectl uncordon "$node"

	wait_longhorn_healthy
	clear_cycle_state "$node"
	log "[$node] cycle complete. Safe to proceed to the next node."
}

cmd_reboot() {
	(($# >= 1 && $# <= 2)) || die "usage: $0 reboot <node> [--no-reboot]"
	local node=$1 boot_id
	local do_reboot=1
	case ${2:-} in
	"") ;;
	--no-reboot) do_reboot=0 ;;
	*) die "unknown reboot option '$2'" ;;
	esac
	require_node "$node"
	require_no_cordoned_nodes
	require_reboot_order "$node"

	log "[$node] preflight: PDB check"
	pdb_preflight "$node"

	log "[$node] preflight: no active sync Jobs in the compendium namespace"
	local active
	active=$(kubectl get jobs -n compendium -o jsonpath='{range .items[?(@.status.active>0)]}{.metadata.name}{"\n"}{end}' 2>/dev/null)
	[[ -n $active ]] && die "active compendium Job(s) running: $active — wait for sync to finish"

	log "[$node] preflight: Longhorn must be healthy before we take a node down"
	wait_longhorn_healthy
	boot_id=$(remote_boot_id "$node") || die "cannot read the current boot ID from $node"
	[[ -n $boot_id ]] || die "empty boot ID returned by $node"
	require_valid_boot_id "$boot_id"

	log "[$node] cordon"
	kubectl cordon "$node"
	save_cycle_boot_id "$node" "$boot_id"

	log "[$node] drain (timeout $DRAIN_TIMEOUT)"
	# Best-effort: on a node holding the LAST replica of any single-replica Longhorn
	# volume, the instance-manager PDB never releases (block-if-contains-last-replica),
	# so a full drain is impossible by design. If the instance-manager is the only
	# survivor, report the volumes that will blip during the reboot and proceed.
	if ! kubectl drain "$node" --ignore-daemonsets --delete-emptydir-data --timeout="$DRAIN_TIMEOUT"; then
		warn "drain incomplete — assessing what is left on $node"
		local leftovers
		# Fail closed: every non-terminal pod counts (Running, Pending, Unknown, or
		# missing phase), not just Running — only Succeeded/Failed are safely done.
		leftovers=$(kubectl get pods -A -o json --field-selector "spec.nodeName=$node" |
			jq -r '.items[] | select((.status.phase // "Unknown") as $p | $p != "Succeeded" and $p != "Failed")
				| select((.metadata.ownerReferences // []) | any(.kind == "DaemonSet") | not)
				| select((.metadata.namespace == "longhorn-system" and (.metadata.name | startswith("instance-manager"))) | not)
				| "\(.metadata.namespace)/\(.metadata.name) (\(.status.phase // "unknown"))"')
		[[ -n $leftovers ]] && die "non-Longhorn pods still on $node after drain: $leftovers"
		warn "only the Longhorn instance-manager remains, pinned by last-replica volumes."
		warn "single-replica volumes on $node — briefly unavailable during the reboot:"
		kubectl get volumes.longhorn.io,replicas.longhorn.io -n longhorn-system -o json |
			jq -r --arg n "$node" '.items as $all
				| [$all[] | select(.kind == "Replica" and .spec.nodeID == $n) | .spec.volumeName] as $onnode
				| $all[] | select(.kind == "Volume" and .spec.numberOfReplicas == 1
					and (.metadata.name as $m | $onnode | index($m)))
				| "    \(.metadata.name)  \(.status.kubernetesStatus.namespace)/\(.status.kubernetesStatus.pvcName)  \(.status.state)"'
	fi

	if ((!do_reboot)); then
		log "[$node] --no-reboot: stopping before restart. Node is cordoned and drained."
		log "[$node] next: ssh $node sudo reboot   (or: kubectl uncordon $node to back out)"
		log "[$node] then: $0 finish $node   (saved previous boot ID: $boot_id)"
		return 0
	fi

	log "[$node] reboot"
	ssh -t "${SSH_OPTS[@]}" "$node" 'sudo reboot' || warn "SSH disconnected or reboot command returned non-zero; verifying the boot ID"

	cmd_finish "$node" "$boot_id"
}

main() {
	case ${1:-} in
	apt) shift && acquire_maintenance_lock && cmd_apt "$@" ;;
	status) cmd_status ;;
	reboot) shift && acquire_maintenance_lock && cmd_reboot "$@" ;;
	finish) shift && acquire_maintenance_lock && cmd_finish "$@" ;;
	*) usage ;;
	esac
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
	main "$@"
fi
