#!/usr/bin/env bash
# Pi-hole failover drill — current (three-resolver, client-side failover) topology.
# Prerequisite for PROJ-1018 Phase 5. Successor to BUG-1059's 2026-07-21 drill,
# whose result does not transfer: that one measured failover *through the router*
# with two resolvers; DHCP now hands clients all three Pi-hole IPs directly.
#
# READ-ONLY unless --stop <host> is passed. Default run is baseline only.
#
# Usage:
#   ./pihole-failover-drill.sh                 # baseline + resolver fingerprinting, no mutation
#   ./pihole-failover-drill.sh --stop timmy    # full drill: baseline, stop, measure, restore
#
# The stop phase ALWAYS restores in a trap, so the container comes back even if
# the measurement throws or the script is interrupted.

set -uo pipefail

readonly WEMBY=192.168.1.9
readonly MANU=192.168.1.10
readonly TIMMY=192.168.1.19
readonly RESOLVERS=("$WEMBY" "$MANU" "$TIMMY")

# Container-IP fingerprints: `pi.hole` answers with each host's own Docker bridge
# address, so it is the only way to tell which resolver actually served a query
# when all three answer identically for real names. Verified 2026-08-24.
#
# Written as case lookups rather than associative arrays: macOS ships bash 3.2,
# which has no `declare -A`, and this drill has to run from pop as well as from
# the Linux nodes.
fingerprint() {
	case "$1" in
	172.18.0.3) echo "wemby ($WEMBY)" ;;
	# manu moved 172.19.0.2 -> .0.3 when compose recreated its network during the
	# PROJ-1018 Phase 0 rollout (2026-08-24). Both are accepted: the address is
	# assigned by Docker at network-create time and will shift again on any
	# recreate. If a fingerprint comes back UNKNOWN, re-read it with
	# `dig +short @<ip> pi.hole` rather than assuming the host is down.
	172.19.0.2 | 172.19.0.3) echo "manu ($MANU)" ;;
	172.20.0.3) echo "timmy ($TIMMY)" ;;
	*) echo "" ;;
	esac
}

ssh_host() {
	case "$1" in
	wemby | manu | timmy) echo "$1" ;;
	*) echo "" ;;
	esac
}

readonly SAMPLES=15
readonly INTERNAL_NAMES=(registry.nathanwhyte.dev k8s.nathanwhyte.dev longhorn.nathanwhyte.dev)

STOPPED_HOST=""

log() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
row() { printf '%-34s %s\n' "$1" "$2"; }

# --- restore trap -----------------------------------------------------------
restore() {
	local rc=$?
	if [[ -n "$STOPPED_HOST" ]]; then
		log "RESTORING pihole on $STOPPED_HOST"
		if ssh "$(ssh_host "$STOPPED_HOST")" 'docker start pihole'; then
			echo "restarted"
			sleep 5
			verify_all_up
		else
			# A failed restore must NOT exit 0. The whole point of the trap is that
			# a resolver never stays down silently; converting this to a friendly
			# message made the failure invisible to any caller or wrapper.
			echo "!! RESTORE FAILED — resolver is STILL DOWN on $STOPPED_HOST"
			echo "!! Recover with: ssh $(ssh_host "$STOPPED_HOST") docker start pihole"
			exit 90
		fi
	fi
	exit $rc
}
trap restore EXIT INT TERM

# --- helpers ----------------------------------------------------------------

# A random label defeats every cache in the path. BUG-1059's lesson: a method
# that cannot produce a different answer is not evidence. Cached hits would
# "pass" the drill no matter what the resolvers were doing.
rand_name() {
	printf 'drill-%s-%s.nathanwhyte.dev' "$RANDOM" "$RANDOM"
}

# Which resolver is this client actually using right now?
# Uses the pi.hole container-IP fingerprint above.
whoami_resolver() {
	local answer match
	answer=$(dig +short +time=2 +tries=1 pi.hole 2>/dev/null | head -1)
	match=$(fingerprint "$answer")
	if [[ -n "$match" ]]; then
		echo "$match"
	elif [[ -z "$answer" ]]; then
		echo "NO ANSWER (resolver in use does not know pi.hole — see IPv6 router note)"
	else
		echo "UNKNOWN container $answer"
	fi
}

verify_all_up() {
	log "Resolver health"
	for ip in "${RESOLVERS[@]}"; do
		row "$ip pi.hole" "$(dig +short +time=2 +tries=1 @"$ip" pi.hole 2>&1 | head -1)"
	done
}

# INFO-1124's answer-identically invariant. A two-of-three rollout yields
# intermittent failures because clients pick a resolver arbitrarily.
check_invariant() {
	log "Answer-identically invariant (INFO-1124)"
	local failed=0
	for name in "${INTERNAL_NAMES[@]}"; do
		local answers=()
		for ip in "${RESOLVERS[@]}"; do
			answers+=("$(dig +short +time=2 +tries=1 @"$ip" "$name" 2>/dev/null | head -1)")
		done
		# An empty answer is NOT agreement. Three resolvers all failing to answer
		# compare equal as "" and would otherwise pass this gate — which is the
		# exact condition that must block the drill, not clear it.
		if [[ -z "${answers[0]}" || -z "${answers[1]}" || -z "${answers[2]}" ]]; then
			row "$name" "NO ANSWER from at least one resolver: [${answers[*]}]"
			failed=1
		elif [[ "${answers[0]}" == "${answers[1]}" && "${answers[1]}" == "${answers[2]}" ]]; then
			row "$name" "OK  all three -> ${answers[0]}"
		else
			row "$name" "MISMATCH  ${answers[*]}"
			failed=1
		fi
	done
	return $failed
}

# Measure default-path resolution (no @server) — this is what a real client does,
# and the only way to observe client-side failover behaviour.
measure() {
	local phase="$1"
	local answered=0 timeouts=0
	local -a latencies=()

	for ((i = 0; i < SAMPLES; i++)); do
		local name start end ms out
		name=$(rand_name)
		start=$(python3 -c 'import time;print(int(time.monotonic()*1000))')
		out=$(dig +time=5 +tries=1 "$name" 2>/dev/null)
		end=$(python3 -c 'import time;print(int(time.monotonic()*1000))')
		ms=$((end - start))
		if grep -q "status: NOERROR\|status: NXDOMAIN" <<<"$out"; then
			answered=$((answered + 1))
			latencies+=("$ms")
		else
			timeouts=$((timeouts + 1))
		fi
	done

	local stats="n/a"
	if ((${#latencies[@]} > 0)); then
		stats=$(printf '%s\n' "${latencies[@]}" | python3 -c '
import sys, statistics
v = sorted(int(x) for x in sys.stdin)
print(f"median {statistics.median(v):.0f} ms | mean {statistics.mean(v):.0f} ms | max {max(v)} ms")
')
	fi

	log "PHASE: $phase"
	row "Serving resolver" "$(whoami_resolver)"
	row "Queries answered" "$answered/$SAMPLES"
	row "Timeouts" "$timeouts"
	row "Latency" "$stats"

	# Partial-failure check: does the client still resolve INTERNAL names, or only
	# public ones? The unfiltered IPv6 router resolver in pop's list can answer
	# public names while every internal name dies — a mode BUG-1059 never saw.
	for name in "${INTERNAL_NAMES[@]}"; do
		row "  $name" "$(dig +short +time=3 +tries=1 "$name" 2>&1 | head -1 || echo 'FAILED')"
	done
	row "  doubleclick.net (filtering?)" "$(dig +short +time=3 +tries=1 doubleclick.net 2>&1 | head -1)"
}

# --- main -------------------------------------------------------------------

log "Client resolver configuration"
case "$(uname -s)" in
Darwin) scutil --dns | grep -E "^resolver|nameserver\[" | head -20 ;;
Linux) (resolvectl status 2>/dev/null || cat /etc/resolv.conf) | head -20 ;;
esac

verify_all_up

# A failed preflight must BLOCK the stop phase, not warn about it. Previously
# this was `check_invariant || echo ...`, which let a drill proceed to take a
# live resolver down on top of an already-broken resolver set.
PREFLIGHT_OK=1
check_invariant || PREFLIGHT_OK=0

measure "BASELINE (all three resolvers up)"

if [[ "${1:-}" == "--stop" ]]; then
	target="${2:?--stop requires a host: wemby|manu|timmy}"
	[[ -n "$(ssh_host "$target")" ]] || {
		echo "unknown host: $target"
		exit 1
	}

	if ((PREFLIGHT_OK == 0)); then
		echo "!! ABORT: the answer-identically preflight FAILED."
		echo "!! Taking a resolver down now would compound an existing fault."
		echo "!! Fix the resolver set first, then re-run."
		exit 2
	fi

	log "STOPPING pihole on $target"
	read -rp "This takes a live resolver down. Type the hostname to confirm: " confirm
	[[ "$confirm" == "$target" ]] || {
		echo "aborted"
		exit 1
	}

	# Set STOPPED_HOST *before* issuing the stop. If the SSH connection drops
	# after the container stops but before ssh returns, the old ordering left
	# STOPPED_HOST unset and the trap silently skipped restoration, stranding a
	# resolver down. Arming first means the trap always has a recovery target;
	# a spurious restore of a still-running container is harmless.
	STOPPED_HOST="$target"
	ssh "$(ssh_host "$target")" 'docker stop pihole' || {
		echo "stop command failed or connection dropped — trap will attempt restore"
		exit 1
	}
	sleep 3

	measure "$target STOPPED"

	# Whole-host failure is the untested case BUG-1059 explicitly scoped out: a
	# stopped container closes port 53 and the kernel sends an immediate ICMP
	# rejection, so the client learns instantly. A crashed or unplugged host
	# black-holes the packet silently, which is the slower and more realistic
	# failure. Simulating it needs a DROP rule and sudo on the target:
	#   ssh $target 'sudo iptables -I INPUT -p udp --dport 53 -j DROP'
	# Left out of the automated path deliberately — run it as a second pass once
	# the container-stop numbers are in hand.
fi

log "Drill complete"
