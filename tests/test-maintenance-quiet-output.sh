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
# volume reads replay \$STUB_VOLUMES_DIR/<n>.json in order (a missing file
# fails the read), the last one repeating.
echo "\$*" >>"\$STUB_LOG"
case "\$1" in
drain)
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
	echo 'There are pending pods in node "timmy" when an error occurred: error when evicting pods/"$IM" -n "longhorn-system": global timeout reached: 5m0s' >&2
	echo 'error: unable to drain node "timmy" due to error: error when evicting pods/"$IM" -n "longhorn-system": global timeout reached: 5m0s, continuing command...' >&2
	exit 1
	;;
get)
	n=\$(cat "\$STUB_VOLUMES_DIR/next" 2>/dev/null || echo 1)
	f=\$STUB_VOLUMES_DIR/\$n.json
	[[ -e \$STUB_VOLUMES_DIR/\$((n + 1)).json || -e \$STUB_VOLUMES_DIR/\$((n + 1)).fail ]] && echo \$((n + 1)) >"\$STUB_VOLUMES_DIR/next"
	[[ -e \$f ]] || exit 1
	cat "\$f"
	;;
esac
STUB
chmod +x "$TMPDIR_TEST/kubectl"
export PATH=$TMPDIR_TEST:$PATH KUBECTL=$TMPDIR_TEST/kubectl
export STUB_LOG=$TMPDIR_TEST/calls.log STUB_VOLUMES_DIR=$TMPDIR_TEST/volumes

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

	raw=$("$KUBECTL" drain timmy 2>&1)
	out=$("$KUBECTL" drain timmy 2>&1 | drain_filter)
	rc=$?
	echo "rc=$rc"
	echo "raw_max=$(max_repeat "$raw")"
	echo "max=$(max_repeat "$out")"
	echo "lines=$(wc -l <<<"$out" | tr -d ' ')"
	printf 'OUT\n%s\nEND\n' "$out"

	volumes "[$(lh_volume garage-0 attached degraded)]" \
		"[$(lh_volume garage-0 attached degraded)]" \
		"[$(lh_volume garage-0 attached degraded)]" \
		fail fail fail \
		"[$(lh_volume garage-0 attached degraded),$(lh_volume garage-2 attaching unknown)]" \
		"[$(lh_volume garage-0 attached degraded),$(lh_volume garage-2 attaching unknown)]" \
		"[$(lh_volume garage-0 attached healthy)]"
	gate=$(wait_longhorn_healthy 2>&1 | strip)
	echo "gate_rc=$?"
	echo "gate_polls=$(grep -c volumes.longhorn.io "$STUB_LOG")"
	echo "gate_max=$(max_repeat "$gate")"
	printf 'GATE\n%s\nEND\n' "$gate"
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
assert_has "the instance-manager ends as one counted line" \
	"    longhorn-system/$IM: eviction refused 60 times (Cannot evict pod as it would violate the pod's disruption budget.); not evicted when the drain ended" "$drain"
assert_has "a pod evicted after retries says so" \
	"    garage/garage-1: eviction refused 2 times (Cannot evict pod as it would violate the pod's disruption budget.); evicted in the end" "$drain"
assert_has "kubectl's own eviction line passes through" "pod/garage-1 evicted" "$drain"
assert_has "kubectl's timeout error passes through" 'error: unable to drain node "timmy"' "$drain"
assert_has "the DaemonSet warning passes through" "Warning: ignoring DaemonSet-managed Pods" "$drain"
last=$(tail -1 <<<"$drain")
assert_eq "the counted lines come last, after kubectl's own" "    longhorn-system/$IM: eviction refused 60 times (Cannot evict pod as it would violate the pod's disruption budget.); not evicted when the drain ended" "$last"

assert_eq "the health gate still waits for healthy" 0 "$(field gate_rc)"
assert_eq "it polled through every state" 9 "$(field gate_polls)"
gate_max=$(field gate_max)
if ((gate_max <= 2)); then ok "the health gate prints no line more than twice (max $gate_max)"; else bad "the health gate repeats a line $gate_max times"; fi
gate=$(block GATE)
assert_eq "the unchanged degraded list is printed once per change" 2 "$(grep -c '    degraded: garage-0	attached/degraded' <<<"$gate")"
assert_eq "the new volume appears when the list changes" 1 "$(grep -c '    degraded: garage-2	attaching/unknown' <<<"$gate")"
assert_eq "an outage of kubectl is reported once" 1 "$(grep -c 'kubectl unavailable' <<<"$gate")"
assert_has "the gate still ends healthy" "Longhorn volumes healthy." "$gate"

# --- timmy-offline.sh ------------------------------------------------------------
attached="[$(lh_volume garage-0 attached healthy manu),$(lh_replica garage-0 timmy)]"
both="[$(lh_volume garage-0 attached healthy manu),$(lh_replica garage-0 timmy),$(lh_volume garage-2 attached healthy wemby),$(lh_replica garage-2 timmy)]"
detached="[$(lh_volume garage-0 detached unknown manu),$(lh_replica garage-0 timmy)]"
(
	# shellcheck source=../scripts/timmy-offline.sh
	source "$REPO_ROOT/scripts/timmy-offline.sh"
	set +e
	sleep() { :; }
	volumes "$both" "$both" "$both" "$attached" "$attached" "$attached" "$detached"
	wait=$(wait_cross_node_detached 2>&1 | strip)
	echo "wait_rc=$?"
	echo "wait_max=$(max_repeat "$wait")"
	printf 'WAIT\n%s\nEND\n' "$wait"
) >"$TMPDIR_TEST/to.out" 2>&1
nm=$(<"$TMPDIR_TEST/to.out")

assert_eq "the detach wait still returns once detached" 0 "$(field wait_rc)"
wait_max=$(field wait_max)
if ((wait_max <= 2)); then ok "the detach wait prints no line more than twice (max $wait_max)"; else bad "the detach wait repeats a line $wait_max times"; fi
wait=$(block WAIT)
assert_eq "garage-0 is listed once per change of the list" 2 "$(grep -c 'still attached: garage/data-garage-0' <<<"$wait")"
assert_eq "garage-2 is listed while it is attached" 1 "$(grep -c 'still attached: garage/data-garage-2' <<<"$wait")"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
((FAIL == 0))
