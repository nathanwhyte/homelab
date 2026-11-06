#!/usr/bin/env bash
# test-maintenance-quiet-output.sh — hermetic tests that the maintenance scripts'
# retry loops print each line at most twice: node-maintenance.sh's drain filter
# (a PDB-pinned instance-manager retried every 5s until DRAIN_TIMEOUT), its
# Longhorn health gate's degraded list, and timmy-offline.sh's wait for
# cross-node volumes to detach. No cluster needed: kubectl is a stub and sleep a
# no-op. Run: bash tests/test-maintenance-quiet-output.sh

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
TMPDIR_TEST=$(mktemp -d)
trap 'rm -rf "$TMPDIR_TEST"' EXIT

# A 5m drain of timmy as kubectl prints it: garage-1 is refused twice, then
# evicted; the instance-manager is refused every 5s until the global timeout.
IM=instance-manager-0f3c9a2b7d1e4f56a8b9c0d1e2f3a4b5
cat >"$TMPDIR_TEST/kubectl" <<STUB
#!/usr/bin/env bash
# Every call is logged. drain prints the transcript and exits 1 (timeout);
# STUB_DRAIN=samename prints two same-named pods in different namespaces
# instead. A pod read (get pod -n NS NAME) prints \$STUB_PODS_DIR/NS_NAME, prints
# nothing if it is missing (the pod is gone), and fails if NS_NAME.fail exists.
# Volume reads replay \$STUB_VOLUMES_DIR/<n>.json in order (a missing file
# fails the read), the last one repeating.
echo "\$*" >>"\$STUB_LOG"
case "\$1" in
drain)
	if [[ \${STUB_DRAIN:-} == samename ]]; then
		for ns in alpha beta; do
			echo "evicting pod \$ns/same-0"
			echo "error when evicting pods/\"same-0\" -n \"\$ns\" (will retry after 5s): Cannot evict pod as it would violate the pod's disruption budget." >&2
		done
		echo 'pod/same-0 evicted'
		echo 'error: unable to drain node "timmy" due to error: global timeout reached: 5m0s, continuing command...' >&2
		exit 1
	fi
	if [[ \${STUB_DRAIN:-} == other ]]; then
		for i in 1 2 3 4 5; do
			echo 'evicting pod doomed/web-0'
			echo 'error when evicting pod "web-0" from terminating namespace "doomed" (will retry after 5s): pods "web-0" is forbidden: unable to create new content in namespace doomed because it is being terminated' >&2
			echo 'W1106 07:00:00.000000 12345 warnings.go:70] some future kubectl notice' >&2
		done
		exit 0
	fi
	echo 'node/timmy cordoned'
	echo 'Warning: ignoring DaemonSet-managed Pods: kube-system/svclb-traefik-x7k2p, longhorn-system/longhorn-manager-9qz4r' >&2
	echo 'evicting pod garage/garage-1'
	for i in \$(seq 1 60); do
		echo 'evicting pod longhorn-system/$IM'
		if ((i <= 2)); then
			echo 'error when evicting pods/"garage-1" -n "garage" (will retry after 5s): Cannot evict pod as it would violate the pod'"'"'s disruption budget.' >&2
			echo 'evicting pod garage/garage-1'
		fi
		((i == 3)) && echo 'pod/garage-1 evicted'
		echo 'error when evicting pods/"$IM" -n "longhorn-system" (will retry after 5s): Cannot evict pod as it would violate the pod'"'"'s disruption budget.' >&2
	done
	# kubectl's timeout trailer (k8s.io/kubectl pkg/cmd/drain: the pending pods,
	# the per-node error, then the pending nodes).
	echo 'There are pending pods in node "timmy" when an error occurred: error when evicting pods/"$IM" -n "longhorn-system": global timeout reached: 5m0s' >&2
	echo 'pod/$IM' >&2
	echo 'error: unable to drain node "timmy" due to error: error when evicting pods/"$IM" -n "longhorn-system": global timeout reached: 5m0s, continuing command...' >&2
	echo 'There are pending nodes to be drained:' >&2
	echo ' timmy' >&2
	exit 1
	;;
get)
	if [[ \$2 == pod ]]; then
		f=\$STUB_PODS_DIR/\$4_\$5
		[[ -e \$f.fail ]] && exit 1
		[[ -e \$f ]] && cat "\$f"
		exit 0
	fi
	n=\$(cat "\$STUB_VOLUMES_DIR/next" 2>/dev/null || echo 1)
	f=\$STUB_VOLUMES_DIR/\$n.json
	[[ -e \$STUB_VOLUMES_DIR/\$((n + 1)).json || -e \$STUB_VOLUMES_DIR/\$((n + 1)).fail ]] && echo \$((n + 1)) >"\$STUB_VOLUMES_DIR/next"
	[[ -e \$f ]] || { echo 'The connection to the server 10.0.0.1:6443 was refused - did you specify the right host or port?' >&2; exit 1; }
	cat "\$f"
	;;
esac
STUB
chmod +x "$TMPDIR_TEST/kubectl"
export PATH=$TMPDIR_TEST:$PATH KUBECTL=$TMPDIR_TEST/kubectl
export STUB_LOG=$TMPDIR_TEST/calls.log STUB_VOLUMES_DIR=$TMPDIR_TEST/volumes STUB_PODS_DIR=$TMPDIR_TEST/pods
export XDG_STATE_HOME=$TMPDIR_TEST/state # the health gate's stderr capture lives there
mkdir -p "$STUB_PODS_DIR"

PASS=0
FAIL=0
ok() {
	printf 'ok   %s\n' "$1"
	PASS=$((PASS + 1))
}
bad() {
	printf 'FAIL %s\n' "$1"
	FAIL=$((FAIL + 1))
}
assert_eq() { # desc expected actual
	if [[ $2 == "$3" ]]; then ok "$1"; else bad "$1 (expected '$2', got '$3')"; fi
}
assert_has() { # desc needle haystack
	if [[ $3 == *"$2"* ]]; then ok "$1"; else bad "$1 (no '$2' in output)"; fi
}
max_repeat() { # the most times any one line occurs in $1
	sort <<<"$1" | uniq -c | sort -rn | awk 'NR == 1 { print $1 }'
}

# Volume reads, in order: each argument is a JSON items list, or "fail".
volumes() {
	rm -rf "$STUB_VOLUMES_DIR"
	mkdir -p "$STUB_VOLUMES_DIR"
	local i=0 v
	for v in "$@"; do
		i=$((i + 1))
		if [[ $v == fail ]]; then : >"$STUB_VOLUMES_DIR/$i.fail"; else printf '{"items":%s}\n' "$v" >"$STUB_VOLUMES_DIR/$i.json"; fi
	done
	: >"$STUB_LOG"
}
lh_volume() { # name state robustness [node]
	printf '{"kind":"Volume","metadata":{"name":"%s"},"spec":{"numberOfReplicas":1},"status":{"state":"%s","robustness":"%s","currentNodeID":"%s","kubernetesStatus":{"namespace":"garage","pvcName":"%s"}}}' \
		"$1" "$2" "$3" "${4:-manu}" "data-$1"
}
lh_replica() { # volume node
	printf '{"kind":"Replica","metadata":{"name":"r-%s"},"spec":{"volumeName":"%s","nodeID":"%s"}}' "$1" "$1" "$2"
}
strip() { sed $'s/\033\\[[0-9;]*m//g'; }

# --- node-maintenance.sh --------------------------------------------------------
(
	# shellcheck source=../scripts/node-maintenance.sh
	source "$REPO_ROOT/scripts/node-maintenance.sh"
	set +e
	sleep() { :; }

	# Pod reads print <node>|<created>|<deleting>. After the drain the
	# instance-manager is still on timmy, created long before; garage-1 is gone.
	old=2026-10-01T00:00:00Z
	echo "timmy|$old|" >"$STUB_PODS_DIR/longhorn-system_$IM"
	raw=$("$KUBECTL" drain timmy 2>&1)
	out=$("$KUBECTL" drain timmy 2>&1 | drain_filter timmy)
	rc=$?
	echo "rc=$rc"
	echo "raw_max=$(max_repeat "$raw")"
	echo "max=$(max_repeat "$out")"
	echo "lines=$(wc -l <<<"$out" | tr -d ' ')"
	printf 'OUT\n%s\nEND\n' "$out"

	# Same-named pods: kubectl's `pod/same-0 evicted` names no namespace. Only
	# alpha's went (and came back on manu); beta's is still on timmy.
	echo "manu|$old|" >"$STUB_PODS_DIR/alpha_same-0"
	echo "timmy|$old|" >"$STUB_PODS_DIR/beta_same-0"
	printf 'SAMENAME\n%s\nEND\n' "$(STUB_DRAIN=samename "$KUBECTL" drain timmy 2>&1 | drain_filter timmy)"
	# Accepted but still terminating, and a pod that cannot be read.
	echo "timmy|$old|2026-11-06T07:00:00Z" >"$STUB_PODS_DIR/alpha_same-0"
	: >"$STUB_PODS_DIR/beta_same-0.fail"
	printf 'EDGES\n%s\nEND\n' "$(STUB_DRAIN=samename "$KUBECTL" drain timmy 2>&1 | drain_filter timmy)"
	# Evicted, then recreated on timmy with spec.nodeName set (created after the
	# drain began, which bypasses the cordon).
	echo "timmy|2099-01-01T00:00:00Z|" >"$STUB_PODS_DIR/alpha_same-0"
	rm -f "$STUB_PODS_DIR/beta_same-0.fail"
	printf 'RECREATED\n%s\nEND\n' "$(STUB_DRAIN=samename "$KUBECTL" drain timmy 2>&1 | drain_filter timmy)"
	# The terminating-namespace retry format, and a line no rule knows, each 5
	# times, from a drain that succeeds.
	other=$(STUB_DRAIN=other "$KUBECTL" drain timmy 2>&1 | drain_filter timmy)
	echo "other_rc=$?"
	echo "other_max=$(max_repeat "$other")"
	printf 'OTHER\n%s\nEND\n' "$other"

	# A list that shrinks as volumes heal ([A,B,C] → [A,B'] → [A]), with a kubectl
	# outage between, and B changing state on the way.
	abc="[$(lh_volume garage-0 attached degraded),$(lh_volume garage-2 attaching unknown),$(lh_volume garage-1 attached degraded)]"
	ab="[$(lh_volume garage-0 attached degraded),$(lh_volume garage-2 attached degraded)]"
	a="[$(lh_volume garage-0 attached degraded)]"
	volumes "$abc" "$abc" fail fail "$ab" "$ab" "$a" "$a" "[$(lh_volume garage-0 attached healthy)]"
	gate=$(wait_longhorn_healthy 2>&1 | strip)
	echo "gate_rc=$?"
	echo "gate_polls=$(grep -c volumes.longhorn.io "$STUB_LOG")"
	echo "gate_max=$(max_repeat "$gate")"
	printf 'GATE\n%s\nEND\n' "$gate"

	# Three separate kubectl outages, each followed by a good read.
	volumes "$a" fail "$a" fail fail "$a" fail "[$(lh_volume garage-0 attached healthy)]"
	flaky=$(wait_longhorn_healthy 2>&1 | strip)
	echo "flaky_max=$(max_repeat "$flaky")"
	printf 'FLAKY\n%s\nEND\n' "$flaky"
) >"$TMPDIR_TEST/nm.out" 2>&1
nm=$(<"$TMPDIR_TEST/nm.out")
field() { sed -n "s/^$1=//p" <<<"$nm"; }
block() { sed -n "/^$1\$/,/^END\$/p" <<<"$nm" | sed '1d;$d'; }

assert_eq "a filtered drain keeps kubectl's failure under pipefail" 1 "$(field rc)"
raw_max=$(field raw_max)
if ((raw_max > 50)); then ok "the unfiltered transcript repeats a line $raw_max times"; else bad "the stub transcript is not noisy (max $raw_max)"; fi
max=$(field max)
if ((max <= 2)); then ok "the filtered drain prints no line more than twice (max $max)"; else bad "the filtered drain repeats a line $max times"; fi
drain=$(block OUT)
assert_eq "evicting the instance-manager is printed once" 1 "$(grep -c "^evicting pod longhorn-system/$IM\$" <<<"$drain")"
assert_eq "its first retry line is printed once" 1 "$(grep -c "^error when evicting pods/\"$IM\" -n \"longhorn-system\" (will retry" <<<"$drain")"
PDB="(Cannot evict pod as it would violate the pod's disruption budget.)"
assert_has "the instance-manager ends as one counted line" \
	"    longhorn-system/$IM: eviction refused 60 times $PDB; still on timmy when the drain ended" "$drain"
assert_has "a pod evicted after retries says so" \
	"    garage/garage-1: eviction refused 2 times (Cannot evict pod as it would violate the pod's disruption budget.); evicted in the end" "$drain"
assert_has "kubectl's own eviction line passes through" "pod/garage-1 evicted" "$drain"
assert_has "kubectl's timeout error passes through" 'error: unable to drain node "timmy"' "$drain"
assert_has "the DaemonSet warning passes through" "Warning: ignoring DaemonSet-managed Pods" "$drain"
last=$(tail -1 <<<"$drain")
assert_eq "the counted lines come last, after kubectl's own" "    longhorn-system/$IM: eviction refused 60 times $PDB; still on timmy when the drain ended" "$last"

samename=$(block SAMENAME)
assert_has "of two same-named pods, the one that left reads evicted" \
	"    alpha/same-0: eviction refused 1 times $PDB; evicted in the end" "$samename"
assert_has "and the one still on the node does not" \
	"    beta/same-0: eviction refused 1 times $PDB; still on timmy when the drain ended" "$samename"
assert_eq "a successful drain keeps its success through the filter" 0 "$(field other_rc)"
assert_eq "a drain in a terminating namespace prints no line more than twice" 2 "$(field other_max)"
assert_has "a pod recreated on the node after the drain began reads as recreated" \
	"    alpha/same-0: eviction refused 1 times $PDB; evicted, then recreated on timmy" "$(block RECREATED)"
assert_has "kubectl's pending-pod trailer passes through" "pod/$IM" "$drain"
assert_has "and its pending-node trailer" "There are pending nodes to be drained:" "$drain"
other=$(block OTHER)
assert_eq "the terminating-namespace retry is printed once" 1 "$(grep -c '^error when evicting pod "web-0" from terminating namespace' <<<"$other")"
assert_has "and ends as a counted line" "    doomed/web-0: eviction refused 5 times (pods \"web-0\" is forbidden: unable to create new content in namespace doomed because it is being terminated); evicted in the end" "$other"
assert_eq "an unrecognised line passes through twice" 2 "$(grep -c '^W1106 07:00:00.000000 12345 warnings.go:70\] some future kubectl notice$' <<<"$other")"
assert_has "and the rest are named and counted" "    repeated 3 more times: W1106 07:00:00.000000 12345 warnings.go:70] some future kubectl notice" "$other"
edges=$(block EDGES)
assert_has "a pod accepted but still terminating says so" \
	"    alpha/same-0: eviction refused 1 times $PDB; evicted, still terminating on timmy" "$edges"
assert_has "a pod that cannot be read has no outcome claimed" \
	"    beta/same-0: eviction refused 1 times $PDB; outcome unknown (cannot read the pod)" "$edges"

assert_eq "the health gate still waits for healthy" 0 "$(field gate_rc)"
assert_eq "it polled through every state" 9 "$(field gate_polls)"
gate_max=$(field gate_max)
if ((gate_max <= 2)); then ok "the health gate prints no line more than twice (max $gate_max)"; else bad "the health gate repeats a line $gate_max times"; fi
gate=$(block GATE)
assert_eq "a volume degraded through every poll is printed once" 1 "$(grep -c '    degraded: garage-0	attached/degraded' <<<"$gate")"
assert_eq "a volume that heals first is printed once" 1 "$(grep -c '    degraded: garage-1	attached/degraded' <<<"$gate")"
assert_eq "a volume is printed in its first state" 1 "$(grep -c '    degraded: garage-2	attaching/unknown' <<<"$gate")"
assert_eq "and again when its state changes" 1 "$(grep -c '    degraded: garage-2	attached/degraded' <<<"$gate")"
assert_eq "an outage of kubectl is reported once" 1 "$(grep -c 'kubectl unavailable' <<<"$gate")"
assert_has "the gate still ends healthy" "Longhorn volumes healthy." "$gate"
assert_eq "three kubectl outages print no line twice" 1 "$(field flaky_max)"
flaky=$(block FLAKY)
assert_eq "each outage is reported once" 3 "$(grep -c 'kubectl unavailable' <<<"$flaky")"
assert_eq "kubectl's own error is never printed on its own line" 0 "$(grep -c '^The connection to the server' <<<"$flaky")"
assert_has "its first line is in the warning" "WARN: kubectl unavailable (The connection to the server 10.0.0.1:6443 was refused - did you specify the right host or port?), retrying every 10s..." "$flaky"
assert_has "a later outage is numbered" "kubectl unavailable again (outage 3)" "$flaky"
assert_has "the flaky gate still ends healthy" "Longhorn volumes healthy." "$flaky"

# --- timmy-offline.sh ------------------------------------------------------------
orphan() { printf '%s,%s' "$(lh_volume "$1" attached healthy "$2")" "$(lh_replica "$1" timmy)"; }
three="[$(orphan garage-0 manu),$(orphan garage-2 wemby),$(orphan grafana manu)]"
two="[$(orphan garage-0 manu),$(orphan garage-2 wemby)]"
one="[$(orphan garage-0 manu)]"
detached="[$(lh_volume garage-0 detached unknown manu),$(lh_replica garage-0 timmy)]"
(
	# shellcheck source=../scripts/timmy-offline.sh
	source "$REPO_ROOT/scripts/timmy-offline.sh"
	set +e
	sleep() { SECONDS=$((SECONDS + $1)); } # time passes, instantly
	volumes "$three" "$three" "$two" "$two" "$one" "$one" "$detached"
	wait=$(wait_cross_node_detached 2>&1 | strip)
	echo "wait_rc=$?"
	echo "wait_max=$(max_repeat "$wait")"
	printf 'WAIT\n%s\nEND\n' "$wait"
	# Never detaching: the refusal names what is still attached, since the
	# loop printed each row only once.
	volumes "$two"
	timeout=$( (DETACH_TIMEOUT_SECONDS=30 wait_cross_node_detached) 2>&1 | strip)
	echo "timeout_rc=$?"
	printf 'TIMEOUT\n%s\nEND\n' "$timeout"
) >"$TMPDIR_TEST/to.out" 2>&1
nm=$(<"$TMPDIR_TEST/to.out")

assert_eq "the detach wait still returns once detached" 0 "$(field wait_rc)"
wait_max=$(field wait_max)
if ((wait_max <= 2)); then ok "the detach wait prints no line more than twice (max $wait_max)"; else bad "the detach wait repeats a line $wait_max times"; fi
wait=$(block WAIT)
assert_eq "a volume attached through every poll is listed once" 1 "$(grep -c 'still attached: garage/data-garage-0' <<<"$wait")"
assert_eq "one that detaches in the middle is listed once" 1 "$(grep -c 'still attached: garage/data-garage-2' <<<"$wait")"
assert_eq "one that detaches first is listed once" 1 "$(grep -c 'still attached: garage/data-grafana' <<<"$wait")"
assert_eq "a detach wait that times out still refuses" 1 "$(field timeout_rc)"
timeout=$(block TIMEOUT)
assert_has "its refusal names the volumes still attached" \
	"volumes still attached after 30s (garage/data-garage-0	attached on manu; garage/data-garage-2	attached on wemby); not safe to power off" "$timeout"
assert_eq "it listed each of them once before refusing" 1 "$(grep -c 'still attached: garage/data-garage-2' <<<"$timeout")"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
((FAIL == 0))
