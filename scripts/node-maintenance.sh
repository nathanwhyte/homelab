#!/usr/bin/env bash
# node-maintenance.sh — apt updates and serialized reboots for the k3s cluster.
#
# Phase 1 (non-disruptive, any time, nodes updated serially):
#   node-maintenance.sh apt [node ...]      apt update + full-upgrade + autoremove, report reboot-required
#   node-maintenance.sh status              node readiness, reboot-required, Longhorn volume health
#
# Phase 2 (disruptive, one node per invocation — agents first, timmy last):
#   node-maintenance.sh reboot <node>       preflight → cordon → drain → reboot → uncordon → health gate
#   node-maintenance.sh reboot <node> [--no-reboot] [--spin-down] [--override-memory]
#   node-maintenance.sh finish <node> [previous-boot-id]
#                                             verify reboot → wait for Ready → uncordon → health gate
#
# Reboot order matters: wemby → manu → timmy. timmy is the only control plane,
# so its reboot takes the API down — run it last and let the wait loop tolerate
# kubectl outages. The Longhorn health gate at the end of each cycle must pass
# before starting the next node, or a later drain can strand the last replica.
#
# Memory preflight (IMPR-1089): draining a node reschedules its workloads onto
# the others; on 2026-07-20 wemby's drain OOM-froze timmy (Ollama model cache +
# OpenViking already resident). `reboot` now runs a memory-headroom preflight
# (blocking unless --override-memory) and, with --spin-down, scales the
# memory-heavy services (llama/ollama, viking/openviking, viking/ov-vectordb)
# to 0 before the drain and restores them in `finish`.

set -euo pipefail

NODES=(wemby manu timmy)
DRAIN_TIMEOUT=5m # evictions settle in ~2m; the rest would only be spent retrying pinned instance-managers
REBOOT_TIMEOUT_SECONDS=${REBOOT_TIMEOUT_SECONDS:-600}
READY_TIMEOUT_SECONDS=${READY_TIMEOUT_SECONDS:-600}
# Rebooting timmy takes the API server down with it, and a slow POST/initramfs can
# outlast the node's own boot. Waited-for separately from READY so a late API is not
# mistaken for a node that failed to come back.
API_TIMEOUT_SECONDS=${API_TIMEOUT_SECONDS:-600}
APT_TIMEOUT_SECONDS=${APT_TIMEOUT_SECONDS:-3600}
APT_LAUNCH_GRACE_SECONDS=${APT_LAUNCH_GRACE_SECONDS:-60}
APT_REMOTE_LOG=/tmp/node-maintenance-apt.log
SSH_OPTS=(-o ConnectTimeout=5)
# ConnectTimeout bounds connection setup only. A server that completes the
# handshake and then stalls stays stuck forever — Tailscale SSH in check-mode does
# exactly this, holding the session open while it waits for a browser login
# (2026-08-07). Every wait loop here is written as "deadline checked between
# calls", which a single hung call defeats, so each call gets its own hard cap.
SSH_CMD_TIMEOUT_SECONDS=${SSH_CMD_TIMEOUT_SECONDS:-120}
TIMEOUT_BIN=$(command -v timeout || command -v gtimeout || true)
STATE_ROOT=${XDG_STATE_HOME:-"$HOME/.local/state"}
MAINTENANCE_STATE_DIR=$STATE_ROOT/homelab-node-maintenance
MAINTENANCE_LOCK_DIR=$MAINTENANCE_STATE_DIR/lock
LOCK_HELD=0
# Overridable so tests can stub cluster reads/writes without a live cluster.
KUBECTL=${KUBECTL:-kubectl}

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

run_ssh() {
	# Every non-interactive SSH call goes through here so it cannot hang forever.
	local node=$1
	shift
	if [[ -n $TIMEOUT_BIN ]]; then
		"$TIMEOUT_BIN" "$SSH_CMD_TIMEOUT_SECONDS" ssh "${SSH_OPTS[@]}" "$node" "$@"
	else
		ssh "${SSH_OPTS[@]}" "$node" "$@"
	fi
}

probe_ssh() {
	# Echoes a remote command's stdout, but only when the remote actually ran it.
	#
	# The naive form — out=$(ssh node 'cmd' 2>/dev/null) || true — cannot tell
	# "command produced no output" from "SSH never connected", so a broken
	# transport silently becomes an empty, clean-looking result. Every caller here
	# uses that output to decide whether a node is safe, so it has to fail closed.
	# Proof of execution is a sentinel the remote prints after the command; no
	# sentinel means no answer, regardless of exit status.
	local node=$1 remote_cmd=$2 attempts=${3:-3}
	local out i
	for ((i = 1; i <= attempts; i++)); do
		if out=$(run_ssh "$node" "$remote_cmd; printf '\n__PROBE_OK__\n'" 2>/dev/null) &&
			[[ $out == *__PROBE_OK__ ]]; then
			out=${out%__PROBE_OK__}
			printf '%s' "${out%$'\n'}"
			return 0
		fi
		((i < attempts)) && sleep 5
	done
	return 1
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
		if run_ssh "$node" '[ -f /var/run/reboot-required ]' 2>/dev/null; then
			die "$node still requires reboot; complete nodes in order: ${NODES[*]}"
		else
			rc=$?
			((rc == 1)) || die "cannot verify reboot state on $node; refusing to skip it"
		fi
	done
}

remote_boot_id() {
	run_ssh "$1" 'cat /proc/sys/kernel/random/boot_id'
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

wait_for_api() {
	# Every check that shells out to kubectl needs the API server, which lives on
	# timmy — so the moment timmy reboots, those checks fail for reasons that have
	# nothing to do with what they are asserting. Block until the API answers again
	# instead of letting a caller interpret the outage as a real verdict.
	local deadline
	deadline=$((SECONDS + API_TIMEOUT_SECONDS))
	while ((SECONDS < deadline)); do
		kubectl get --raw=/readyz >/dev/null 2>&1 && return 0
		sleep 10
	done
	die "kubernetes API unreachable after ${API_TIMEOUT_SECONDS}s; leaving the node cordoned"
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
	# Longhorn's instance-manager PDBs are expected here (drain waits, not hangs).
	#
	# Only an app PDB that actually selects a pod can hang the drain, which is why
	# this intersects each selector with pods on the target node rather than
	# trusting disruptionsAllowed. Verified 2026-08-06: of the three app PDBs, only
	# portfolio-pdb matches anything — equal-risk-pdb selects app=rails while the
	# pod is app=equal-risk-rails, and phx-pdb is orphaned (no phx workload exists;
	# service/phx points at app=portfolio). Both report 0 allowed disruptions purely
	# because expectedPods is 0, and neither blocks a drain.
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

# --- Memory headroom preflight (IMPR-1089) -----------------------------------

# Convert a Kubernetes memory quantity ("6Gi", "512Mi", "1000") to bytes.
# Integer quantities only — this cluster's requests are whole Gi/Mi, and a
# fractional request would be mis-parsed (documented limitation, not silent).
mem_quantity_to_bytes() {
	local q=$1 num unit
	num=${q//[^0-9]/}
	unit=${q//[0-9]/}
	case $unit in
	Ki) printf '%d\n' "$((num * 1024))" ;;
	Mi) printf '%d\n' "$((num * 1024 * 1024))" ;;
	Gi) printf '%d\n' "$((num * 1024 * 1024 * 1024))" ;;
	Ti) printf '%d\n' "$((num * 1024 * 1024 * 1024 * 1024))" ;;
	'') printf '%d\n' "$num" ;;
	*) printf '0\n' ;;
	esac
}

human_bytes() {
	local b=$1
	if ((b >= 1073741824)); then
		printf '%dGi' "$((b / 1073741824))"
	elif ((b >= 1048576)); then
		printf '%dMi' "$((b / 1048576))"
	elif ((b >= 1024)); then
		printf '%dKi' "$((b / 1024))"
	else
		printf '%dB' "$b"
	fi
}

# Pure decision, no cluster access: given the target node's pod memory (bytes)
# and the remaining nodes' total headroom (bytes), print pass/warn/block.
#   pass  — rescheduled pods fit comfortably
#   warn  — they fit but consume most of the headroom (tight)
#   block — they clearly will not fit
# MEMORY_HEADROOM_WARN_PCT (default 80) sets the "tight" threshold.
memory_headroom_verdict() {
	local target_mem=$1 headroom=$2
	local warn_pct=${MEMORY_HEADROOM_WARN_PCT:-80}
	if ((target_mem > headroom)); then
		printf 'block\n'
	elif ((target_mem * 100 > headroom * warn_pct)); then
		printf 'warn\n'
	else
		printf 'pass\n'
	fi
}

# Sum the memory requests of pods scheduled on a node (bytes). Pods without a
# request contribute 0 — the scheduler treats them the same way, so this is the
# conservative scheduling signal (requests, not live usage, decide placement).
node_pod_memory_bytes() {
	local node=$1 q quantities total=0
	quantities=$($KUBECTL get pods -A -o json --field-selector "spec.nodeName=$node" |
		jq -r '.items[].spec.containers[].resources.requests.memory // "0"') || return 1
	while IFS= read -r q; do
		[[ -n $q ]] && total=$((total + $(mem_quantity_to_bytes "$q")))
	done <<<"$quantities"
	printf '%d\n' "$total"
}

# Total allocatable-minus-requested memory headroom across every node except
# the target (bytes). A node with no allocatable/requested data contributes 0.
remaining_headroom_bytes() {
	local node=$1
	local nodes pods
	nodes=$($KUBECTL get nodes -o json) || return 1
	pods=$($KUBECTL get pods -A -o json) || return 1
	jq -n --arg node "$node" --argjson nodes "$nodes" --argjson pods "$pods" '
		def tobytes:
			if . == null or . == "" then 0
			else
				(capture("^(?<n>[0-9]+)(?<u>[KMGTP]i)?$") as $m
				 | ($m.n | tonumber) as $n
				 | if $m.u == "Ki" then $n*1024
				   elif $m.u == "Mi" then $n*1024*1024
				   elif $m.u == "Gi" then $n*1024*1024*1024
				   elif $m.u == "Ti" then $n*1024*1024*1024*1024
				   else $n end)
			end;
		($pods.items
		 | map(select((.spec.nodeName // "") != ""))
		 | group_by(.spec.nodeName)
		 | map({key: .[0].spec.nodeName,
		        value: ([.[].spec.containers[].resources.requests.memory // "0" | tobytes] | add)})
		 | from_entries) as $req
		| [$nodes.items[]
		   | select(.metadata.name != $node)
		   | ((.status.allocatable.memory | tobytes) - ($req[.metadata.name] // 0))]
		| add
	'
}

# Orchestrator: gather the target node's pod memory and the remaining nodes'
# headroom, then pass/warn/block. On block, abort unless override is set.
memory_headroom_preflight() {
	local node=$1 override=${2:-0}
	local target_mem headroom verdict
	target_mem=$(node_pod_memory_bytes "$node") || die "cannot read pod memory on $node"
	headroom=$(remaining_headroom_bytes "$node") || die "cannot read remaining-node headroom"
	verdict=$(memory_headroom_verdict "$target_mem" "$headroom")
	log "[$node] memory preflight: target pods request $(human_bytes "$target_mem"), remaining headroom $(human_bytes "$headroom")"
	case $verdict in
	pass) log "[$node] memory headroom OK." ;;
	warn) warn "[$node] memory headroom is TIGHT — rescheduled pods will consume most of the remaining capacity. Consider --spin-down." ;;
	block)
		if ((override)); then
			warn "[$node] memory headroom INSUFFICIENT but --override-memory given; proceeding."
		else
			die "[$node] memory preflight BLOCKED: target pods request $(human_bytes "$target_mem") but remaining nodes have only $(human_bytes "$headroom") headroom. Re-run with --spin-down and/or --override-memory."
		fi
		;;
	esac
}

# --- Pre-drain spin-down / restore (IMPR-1089) --------------------------------

# Memory-heavy services that can sit out a maintenance window. Each entry is
# "namespace/deployment". Scaled to 0 before the drain, restored after.
MEMORY_HEAVY_SERVICES=(
	llama/ollama
	viking/openviking
	viking/ov-vectordb
)

spin_down_state_file() {
	printf '%s/spin-down-replicas\n' "$MAINTENANCE_STATE_DIR"
}

# Record each service's current replica count, then scale it to 0. The recorded
# counts are what restore_memory_services reads back, so a prior replica count
# is always restored without manual intervention.
spin_down_memory_services() {
	local svc ns name replicas saved_svc saved_replicas state
	mkdir -p "$MAINTENANCE_STATE_DIR"
	state=$(spin_down_state_file)
	touch "$state" || return 1
	for svc in "${MEMORY_HEAVY_SERVICES[@]}"; do
		ns=${svc%%/*}
		name=${svc#*/}
		# A retry must retain the count saved before the first scale attempt.
		replicas=
		while IFS=$'\t' read -r saved_svc saved_replicas; do
			if [[ $saved_svc == "$svc" ]]; then
				replicas=$saved_replicas
				break
			fi
		done <"$state"
		if [[ -z $replicas ]]; then
			replicas=$($KUBECTL get deployment "$name" -n "$ns" -o jsonpath='{.spec.replicas}') || return 1
			[[ $replicas =~ ^[0-9]+$ ]] || die "invalid replica count for $svc"
			printf '%s\t%s\n' "$svc" "$replicas" >>"$state" || return 1
		fi
		[[ $replicas =~ ^[0-9]+$ ]] || die "invalid saved replica count for $svc"
		if ((replicas > 0)); then
			log "spin-down: scaling $svc to 0 (was $replicas)"
			$KUBECTL scale deployment "$name" -n "$ns" --replicas=0 || return 1
		fi
	done
}

# Restore each service to the replica count recorded before the drain. No-op
# when no spin-down happened (state file absent).
restore_memory_services() {
	local svc ns name replicas
	[[ -s $(spin_down_state_file) ]] || return 0
	while IFS=$'\t' read -r svc replicas; do
		ns=${svc%%/*}
		name=${svc#*/}
		if ((replicas > 0)); then
			log "restore: scaling $svc back to $replicas"
			$KUBECTL scale deployment "$name" -n "$ns" --replicas="$replicas" || return 1
		fi
	done <"$(spin_down_state_file)"
	rm -f "$(spin_down_state_file)"
}

# --- Commands -----------------------------------------------------------------

launch_remote_apt() {
	# The upgrade is detached from this SSH session on purpose. Some packages
	# restart the very transport the command arrived over — upgrading tailscale
	# bounces tailscaled, which kills the connection — and if the apt chain is
	# still in that session's process group, the resulting SIGHUP lands mid-dpkg
	# and leaves packages half-configured (wemby/grafana, 2026-08-06). setsid puts
	# it in its own session so the work survives losing the link.
	#
	# The remote chain announces itself with APT_STARTED before doing any work.
	# That marker is the only reliable proof the child came up: the launch is
	# backgrounded ahead of `sleep`, so SSH reports `sleep`'s status and would
	# return 0 even if setsid, nohup, bash, or the redirection had failed.
	local node=$1 deadline
	run_ssh "$node" \
		"rm -f $APT_REMOTE_LOG; setsid nohup bash -c 'echo APT_STARTED; sudo -n apt update && sudo -n apt full-upgrade -y && sudo -n apt autoremove -y; echo APT_DONE_RC=\$?' >$APT_REMOTE_LOG 2>&1 </dev/null & sleep 1" ||
		die "[$node] could not start the apt run"

	deadline=$((SECONDS + APT_LAUNCH_GRACE_SECONDS))
	while ((SECONDS < deadline)); do
		if probe_ssh "$node" "grep -c APT_STARTED $APT_REMOTE_LOG 2>/dev/null || true" | grep -qx '[1-9][0-9]*'; then
			return 0
		fi
		sleep 5
	done
	warn "[$node] launch diagnostics:"
	probe_ssh "$node" "tail -20 $APT_REMOTE_LOG 2>&1 || true" | sed 's/^/    /' || true
	die "[$node] apt never started within ${APT_LAUNCH_GRACE_SECONDS}s (no APT_STARTED marker)"
}

wait_remote_apt() {
	# Echoes the apt chain's exit code. SSH failures while polling are expected
	# (that is the transport restarting), so reconnect rather than give up.
	local node=$1 deadline rc
	deadline=$((SECONDS + APT_TIMEOUT_SECONDS))
	while ((SECONDS < deadline)); do
		# Single attempt per poll: a failure here is usually the transport
		# restarting under us, and the surrounding loop is already the retry.
		rc=$(probe_ssh "$node" "sed -n 's/^APT_DONE_RC=//p' $APT_REMOTE_LOG" 1) || {
			sleep 10
			continue
		}
		if [[ -n $rc ]]; then
			printf '%s\n' "$rc"
			return 0
		fi
		sleep 10
	done
	die "[$node] apt did not finish within ${APT_TIMEOUT_SECONDS}s; inspect $node:$APT_REMOTE_LOG"
}

report_apt_result() {
	# A half-configured package does not fail the apt chain's exit code but does
	# break every later apt run on that node, so surface it explicitly instead of
	# letting the next cycle discover it. Repair needs unrestricted sudo, which is
	# deliberately outside this script's NOPASSWD allow-list.
	#
	# Both probes below decide whether a node is safe to move on from, so neither
	# may silently degrade to a reassuring answer when the transport is broken.
	# probe_ssh proves execution with a sentinel, and the reboot check reports its
	# verdict as a word rather than an exit status — `[ -f … ]` returning non-zero
	# is indistinguishable from SSH failing, and both used to print "no reboot
	# required".
	local node=$1 broken reboot_state
	broken=$(probe_ssh "$node" 'dpkg --audit 2>/dev/null || true') ||
		die "[$node] cannot read dpkg state (SSH failed); refusing to report this node as clean"
	if [[ -n $broken ]]; then
		warn "[$node] dpkg reports packages that are not fully configured:"
		printf '%s\n' "$broken" | sed 's/^/    /'
		warn "[$node] repair before the next cycle: ssh $node sudo dpkg --configure -a"
	fi

	reboot_state=$(probe_ssh "$node" \
		'if [ -f /var/run/reboot-required ]; then echo required; else echo current; fi') ||
		die "[$node] cannot read reboot-required state (SSH failed); refusing to guess"
	case $reboot_state in
	required) log "[$node] *** REBOOT REQUIRED ***" ;;
	current) log "[$node] no reboot required" ;;
	*) die "[$node] unexpected reboot-required probe result: '$reboot_state'" ;;
	esac
}

cmd_apt() {
	local targets=("$@")
	((${#targets[@]})) || targets=("${NODES[@]}")
	local node rc
	for node in "${targets[@]}"; do
		require_node "$node"
		log "[$node] apt update + full-upgrade + autoremove (detached; nodes still serial)"
		launch_remote_apt "$node"
		rc=$(wait_remote_apt "$node")
		if [[ $rc != 0 ]]; then
			run_ssh "$node" "tail -25 $APT_REMOTE_LOG" 2>/dev/null | sed 's/^/    /' || true
			die "[$node] apt exited $rc; full log at $node:$APT_REMOTE_LOG"
		fi
		report_apt_result "$node"
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
		if remote_status=$(run_ssh "$node" \
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

	if [[ -z $previous_boot_id ]]; then
		previous_boot_id=$(load_cycle_boot_id "$node")
	elif [[ -s $(cycle_state_file "$node") ]]; then
		saved_boot_id=$(load_cycle_boot_id "$node")
		[[ $saved_boot_id == "$previous_boot_id" ]] || die "provided boot ID does not match the saved cycle state for $node"
	fi
	require_valid_boot_id "$previous_boot_id"

	wait_for_new_boot "$node" "$previous_boot_id"
	log "[$node] SSH is back (kernel: $(run_ssh "$node" 'uname -r'))"

	# Ordered deliberately: boot proof comes from SSH and works while the API is
	# down, so it runs first and gives a real diagnosis when a node fails to come
	# back. Only then wait for the API, because the cordon check below is a kubectl
	# call — running it during timmy's own reboot aborted the cycle on 2026-08-06
	# and left the node cordoned. The check still precedes every mutation.
	wait_for_api
	require_only_target_cordoned "$node"

	wait_for_node_ready "$node"

	# Restore desired replicas while still cordoned: a failed scale keeps finish
	# retryable. Pods pinned to this node can schedule once it is uncordoned.
	restore_memory_services || return 1

	log "[$node] uncordon"
	kubectl uncordon "$node"

	wait_longhorn_healthy
	clear_cycle_state "$node"
	log "[$node] cycle complete. Safe to proceed to the next node."
}

cmd_reboot() {
	(($# >= 1 && $# <= 4)) || die "usage: $0 reboot <node> [--no-reboot] [--spin-down] [--override-memory]"
	local node=$1 boot_id
	local do_reboot=1 spin_down=0 override_memory=0
	shift
	while (($#)); do
		case $1 in
		--no-reboot) do_reboot=0 ;;
		--spin-down) spin_down=1 ;;
		--override-memory) override_memory=1 ;;
		*) die "unknown reboot option '$1'" ;;
		esac
		shift
	done
	require_node "$node"
	require_no_cordoned_nodes
	require_reboot_order "$node"

	log "[$node] preflight: PDB check"
	pdb_preflight "$node"

	log "[$node] preflight: memory headroom"
	memory_headroom_preflight "$node" "$override_memory"

	log "[$node] preflight: no active sync Jobs in the compendium namespace"
	local active
	active=$(kubectl get jobs -n compendium -o jsonpath='{range .items[?(@.status.active>0)]}{.metadata.name}{"\n"}{end}' 2>/dev/null)
	[[ -n $active ]] && die "active compendium Job(s) running: $active — wait for sync to finish"

	log "[$node] preflight: Longhorn must be healthy before we take a node down"
	wait_longhorn_healthy
	boot_id=$(remote_boot_id "$node") || die "cannot read the current boot ID from $node"
	[[ -n $boot_id ]] || die "empty boot ID returned by $node"
	require_valid_boot_id "$boot_id"

	if ((spin_down)); then
		log "[$node] pre-drain spin-down of memory-heavy services"
		spin_down_memory_services || return 1
	fi

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

	if ((! do_reboot)); then
		log "[$node] --no-reboot: stopping before restart. Node is cordoned and drained."
		log "[$node] next: ssh $node sudo reboot   (or: kubectl uncordon $node to back out)"
		log "[$node] then: $0 finish $node   (saved previous boot ID: $boot_id)"
		return 0
	fi

	log "[$node] reboot"
	# -t so sudo gets a TTY; bounded because a stalled SSH here would hang the whole
	# cycle with the node already cordoned and drained. A non-zero result is
	# expected and harmless — the reboot kills the connection — so the boot-ID
	# proof below, not this exit status, is what decides whether it worked.
	if [[ -n $TIMEOUT_BIN ]]; then
		"$TIMEOUT_BIN" "$SSH_CMD_TIMEOUT_SECONDS" ssh -t "${SSH_OPTS[@]}" "$node" 'sudo reboot' ||
			warn "SSH disconnected or reboot command returned non-zero; verifying the boot ID"
	else
		ssh -t "${SSH_OPTS[@]}" "$node" 'sudo reboot' ||
			warn "SSH disconnected or reboot command returned non-zero; verifying the boot ID"
	fi

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
