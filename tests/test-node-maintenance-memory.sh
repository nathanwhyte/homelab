#!/usr/bin/env bash
# test-node-maintenance-memory.sh — hermetic tests for the IMPR-1089 memory
# preflight and pre-drain spin-down in scripts/node-maintenance.sh.
#
# No live cluster is required: KUBECTL is pointed at a stub that serves
# synthetic node/pod JSON, and the pure decision functions are exercised
# directly. Run: bash tests/test-node-maintenance-memory.sh

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
TMPDIR_TEST=$(mktemp -d)
trap 'rm -rf "$TMPDIR_TEST"' EXIT

# --- Stub kubectl ------------------------------------------------------------
# Serves synthetic cluster state and records scale commands so the test can
# assert the spin-down/restore symmetry without touching a real cluster.
STUB_LOG=$TMPDIR_TEST/scale.log
STUB_NODES=$TMPDIR_TEST/nodes.json
STUB_PODS=$TMPDIR_TEST/pods.json
STUB_DEPLOY_SELECTORS=$TMPDIR_TEST/deploy-selectors
: >"$STUB_DEPLOY_SELECTORS"

cat >"$TMPDIR_TEST/kubectl" <<'STUB'
#!/usr/bin/env bash
# $STUB_LOG, $STUB_NODES, $STUB_PODS, $STUB_REPLICAS are exported by the harness.
set -euo pipefail
case "$1" in
get)
  case "$2" in
  nodes) cat "$STUB_NODES" ;;
  pods)
    # Honor --field-selector spec.nodeName=<node> and -n <namespace>.
    node=""; ns=""; prev=""
    for a in "$@"; do
      if [[ $prev == "-n" ]]; then ns=$a; fi
      if [[ $a == spec.nodeName=* ]]; then node=${a#spec.nodeName=}; fi
      prev=$a
    done
    if [[ -n $node ]]; then
      [[ ${STUB_FAIL_TARGET:-0} == 0 ]] || exit 1
      if [[ ${STUB_MALFORMED_TARGET:-0} == 1 ]]; then
        printf 'invalid json\n'
        exit 0
      fi
      jq --arg n "$node" '{items: [.items[] | select(.spec.nodeName == $n)]}' "$STUB_PODS"
    elif [[ -n $ns ]]; then
      # A namespaced list is the spin-down wait's read. STUB_FAIL_SPINDOWN_READ
      # models an unreadable response and STUB_SPINDOWN_SLEEP a stalled one, which
      # the wait must bound rather than block on.
      [[ ${STUB_FAIL_SPINDOWN_READ:-0} == 0 ]] || exit 1
      if [[ ${STUB_SPINDOWN_SLEEP:-0} != 0 ]]; then sleep "$STUB_SPINDOWN_SLEEP"; fi
      jq --arg ns "$ns" '{items: [.items[] | select(.metadata.namespace == $ns)]}' "$STUB_PODS"
    else
      cat "$STUB_PODS"
    fi
    ;;
  deployment)
    # get deployment <name> -n <ns> -o jsonpath='{.spec.replicas}'
    # get deployment <name> -n <ns> -o json   -- selector, for the spin-down wait
    ns=""; name=""; fmt=""
    shift 2  # drop "get deployment"
    while (($#)); do
      case "$1" in
      -n) ns=$2; shift 2 ;;
      -o) fmt=$2; shift 2 ;;
      --request-timeout=*) shift ;;
      *) name=$1; shift ;;
      esac
    done
    if [[ $fmt == json ]]; then
      labels=$(grep "^$ns/$name=" "$STUB_DEPLOY_SELECTORS" 2>/dev/null | cut -d= -f2-)
      printf '{"spec":{"selector":{"matchLabels":%s}}}\n' "${labels:-null}"
    else
      grep "^$ns/$name=" "$STUB_REPLICAS" | cut -d= -f2
    fi
    ;;
  esac
  ;;
scale)
  # scale deployment <name> -n <ns> --replicas=N
  ns=""; name=""; replicas=""
  shift
  while (($#)); do
    case "$1" in
    -n) ns=$2; shift 2 ;;
    --replicas=*) replicas=${1#--replicas=}; shift ;;
    *) name=$1; shift ;;
    esac
  done
  [[ ${STUB_FAIL_SCALE:-} != "$ns/$name=$replicas" ]] || exit 1
  echo "$ns/$name -> $replicas" >>"$STUB_LOG"
  sed "s|^$ns/$name=.*|$ns/$name=$replicas|" "$STUB_REPLICAS" >"$STUB_REPLICAS.next"
  mv "$STUB_REPLICAS.next" "$STUB_REPLICAS"
  ;;
esac
STUB
chmod +x "$TMPDIR_TEST/kubectl"

# --- Synthetic cluster state -------------------------------------------------
# Three nodes: wemby (target), manu, timmy. Each 64Gi allocatable.
# wemby's pods request 20Gi total; manu+timmy already have 50Gi requested
# between them, leaving 78Gi headroom — comfortably above wemby's 20Gi.
cat >"$STUB_NODES" <<'JSON'
{"items":[
  {"metadata":{"name":"wemby"},"status":{"allocatable":{"memory":"64Gi"}}},
  {"metadata":{"name":"manu"},"status":{"allocatable":{"memory":"64Gi"}}},
  {"metadata":{"name":"timmy"},"status":{"allocatable":{"memory":"64Gi"}}}
]}
JSON

# Pods: wemby carries 20Gi of requests; manu 30Gi; timmy 20Gi.
cat >"$STUB_PODS" <<'JSON'
{"items":[
  {"spec":{"nodeName":"wemby","containers":[{"resources":{"requests":{"memory":"10Gi"}}},{"resources":{"requests":{"memory":"10Gi"}}}]}},
  {"spec":{"nodeName":"manu","containers":[{"resources":{"requests":{"memory":"30Gi"}}}]}},
  {"spec":{"nodeName":"timmy","containers":[{"resources":{"requests":{"memory":"20Gi"}}}]}}
]}
JSON

export KUBECTL=$TMPDIR_TEST/kubectl
export STUB_LOG STUB_NODES STUB_PODS STUB_DEPLOY_SELECTORS
export XDG_STATE_HOME=$TMPDIR_TEST/state

# Source the script under test (its main() is guarded, so this only defines
# functions and variables).
# shellcheck source=../scripts/node-maintenance.sh
source "$REPO_ROOT/scripts/node-maintenance.sh"

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

assert_eq "state stays in the fixture" "$TMPDIR_TEST/state/homelab-node-maintenance" "$MAINTENANCE_STATE_DIR"

# --- mem_quantity_to_bytes ---------------------------------------------------
assert_eq "6Gi -> bytes" "$((6 * 1024 * 1024 * 1024))" "$(mem_quantity_to_bytes 6Gi)"
assert_eq "512Mi -> bytes" "$((512 * 1024 * 1024))" "$(mem_quantity_to_bytes 512Mi)"
assert_eq "bare int -> bytes" "1000" "$(mem_quantity_to_bytes 1000)"

# --- memory_headroom_verdict (pure decision) ---------------------------------
assert_eq "block when target > headroom" "block" "$(memory_headroom_verdict 100 50)"
assert_eq "pass when target well under headroom" "pass" "$(memory_headroom_verdict 20 100)"
assert_eq "warn when target consumes >80% of headroom" "warn" "$(memory_headroom_verdict 90 100)"
assert_eq "pass at exactly 80% threshold" "pass" "$(memory_headroom_verdict 80 100)"

# --- node_pod_memory_bytes (stubbed kubectl) ---------------------------------
assert_eq "wemby pod memory = 20Gi" "$((20 * 1024 * 1024 * 1024))" "$(node_pod_memory_bytes wemby)"

# --- remaining_headroom_bytes (stubbed kubectl) ------------------------------
# manu: 64Gi - 30Gi = 34Gi; timmy: 64Gi - 20Gi = 44Gi; total 78Gi (no host reservation).
HOST_MEMORY_RESERVATIONS=""
assert_eq "remaining headroom = 78Gi" "$((78 * 1024 * 1024 * 1024))" "$(remaining_headroom_bytes wemby)"

# Host reservations (IMPR-1075): timmy's host Ollama unit is invisible to pod
# accounting, so its MemoryMax comes off timmy's allocatable. 78Gi - 16Gi = 62Gi.
HOST_MEMORY_RESERVATIONS="timmy=16Gi"
assert_eq "host reservation lowers headroom to 62Gi" "$((62 * 1024 * 1024 * 1024))" "$(remaining_headroom_bytes wemby)"
# A reservation on the drain target itself must not count (that node is leaving).
HOST_MEMORY_RESERVATIONS="wemby=16Gi"
assert_eq "reservation on the target node is ignored" "$((78 * 1024 * 1024 * 1024))" "$(remaining_headroom_bytes wemby)"
HOST_MEMORY_RESERVATIONS="timmy=16Gi manu=2Gi"
assert_eq "multiple reservations subtract per node" "$((60 * 1024 * 1024 * 1024))" "$(remaining_headroom_bytes wemby)"
if (HOST_MEMORY_RESERVATIONS="timmy" remaining_headroom_bytes wemby) >/dev/null 2>&1; then
	bad "malformed reservation entry should fail"
else
	ok "malformed reservation entry fails closed"
fi
HOST_MEMORY_RESERVATIONS=""

# Unscheduled Pending pods reserve no capacity on any node.
jq '.items += [{"status":{"phase":"Pending"},"spec":{"containers":[{"resources":{"requests":{"memory":"1Gi"}}}]}}]' "$STUB_PODS" >"$STUB_PODS.next"
mv "$STUB_PODS.next" "$STUB_PODS"
assert_eq "Pending pod does not break headroom" "$((78 * 1024 * 1024 * 1024))" "$(remaining_headroom_bytes wemby)"

for override in 0 1; do
	if (
		export STUB_FAIL_TARGET=1
		memory_headroom_preflight wemby "$override"
	) >/dev/null 2>&1; then
		bad "failed target read passed with override=$override"
	else
		ok "failed target read blocks with override=$override"
	fi
done
if (
	export STUB_MALFORMED_TARGET=1
	memory_headroom_preflight wemby 0
) >/dev/null 2>&1; then
	bad "malformed target JSON passed"
else
	ok "malformed target JSON blocks"
fi

# --- memory_headroom_preflight (orchestrator) --------------------------------
# 20Gi target vs 78Gi headroom -> pass (no abort).
if memory_headroom_preflight wemby 0 >/dev/null 2>&1; then
	ok "preflight passes when headroom is ample"
else
	bad "preflight should pass when headroom is ample"
fi

# --- acceptance: insufficient headroom blocks the drain ----------------------
# Rewrite the synthetic state so the remaining nodes have almost no headroom:
# manu and timmy each carry 60Gi of requests, leaving 4Gi+4Gi=8Gi headroom,
# while wemby's pods request 20Gi — clearly will not fit.
cat >"$STUB_PODS" <<'JSON'
{"items":[
  {"spec":{"nodeName":"wemby","containers":[{"resources":{"requests":{"memory":"10Gi"}}},{"resources":{"requests":{"memory":"10Gi"}}}]}},
  {"spec":{"nodeName":"manu","containers":[{"resources":{"requests":{"memory":"60Gi"}}}]}},
  {"spec":{"nodeName":"timmy","containers":[{"resources":{"requests":{"memory":"60Gi"}}}]}}
]}
JSON

# Without override: must abort (die -> exit 1). Run in a subshell so the
# sourced script's `die` (which calls `exit`) cannot kill the test harness.
if (memory_headroom_preflight wemby 0) >/dev/null 2>&1; then
	bad "preflight should BLOCK when headroom is insufficient"
else
	ok "preflight blocks when headroom is insufficient (no override)"
fi

# With override: must proceed (exit 0).
if (memory_headroom_preflight wemby 1) >/dev/null 2>&1; then
	ok "preflight proceeds with --override-memory"
else
	bad "preflight should proceed when override is set"
fi

# --- spin-down / restore symmetry --------------------------------------------
export STUB_REPLICAS=$TMPDIR_TEST/replicas
cat >"$STUB_REPLICAS" <<'REPLICAS'
viking/openviking=1
viking/ov-vectordb=1
REPLICAS

spin_down_memory_services
# Both should have been scaled to 0 (llama/ollama left the list in IMPR-1075:
# it is a host systemd unit now, not a Deployment).
for svc in viking/openviking viking/ov-vectordb; do
	if grep -q "^$svc -> 0$" "$STUB_LOG"; then
		ok "spin-down scaled $svc to 0"
	else
		bad "spin-down did not scale $svc to 0"
	fi
done

restore_memory_services
# Both should have been restored to 1.
for svc in viking/openviking viking/ov-vectordb; do
	if grep -q "^$svc -> 1$" "$STUB_LOG"; then
		ok "restore scaled $svc back to 1"
	else
		bad "restore did not scale $svc back to 1"
	fi
done

# State file must be cleaned up after restore.
if [[ -e $(spin_down_state_file) ]]; then
	bad "spin-down state file not cleaned up after restore"
else
	ok "spin-down state file cleaned up after restore"
fi

# --- restore is a no-op when nothing was spun down ---------------------------
rm -f "$STUB_LOG"
restore_memory_services
if [[ -s $STUB_LOG ]]; then
	bad "restore should be a no-op with no prior spin-down"
else
	ok "restore is a no-op with no prior spin-down"
fi

# A partial spin-down followed by a retry must preserve the original counts.
export STUB_FAIL_SCALE=viking/ov-vectordb=0
if spin_down_memory_services >/dev/null 2>&1; then
	bad "partial spin-down should fail"
else
	ok "partial spin-down propagates scale failure"
fi
assert_eq "first deployment really stopped" "viking/openviking=0" "$(grep '^viking/openviking=' "$STUB_REPLICAS")"
unset STUB_FAIL_SCALE
spin_down_memory_services
restore_memory_services
assert_eq "retry restores original openviking replicas" "viking/openviking=1" "$(grep '^viking/openviking=' "$STUB_REPLICAS")"

# Exercise finish itself, keeping every cluster/SSH surface stubbed. The scale
# stub persists replica changes, so a retry observes actual partial recovery.
spin_down_memory_services
touch "$TMPDIR_TEST/cordoned"
export STUB_FAIL_SCALE=viking/openviking=1
finish_fixture() (
	wait_for_new_boot() { :; }
	run_ssh() { :; }
	wait_for_api() { :; }
	wait_for_node_ready() { :; }
	wait_longhorn_healthy() { :; }
	cordoned_nodes() {
		if [[ -e $TMPDIR_TEST/cordoned ]]; then printf 'wemby\n'; fi
	}
	kubectl() {
		[[ $1 == uncordon ]] || return 1
		rm "$TMPDIR_TEST/cordoned"
	}
	cmd_finish wemby 12345678-1234-1234-1234-123456789abc
)
if finish_fixture >/dev/null 2>&1; then
	bad "finish should fail on partial restoration"
else
	ok "finish propagates restore failure"
fi
if [[ -e $TMPDIR_TEST/cordoned && -s $(spin_down_state_file) ]]; then
	ok "failed finish retains cordon and recovery state"
else
	bad "failed finish discarded recovery prerequisites"
fi
unset STUB_FAIL_SCALE
if finish_fixture >/dev/null 2>&1; then
	ok "finish retry completes"
else
	bad "finish retry failed"
fi
for svc in viking/openviking viking/ov-vectordb; do
	assert_eq "finish restored $svc" "$svc=1" "$(grep "^$svc=" "$STUB_REPLICAS")"
done
if [[ ! -e $TMPDIR_TEST/cordoned && ! -e $(spin_down_state_file) ]]; then
	ok "successful finish clears cordon and recovery state"
else
	bad "successful finish left stale state"
fi

# --- BUG-1178: target demand counts only what a drain can move ---------------
# `kubectl drain --ignore-daemonsets` never evicts DaemonSet-owned pods, so
# counting them was demand the drain could never move.
cat >"$STUB_PODS" <<'JSON'
{"items":[
  {"metadata":{},"spec":{"nodeName":"wemby","containers":[{"resources":{"requests":{"memory":"5Gi"}}}]}},
  {"metadata":{"ownerReferences":[{"kind":"DaemonSet","name":"ds-agent"}]},"spec":{"nodeName":"wemby","containers":[{"resources":{"requests":{"memory":"2Gi"}}}]}}
]}
JSON
assert_eq "DaemonSet-owned pods are not movable demand" "$((5 * 1024 * 1024 * 1024))" "$(node_pod_memory_bytes wemby)"

# A terminating pod's demand is NOT gone: its controller may already be
# recreating it, so excluding it would understate what the remaining nodes must
# absorb. The spin-down's own victims are handled by waiting for them to leave,
# which is why this asserts the demand is retained.
jq '.items += [{"metadata":{"deletionTimestamp":"2026-09-26T00:00:00Z"},"spec":{"nodeName":"wemby","containers":[{"resources":{"requests":{"memory":"3Gi"}}}]}}]' "$STUB_PODS" >"$STUB_PODS.next"
mv "$STUB_PODS.next" "$STUB_PODS"
assert_eq "a terminating pod is still counted as demand" "$((8 * 1024 * 1024 * 1024))" "$(node_pod_memory_bytes wemby)"

# An ordinary ReplicaSet-owned pod counts, as it always did.
jq '.items += [{"metadata":{"ownerReferences":[{"kind":"ReplicaSet","name":"rs"}]},"spec":{"nodeName":"wemby","containers":[{"resources":{"requests":{"memory":"1Gi"}}}]}}]' "$STUB_PODS" >"$STUB_PODS.next"
mv "$STUB_PODS.next" "$STUB_PODS"
assert_eq "a plain ReplicaSet-owned pod still counts as demand" "$((9 * 1024 * 1024 * 1024))" "$(node_pod_memory_bytes wemby)"

# --- BUG-1178: advice names a lever that can still help ----------------------
# Block fixture: wemby requests 20Gi against 8Gi of headroom on manu+timmy.
cat >"$STUB_PODS" <<'JSON'
{"items":[
  {"metadata":{},"spec":{"nodeName":"wemby","containers":[{"resources":{"requests":{"memory":"20Gi"}}}]}},
  {"metadata":{},"spec":{"nodeName":"manu","containers":[{"resources":{"requests":{"memory":"60Gi"}}}]}},
  {"metadata":{},"spec":{"nodeName":"timmy","containers":[{"resources":{"requests":{"memory":"60Gi"}}}]}}
]}
JSON

blocked_plain=$( (memory_headroom_preflight wemby 0 0) 2>&1 ) || true
if [[ $blocked_plain == *"--spin-down"* ]]; then
	ok "blocked preflight offers --spin-down when it has not run"
else
	bad "blocked preflight should offer --spin-down when it has not run"
fi

blocked_spun=$( (memory_headroom_preflight wemby 0 1) 2>&1 ) || true
if [[ $blocked_spun == *"Re-run with --spin-down"* ]]; then
	bad "blocked preflight recommends --spin-down while it is already in effect"
elif [[ $blocked_spun == *"--override-memory"* ]]; then
	ok "blocked preflight names only a lever that can still help"
else
	bad "blocked preflight names no lever when --spin-down is already in effect"
fi

# Warn fixture: wemby requests 20Gi against 24Gi of headroom (>80%, not over).
cat >"$STUB_PODS" <<'JSON'
{"items":[
  {"metadata":{},"spec":{"nodeName":"wemby","containers":[{"resources":{"requests":{"memory":"20Gi"}}}]}},
  {"metadata":{},"spec":{"nodeName":"manu","containers":[{"resources":{"requests":{"memory":"50Gi"}}}]}},
  {"metadata":{},"spec":{"nodeName":"timmy","containers":[{"resources":{"requests":{"memory":"54Gi"}}}]}}
]}
JSON
warned=$(memory_headroom_preflight wemby 0 1 2>&1)
if [[ $warned == *"TIGHT"* && $warned == *"already in effect"* ]]; then
	ok "warn states that --spin-down is already in effect"
else
	bad "warn should state that --spin-down is already in effect"
fi

# --- BUG-1178: the spin-down wait gates the measurement ----------------------
# The wait polls a pod list, so these exercise the three things it must do:
# return once the pods are gone, keep polling (and warn) while they are not, and
# stay bounded when a cluster read stalls. Each call goes through an external
# watchdog so a wait that never returns fails the suite rather than stalling it.
WATCHDOG=""
if command -v timeout >/dev/null 2>&1; then WATCHDOG=timeout; fi

# Run a snippet against a freshly sourced script, under the watchdog when the
# platform has one. A stalled read is modelled by STUB_SPINDOWN_SLEEP, which the
# wait must cut short rather than block on.
watched() { # max_seconds snippet
	if [[ -n $WATCHDOG ]]; then
		"$WATCHDOG" "$1" bash -c "source '$REPO_ROOT/scripts/node-maintenance.sh'; $2"
	else
		bash -c "source '$REPO_ROOT/scripts/node-maintenance.sh'; $2"
	fi
}

# Membership is decided by the Deployment selector, so the stub must serve one per
# service. The cluster runs openviking-test and ov-vectordb-test beside the pair
# with distinct selectors — which is what keeps them apart here.
cat >"$STUB_DEPLOY_SELECTORS" <<'SELECTORS'
viking/openviking={"app":"openviking"}
viking/ov-vectordb={"app":"ov-vectordb"}
viking/openviking-test={"app":"openviking-test"}
SELECTORS

# One scaled-down Deployment pod, still terminating: it carries a
# deletionTimestamp precisely because that must NOT read as gone.
terminating_viking_pods() {
	cat >"$STUB_PODS" <<'JSON'
{"items":[
  {"metadata":{"namespace":"viking","name":"openviking-6c9b675f9f-h5v9h","labels":{"app":"openviking"},"deletionTimestamp":"2026-09-26T00:00:00Z","ownerReferences":[{"kind":"ReplicaSet","name":"openviking-6c9b675f9f"}]},"spec":{"containers":[]}}
]}
JSON
}

# Gone: must return promptly and without the timeout warning. A wait that never
# recognises the empty state would sit here until SPIN_DOWN_TIMEOUT_SECONDS and
# then warn, so both checks are load-bearing.
printf '{"items":[]}' >"$STUB_PODS"
spin_down_start=$SECONDS
wait_out=$(watched 25 'SPIN_DOWN_TIMEOUT_SECONDS=10 wait_for_spin_down_pods_gone' 2>&1) && wait_rc=0 || wait_rc=$?
spin_down_elapsed=$((SECONDS - spin_down_start))
if ((wait_rc == 0)) && [[ $wait_out != *"still present"* ]] && ((spin_down_elapsed < 5)); then
	ok "spin-down wait returns promptly once the pods are gone"
else
	bad "spin-down wait did not return promptly and un-warned (rc=${wait_rc} elapsed=${spin_down_elapsed}s out='${wait_out}')"
fi

# A sibling Deployment sharing the name prefix must not be mistaken for the
# target: openviking-test runs beside openviking, and its pods carry a different
# selector value, so they are not this Deployment's to wait on.
cat >"$STUB_PODS" <<'JSON'
{"items":[
  {"metadata":{"namespace":"viking","name":"openviking-test-5d946b994-wbkrj","labels":{"app":"openviking-test"},"ownerReferences":[{"kind":"ReplicaSet","name":"openviking-test-5d946b994"}]},"spec":{"containers":[]}}
]}
JSON
spin_down_start=$SECONDS
wait_out=$(watched 25 'SPIN_DOWN_TIMEOUT_SECONDS=10 wait_for_spin_down_pods_gone' 2>&1) && wait_rc=0 || wait_rc=$?
spin_down_elapsed=$((SECONDS - spin_down_start))
if ((wait_rc == 0)) && [[ $wait_out != *"still present"* ]] && ((spin_down_elapsed < 5)); then
	ok "spin-down wait ignores a sibling deployment's pods"
else
	bad "spin-down wait treated a sibling's pods as its own (rc=${wait_rc} elapsed=${spin_down_elapsed}s out='${wait_out}')"
fi

# Still terminating: the wait must keep polling to its deadline and then warn.
# This is what a stub that returns immediately, or ignores the configured
# timeout, cannot satisfy.
terminating_viking_pods
spin_down_start=$SECONDS
wait_out=$(watched 30 'SPIN_DOWN_TIMEOUT_SECONDS=2 wait_for_spin_down_pods_gone' 2>&1) || true
spin_down_elapsed=$((SECONDS - spin_down_start))
if [[ $wait_out == *"still present after 2s"* ]]; then
	ok "spin-down wait warns when the pods never leave"
else
	bad "spin-down wait should warn when the pods never leave (got: ${wait_out})"
fi
if ((spin_down_elapsed >= 2 && spin_down_elapsed < 25)); then
	ok "spin-down wait polls until its deadline rather than returning early"
else
	bad "spin-down wait did not respect its deadline (elapsed ${spin_down_elapsed}s)"
fi

# A stalled read must be cut short at the budget, not merely bounded overall. With
# two services, a budget computed once per pass lets each stall a full allowance
# and runs the wait to twice SPIN_DOWN_TIMEOUT_SECONDS. Each fixture read sleeps 8s
# against a 4s deadline, so a per-read bound finishes near 4s and a per-pass bound
# near 8s: the assertion separates them rather than accepting either.
if [[ -n $WATCHDOG ]]; then
	printf '{"items":[]}' >"$STUB_PODS"
	export STUB_SPINDOWN_SLEEP=8
	spin_down_start=$SECONDS
	watched 30 'SPIN_DOWN_TIMEOUT_SECONDS=4 wait_for_spin_down_pods_gone' >/dev/null 2>&1 || true
	spin_down_elapsed=$((SECONDS - spin_down_start))
	unset STUB_SPINDOWN_SLEEP
	if ((spin_down_elapsed < 6)); then
		ok "spin-down wait bounds each stalled read to the remaining budget"
	else
		bad "spin-down wait let stalled reads exceed its budget (elapsed ${spin_down_elapsed}s)"
	fi
else
	printf 'skip spin-down wait read-bound test (no timeout(1) on this platform)\n'
fi

echo
echo "$PASS passed, $FAIL failed"
((FAIL == 0))
