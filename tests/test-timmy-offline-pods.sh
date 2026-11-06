#!/usr/bin/env bash
# test-timmy-offline-pods.sh — hermetic tests for the pod-health report that ends
# `timmy-offline.sh up` and `status`: pods neither Running nor Completed are
# listed as a warning that never changes the exit status. No cluster needed:
# KUBECTL and NODE_MAINTENANCE point at stubs.
# Run: bash tests/test-timmy-offline-pods.sh

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
OUTAGE=$REPO_ROOT/scripts/timmy-offline.sh
TMPDIR_TEST=$(mktemp -d)
trap 'rm -rf "$TMPDIR_TEST"' EXIT

STUB_LOG=$TMPDIR_TEST/calls.log
STUB_PODS=$TMPDIR_TEST/pods # what `kubectl get pods -A --no-headers` prints

cat >"$TMPDIR_TEST/kubectl" <<'STUB'
#!/usr/bin/env bash
# Pods come from $STUB_PODS (STUB_FAIL_PODS=1 fails the read). Workload reads
# answer 0 replicas, so up has nothing to wait for; Longhorn has no volumes.
set -euo pipefail
echo "$*" >>"$STUB_LOG"
if [[ $1 == -n ]]; then shift 2; fi
case "$1" in
get)
  case "$2" in
  pods)
    [[ ${STUB_FAIL_PODS:-0} == 0 ]] || { echo "The connection to the server was refused" >&2; exit 1; }
    cat "$STUB_PODS"
    ;;
  volumes.longhorn.io*) echo '{"items":[]}' ;;
  *) printf 0 ;;
  esac
  ;;
exec) for a in "$@"; do [[ $a != add ]] || echo 0b5c1f0e-7a3d-4c51-9e2b-1d2f3a4b5c6d; done ;;
esac
STUB
chmod +x "$TMPDIR_TEST/kubectl"

# node-maintenance.sh stand-in: no cycle recorded, every subcommand succeeds
# unless STUB_NM_<SUBCOMMAND>_RC says otherwise.
cat >"$TMPDIR_TEST/node-maintenance.sh" <<'STUB'
#!/usr/bin/env bash
echo "nm: $*" >>"$STUB_LOG"
rc_var=STUB_NM_$(tr 'a-z-' 'A-Z_' <<<"$1")_RC
if [[ $1 == cycle-status ]]; then echo none; fi
exit "${!rc_var:-0}"
STUB
chmod +x "$TMPDIR_TEST/node-maintenance.sh"

export KUBECTL=$TMPDIR_TEST/kubectl NODE_MAINTENANCE=$TMPDIR_TEST/node-maintenance.sh
export STUB_LOG STUB_PODS XDG_STATE_HOME=$TMPDIR_TEST/state
STATE_DIR=$XDG_STATE_HOME/homelab-timmy-offline

CRASHLOOP='longhorn-system   csi-provisioner-6c8b9d7f4-x2k9q    0/1   CrashLoopBackOff   93 (2m14s ago)   19d'
PENDING='viking            openviking-0                       0/1   Pending            0                 3m'
HEALTHY='garage            garage-0                           1/1   Running            0                 3m
kube-system       k3s-datastore-backup-29371-abcde   0/1   Completed          0                 6h'

set +e # assertions below inspect failures themselves; a grep miss must not end the run

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
assert_lacks() { # desc needle haystack
	if [[ $3 != *"$2"* ]]; then ok "$1"; else bad "$1 ('$2' in output)"; fi
}

# Reset the stubs; pods are the given rows. up needs one record from down to
# proceed: a recorded silence, the lightest of them.
reset() {
	printf '%s\n' "$@" >"$STUB_PODS"
	: >"$STUB_LOG"
	rm -rf "$XDG_STATE_HOME"
	mkdir -p "$STATE_DIR"
	echo 0b5c1f0e-7a3d-4c51-9e2b-1d2f3a4b5c6d >"$STATE_DIR/silence-id"
}

run() { # <subcommand>: output without colour codes, then the exit status on the last line
	local out rc=0
	out=$(bash "$OUTAGE" "$1" 2>&1) || rc=$?
	printf '%s\n%s\n' "$(sed $'s/\033\\[[0-9;]*m//g' <<<"$out")" "$rc"
}

# --- up -------------------------------------------------------------------------
reset "$HEALTHY" "$CRASHLOOP" "$PENDING"
out=$(run up)
assert_eq "up with a crashlooping pod still succeeds" 0 "${out##*$'\n'}"
assert_has "up lists the crashlooping pod" "csi-provisioner-6c8b9d7f4-x2k9q    0/1   CrashLoopBackOff   93 (2m14s ago)   19d" "$out"
assert_has "up lists the pending pod" "openviking-0" "$out"
assert_has "up marks the list as a warning" "WARN: pods neither Running nor Completed" "$out"
assert_lacks "up leaves Running pods out" "garage-0 " "$out"
assert_lacks "up leaves Completed pods out" "k3s-datastore-backup-29371" "$out"
back=$(grep -n 'timmy is back in service' <<<"$out" | cut -d: -f1)
warned=$(grep -n 'pods neither Running' <<<"$out" | cut -d: -f1)
if [[ -n $back && -n $warned ]] && ((back < warned)); then
	ok "up reports pods last, after restoring service"
else
	bad "pod report order wrong (back in service at '${back:-none}', report at '${warned:-none}')"
fi

reset "$HEALTHY"
out=$(run up)
assert_eq "up on a healthy cluster succeeds" 0 "${out##*$'\n'}"
assert_has "it says every pod is fine" "all pods Running or Completed" "$out"
assert_lacks "it prints no pod warning" "pods neither Running" "$out"

reset "$CRASHLOOP"
out=$(STUB_FAIL_PODS=1 run up)
assert_eq "up whose pod read fails still succeeds" 0 "${out##*$'\n'}"
assert_has "it warns that pod health is unavailable" "WARN: pod health unavailable (The connection to the server was refused)" "$out"

# A kubectl that answers something other than its table (the cycle tests' fake
# prints JSON for every pod read) is not reported as an unhealthy pod.
reset '{"items": []}'
out=$(run up)
assert_has "a non-table answer reads as no unhealthy pods" "all pods Running or Completed" "$out"

# --- status ---------------------------------------------------------------------
reset "$HEALTHY" "$CRASHLOOP"
out=$(run status)
assert_eq "status with a crashlooping pod succeeds" 0 "${out##*$'\n'}"
assert_has "status lists the crashlooping pod" "csi-provisioner-6c8b9d7f4-x2k9q    0/1   CrashLoopBackOff" "$out"
nm=$(grep -n '^nm: status' "$STUB_LOG" | cut -d: -f1)
pods=$(grep -n '^get pods -A' "$STUB_LOG" | cut -d: -f1)
if [[ -n $nm && -n $pods ]] && ((nm < pods)); then
	ok "status reads pods after the node and Longhorn report"
else
	bad "status order wrong (nm status at '${nm:-none}', pods at '${pods:-none}')"
fi

# Unhealthy Longhorn fails node-maintenance.sh status; the pods are still listed
# and status keeps that failure.
reset "$CRASHLOOP"
out=$(STUB_NM_STATUS_RC=3 run status)
assert_eq "status keeps node-maintenance.sh status's failure" 3 "${out##*$'\n'}"
assert_has "it still lists the crashlooping pod" "CrashLoopBackOff" "$out"

reset "$HEALTHY"
out=$(STUB_FAIL_PODS=1 run status)
assert_eq "status whose pod read fails still succeeds" 0 "${out##*$'\n'}"
assert_has "it warns that pod health is unavailable" "WARN: pod health unavailable" "$out"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
((FAIL == 0))
