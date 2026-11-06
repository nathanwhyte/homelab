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
STUB_PODS=$TMPDIR_TEST/pods # what `kubectl get pods -A -o json` prints

cat >"$TMPDIR_TEST/kubectl" <<'STUB'
#!/usr/bin/env bash
# Pods come from $STUB_PODS (STUB_FAIL_PODS=1 fails the read; STUB_PODS_WARN
# adds a stderr warning to a successful one). Workload reads answer 0 replicas,
# so up has nothing to wait for; Longhorn has no volumes.
set -euo pipefail
echo "$*" >>"$STUB_LOG"
if [[ $1 == -n ]]; then shift 2; fi
case "$1" in
get)
  case "$2" in
  pods)
    [[ ${STUB_FAIL_PODS:-0} == 0 ]] || { echo "The connection to the server was refused" >&2; exit 1; }
    [[ -z ${STUB_PODS_WARN:-} ]] || echo "$STUB_PODS_WARN" >&2
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

# Pod JSON as the API returns it. pod <ns> <name> <age seconds> <phase>
# <containerStatuses JSON> [extra JSON merged into the pod].
pod() {
	local extra='{}'
	[[ -z ${6:-} ]] || extra=$6
	jq -cn --arg ns "$1" --arg name "$2" --argjson age "$3" --arg phase "$4" \
		--argjson cs "$5" --argjson extra "$extra" '
		{metadata: {namespace: $ns, name: $name,
			creationTimestamp: (now - $age | floor | todate)},
		 spec: {containers: [$cs[] | {name}]},
		 status: {phase: $phase, containerStatuses: $cs}} * $extra'
}
running='{"name":"app","ready":true,"restartCount":0,"state":{"running":{}}}'
CRASHLOOP=$(pod longhorn-system csi-provisioner-6c8b9d7f4-x2k9q $((19 * 86400 + 3600)) Running \
	'[{"name":"csi-provisioner","ready":false,"restartCount":93,"state":{"waiting":{"reason":"CrashLoopBackOff"}}}]')
PENDING=$(pod viking openviking-0 200 Pending '[]' '{"spec":{"containers":[{"name":"openviking"}]}}')
INIT_CRASH=$(pod garage garage-2 7200 Pending '[{"name":"garage","ready":false,"restartCount":0,"state":{"waiting":{"reason":"PodInitializing"}}}]' \
	'{"status":{"initContainerStatuses":[{"name":"init","ready":false,"restartCount":4,"state":{"waiting":{"reason":"CrashLoopBackOff"}}}]}}')
EVICTED=$(pod llama cloud-llm-counter-7d9f-abcde 86400 Failed '[]' '{"status":{"reason":"Evicted"}}')
TERMINATING=$(pod grafana grafana-6b5c-xyz 600 Running "[$running]" '{"metadata":{"deletionTimestamp":"2026-11-06T00:00:00Z"}}')
HEALTHY=$(pod garage garage-0 180 Running "[$running]")
COMPLETED=$(pod kube-system k3s-datastore-backup-29371-abcde 21600 Succeeded \
	'[{"name":"backup","ready":false,"restartCount":0,"state":{"terminated":{"reason":"Completed","exitCode":0}}}]')
# A running native sidecar (an init container with restartPolicy Always) is not
# an unfinished init container.
SIDECAR=$(pod viking openviking-1 900 Running "[$running]" \
	'{"spec":{"initContainers":[{"name":"proxy","restartPolicy":"Always"}]},"status":{"initContainerStatuses":[{"name":"proxy","ready":true,"restartCount":0,"state":{"running":{}}}]}}')
# A finished container beside a running one reads Running, as in kubectl.
MIXED=$(pod viking ov-vectordb-0 900 Running \
	"[{\"name\":\"setup\",\"ready\":false,\"restartCount\":0,\"state\":{\"terminated\":{\"reason\":\"Completed\",\"exitCode\":0}}},$running]")

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

# Reset the stubs; the pod list holds the given pods. up needs one record from
# down to proceed: a recorded silence, the lightest of them.
reset() {
	printf '%s\n' "$@" | jq -s '{items: .}' >"$STUB_PODS"
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
row() { # the report row naming pod $1, whitespace collapsed
	grep -E "^    [^ ]+ +$1 " <<<"$2" | tr -s ' ' | sed 's/^ //'
}

reset "$HEALTHY" "$CRASHLOOP" "$PENDING" "$COMPLETED" "$INIT_CRASH" "$EVICTED" "$TERMINATING" "$SIDECAR" "$MIXED"
out=$(run up)
assert_eq "up with a crashlooping pod still succeeds" 0 "${out##*$'\n'}"
assert_eq "up lists the crashlooping pod with ready, restarts and age" \
	"longhorn-system csi-provisioner-6c8b9d7f4-x2k9q 0/1 CrashLoopBackOff 93 19d" "$(row csi-provisioner-6c8b9d7f4-x2k9q "$out")"
assert_eq "a pod with no container started reads Pending" "viking openviking-0 0/1 Pending 0 3m" "$(row openviking-0 "$out")"
assert_eq "a crashing init container reads Init:CrashLoopBackOff" "garage garage-2 0/1 Init:CrashLoopBackOff 0 2h" "$(row garage-2 "$out")"
assert_eq "an evicted pod reads Evicted" "llama cloud-llm-counter-7d9f-abcde 0/0 Evicted 0 1d" "$(row cloud-llm-counter-7d9f-abcde "$out")"
assert_eq "a deleted pod reads Terminating" "grafana grafana-6b5c-xyz 1/1 Terminating 0 10m" "$(row grafana-6b5c-xyz "$out")"
assert_has "the report has kubectl's column header" "NAMESPACE        NAME" "$out"
assert_has "up marks the list as a warning" "WARN: pods neither Running nor Completed" "$out"
assert_lacks "up leaves Running pods out" "garage-0 " "$out"
assert_lacks "up leaves Completed pods out" "k3s-datastore-backup-29371" "$out"
assert_lacks "a running native sidecar is not an unfinished init container" "openviking-1" "$out"
assert_lacks "a finished container beside a running one reads Running" "ov-vectordb-0" "$out"
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

# A warning kubectl prints on stderr beside a successful read is not a pod
# (round 1 reproduced it as one when the table and stderr were parsed together).
reset "$HEALTHY"
out=$(STUB_PODS_WARN='Warning: Use tokens from the TokenRequest API or manually created secret-based tokens instead of auto-generated secret-based tokens.' run up)
assert_has "a stderr warning beside healthy pods reads as no unhealthy pods" "all pods Running or Completed" "$out"
assert_lacks "it lists no pod" "pods neither Running" "$out"

# Anything but pod JSON on stdout is an unavailable report, never a pod.
reset
printf 'NAMESPACE NAME READY STATUS RESTARTS AGE\ndefault x 0/1 Pending 0 1m\n' >"$STUB_PODS"
out=$(run up)
assert_eq "up whose pod read is not JSON still succeeds" 0 "${out##*$'\n'}"
assert_has "it warns that pod health is unavailable" "WARN: pod health unavailable (kubectl did not return pod JSON)" "$out"
assert_lacks "it lists no pod" "pods neither Running" "$out"

# --- status ---------------------------------------------------------------------
reset "$HEALTHY" "$CRASHLOOP"
out=$(run status)
assert_eq "status with a crashlooping pod succeeds" 0 "${out##*$'\n'}"
assert_eq "status lists the crashlooping pod" \
	"longhorn-system csi-provisioner-6c8b9d7f4-x2k9q 0/1 CrashLoopBackOff 93 19d" "$(row csi-provisioner-6c8b9d7f4-x2k9q "$out")"
nm=$(grep -n '^nm: status' "$STUB_LOG" | cut -d: -f1)
pods=$(grep -n '^get pods -A' "$STUB_LOG" | cut -d: -f1)
if [[ -n $nm && -n $pods ]] && ((nm < pods)); then
	ok "status reads pods after the node and Longhorn report"
else
	bad "status order wrong (nm status at '${nm:-none}', pods at '${pods:-none}')"
fi

# node-maintenance.sh status fails when it cannot read the nodes or Longhorn;
# the pods are still listed and status keeps that failure.
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
