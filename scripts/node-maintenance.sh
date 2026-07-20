#!/usr/bin/env bash
# node-maintenance.sh — apt updates and serialized reboots for the k3s cluster.
#
# Phase 1 (non-disruptive, any time, all nodes):
#   node-maintenance.sh apt [node ...]      apt update + full-upgrade + autoremove, report reboot-required
#   node-maintenance.sh status              node readiness, reboot-required, Longhorn volume health
#
# Phase 2 (disruptive, one node per invocation — agents first, timmy last):
#   node-maintenance.sh reboot <node>       preflight → cordon → drain → reboot → uncordon → health gate
#   node-maintenance.sh reboot <node> --no-reboot   stop after drain (reboot manually, then: finish <node>)
#   node-maintenance.sh finish <node>       post-reboot half: wait for Ready → uncordon → health gate
#
# Reboot order matters: wemby → manu → timmy. timmy is the only control plane,
# so its reboot takes the API down — run it last and let the wait loop tolerate
# kubectl outages. The Longhorn health gate at the end of each cycle must pass
# before starting the next node, or a later drain can strand the last replica.

set -euo pipefail

NODES=(wemby manu timmy)
DRAIN_TIMEOUT=5m # evictions settle in ~2m; the rest would only be spent retrying pinned instance-managers
SSH_OPTS=(-o ConnectTimeout=5)

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

# --- Longhorn -----------------------------------------------------------------

longhorn_unhealthy() {
	# Prints attached volumes whose robustness is not "healthy"; empty output = all clear.
	kubectl get volumes.longhorn.io -n longhorn-system -o json |
		jq -r '.items[] | select(.status.state == "attached" and .status.robustness != "healthy") |
			"\(.metadata.name)\t\(.status.robustness)"'
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
		done) || true
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
	local targets=("${@:-${NODES[@]}}")
	local node
	for node in "${targets[@]}"; do
		require_node "$node"
		log "[$node] apt update && apt full-upgrade"
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
	local node
	for node in "${NODES[@]}"; do
		if ssh "${SSH_OPTS[@]}" "$node" '[ -f /var/run/reboot-required ]' 2>/dev/null; then
			printf '%-8s reboot required (kernel: %s)\n' "$node" "$(ssh "${SSH_OPTS[@]}" "$node" 'uname -r')"
		else
			printf '%-8s up to date (kernel: %s)\n' "$node" "$(ssh "${SSH_OPTS[@]}" "$node" 'uname -r' 2>/dev/null || echo unreachable)"
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
	local node=${1:?usage: $0 finish <node>}
	require_node "$node"

	log "[$node] waiting for SSH to return..."
	until ssh "${SSH_OPTS[@]}" "$node" true 2>/dev/null; do sleep 10; done
	log "[$node] SSH is back (kernel: $(ssh "${SSH_OPTS[@]}" "$node" 'uname -r'))"

	log "[$node] waiting for k8s Ready..."
	# kubectl errors are tolerated here — rebooting timmy takes the API down.
	until [[ $(kubectl get node "$node" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null) == True ]]; do
		sleep 10
	done

	log "[$node] uncordon"
	kubectl uncordon "$node"

	wait_longhorn_healthy
	log "[$node] cycle complete. Safe to proceed to the next node."
}

cmd_reboot() {
	local node=${1:?usage: $0 reboot <node> [--no-reboot]}
	local do_reboot=1
	[[ ${2:-} == --no-reboot ]] && do_reboot=0
	require_node "$node"

	log "[$node] preflight: PDB check"
	pdb_preflight "$node"

	log "[$node] preflight: no active sync Jobs in the compendium namespace"
	local active
	active=$(kubectl get jobs -n compendium -o jsonpath='{range .items[?(@.status.active>0)]}{.metadata.name}{"\n"}{end}' 2>/dev/null)
	[[ -n $active ]] && die "active compendium Job(s) running: $active — wait for sync to finish"

	log "[$node] preflight: Longhorn must be healthy before we take a node down"
	wait_longhorn_healthy

	log "[$node] cordon"
	kubectl cordon "$node"

	log "[$node] drain (timeout $DRAIN_TIMEOUT)"
	# Best-effort: on a node holding the LAST replica of any single-replica Longhorn
	# volume, the instance-manager PDB never releases (block-if-contains-last-replica),
	# so a full drain is impossible by design. If the instance-manager is the only
	# survivor, report the volumes that will blip during the reboot and proceed.
	if ! kubectl drain "$node" --ignore-daemonsets --delete-emptydir-data --timeout="$DRAIN_TIMEOUT"; then
		warn "drain incomplete — assessing what is left on $node"
		local leftovers
		leftovers=$(kubectl get pods -A -o json --field-selector "spec.nodeName=$node" |
			jq -r '.items[] | select(.status.phase == "Running")
				| select((.metadata.ownerReferences // []) | any(.kind == "DaemonSet") | not)
				| select((.metadata.namespace == "longhorn-system" and (.metadata.name | startswith("instance-manager"))) | not)
				| "\(.metadata.namespace)/\(.metadata.name)"')
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
		log "[$node] then: $0 finish $node"
		return 0
	fi

	log "[$node] reboot"
	ssh -t "${SSH_OPTS[@]}" "$node" 'sudo reboot' || true # ssh drops when the node goes down

	log "[$node] waiting for node to leave Ready (going down)..."
	sleep 20

	cmd_finish "$node"
}

case ${1:-} in
apt) shift && cmd_apt "$@" ;;
status) cmd_status ;;
reboot) shift && cmd_reboot "$@" ;;
finish) shift && cmd_finish "$@" ;;
*) usage ;;
esac
