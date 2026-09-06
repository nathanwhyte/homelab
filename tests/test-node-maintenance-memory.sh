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

cat >"$TMPDIR_TEST/kubectl" <<'STUB'
#!/usr/bin/env bash
# $STUB_LOG, $STUB_NODES, $STUB_PODS, $STUB_REPLICAS are exported by the harness.
case "$1" in
get)
  case "$2" in
  nodes) cat "$STUB_NODES" ;;
  pods)
    # Honor --field-selector spec.nodeName=<node> by filtering with jq.
    node=""
    for a in "$@"; do
      [[ $a == spec.nodeName=* ]] && node=${a#spec.nodeName=}
    done
    if [[ -n $node ]]; then
      jq --arg n "$node" '{items: [.items[] | select(.spec.nodeName == $n)]}' "$STUB_PODS"
    else
      cat "$STUB_PODS"
    fi
    ;;
  deployment)
    # get deployment <name> -n <ns> -o jsonpath='{.spec.replicas}'
    ns=""; name=""
    shift 2  # drop "get deployment"
    while (($#)); do
      case "$1" in
      -n) ns=$2; shift 2 ;;
      -o) shift 2 ;;
      *) name=$1; shift ;;
      esac
    done
    grep "^$ns/$name=" "$STUB_REPLICAS" | cut -d= -f2
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
  echo "$ns/$name -> $replicas" >>"$STUB_LOG"
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
export STUB_LOG STUB_NODES STUB_PODS
export MAINTENANCE_STATE_DIR=$TMPDIR_TEST/state

# Source the script under test (its main() is guarded, so this only defines
# functions and variables).
# shellcheck source=../scripts/node-maintenance.sh
source "$REPO_ROOT/scripts/node-maintenance.sh"

PASS=0
FAIL=0
ok() { printf 'ok   %s\n' "$1"; PASS=$((PASS + 1)); }
bad() { printf 'FAIL %s\n' "$1"; FAIL=$((FAIL + 1)); }
assert_eq() { # desc expected actual
	if [[ $2 == "$3" ]]; then ok "$1"; else bad "$1 (expected '$2', got '$3')"; fi
}

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
# manu: 64Gi - 30Gi = 34Gi; timmy: 64Gi - 20Gi = 44Gi; total 78Gi.
assert_eq "remaining headroom = 78Gi" "$((78 * 1024 * 1024 * 1024))" "$(remaining_headroom_bytes wemby)"

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
llama/ollama=1
viking/openviking=1
viking/ov-vectordb=1
REPLICAS

spin_down_memory_services
# All three should have been scaled to 0.
for svc in llama/ollama viking/openviking viking/ov-vectordb; do
	if grep -q "^$svc -> 0$" "$STUB_LOG"; then
		ok "spin-down scaled $svc to 0"
	else
		bad "spin-down did not scale $svc to 0"
	fi
done

restore_memory_services
# All three should have been restored to 1.
for svc in llama/ollama viking/openviking viking/ov-vectordb; do
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

echo
echo "$PASS passed, $FAIL failed"
((FAIL == 0))
