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
STUB_PROM=$TMPDIR_TEST/prom # what the Prometheus query through `get --raw` prints

cat >"$TMPDIR_TEST/kubectl" <<'STUB'
#!/usr/bin/env bash
# Pods come from $STUB_PODS (STUB_FAIL_PODS=1 fails the read; STUB_PODS_WARN
# adds a stderr warning to a successful one). Prometheus queries (get --raw) fail
# with STUB_FAIL_PROM=1; the boot-time query answers timmy's uptime,
# STUB_UPTIME seconds (default 200000, so the window is a full 24h; `none`
# answers no series), the coverage query STUB_SCRAPES kube-state-metrics scrapes
# (default 100000, always enough; `none` answers no series), and the restart
# query prints $STUB_PROM. Workload reads
# answer 0 replicas, so up has nothing to wait for; Longhorn has no volumes.
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
  --raw)
    [[ ${STUB_FAIL_PROM:-0} == 0 ]] || { echo 'Error from server (ServiceUnavailable): no endpoints available for service "prom-prometheus:9090"' >&2; exit 1; }
    if [[ $3 == *sum_over_time* ]]; then
      if [[ ${STUB_SCRAPES:-} == none ]]; then
        echo '{"status":"success","data":{"resultType":"vector","result":[]}}'
      else
        echo "{\"status\":\"success\",\"data\":{\"resultType\":\"vector\",\"result\":[{\"metric\":{},\"value\":[1762412400,\"${STUB_SCRAPES:-100000}\"]}]}}"
      fi
    elif [[ $3 == *node_boot_time_seconds* ]]; then
      if [[ ${STUB_UPTIME:-} == none ]]; then
        echo '{"status":"success","data":{"resultType":"vector","result":[]}}'
      else
        echo "{\"status\":\"success\",\"data\":{\"resultType\":\"vector\",\"result\":[{\"metric\":{},\"value\":[1762412400,\"${STUB_UPTIME:-200000}.25\"]}]}}"
      fi
    else
      cat "$STUB_PROM"
    fi
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
export STUB_LOG STUB_PODS STUB_PROM XDG_STATE_HOME=$TMPDIR_TEST/state
STATE_DIR=$XDG_STATE_HOME/homelab-timmy-offline

# Pod JSON as the API returns it. pod <ns> <name> <age seconds> <phase>
# <containerStatuses JSON> [extra JSON merged into the pod]. Its Ready condition
# is True when every container is ready, else False since the pod was created;
# extra may replace it.
pod() {
	local extra='{}'
	[[ -z ${6:-} ]] || extra=$6
	jq -cn --arg ns "$1" --arg name "$2" --argjson age "$3" --arg phase "$4" \
		--argjson cs "$5" --argjson extra "$extra" '
		(now - $age | floor | todate) as $created
		| {metadata: {namespace: $ns, name: $name, uid: "uid-\($name)", creationTimestamp: $created},
		 spec: {containers: [$cs[] | {name}]},
		 status: {phase: $phase, containerStatuses: $cs,
			conditions: [{type: "Ready", lastTransitionTime: $created,
				status: (if ($cs | length) > 0 and all($cs[]; .ready) then "True" else "False" end)}]}} * $extra'
}
ready_since() { # a Ready condition JSON: <True|False> <seconds ago>
	jq -cn --arg s "$1" --argjson ago "$2" '{status: {conditions: [{type: "Ready", status: $s, lastTransitionTime: (now - $ago | floor | todate)}]}}'
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
# A container that exited 0 beside a running one: not a failure, but the pod is
# not Ready, so it is listed NotReady once past the grace.
MIXED=$(pod viking ov-vectordb-0 900 Running \
	"[{\"name\":\"setup\",\"ready\":false,\"restartCount\":0,\"state\":{\"terminated\":{\"reason\":\"Completed\",\"exitCode\":0}}},$running]")
unready='{"name":"app","ready":false,"restartCount":0,"state":{"running":{}}}'
NOTREADY=$(pod omnipendium omnipendium-db-0 3600 Running "[$unready]")
STARTING=$(pod omnipendium omnipendium-db-1 3600 Running "[$unready]" "$(ready_since False 60)")
FRESH_CRASH=$(pod viking embedder-qwen-cuda-abc 3600 Running \
	'[{"name":"embedder","ready":false,"restartCount":2,"state":{"waiting":{"reason":"CrashLoopBackOff"}}}]' "$(ready_since False 60)")
SIDECAR_CRASH=$(pod viking openviking-2 900 Running "[$running]" \
	'{"spec":{"initContainers":[{"name":"proxy","restartPolicy":"Always"}]},"status":{"initContainerStatuses":[{"name":"proxy","ready":false,"restartCount":7,"state":{"waiting":{"reason":"CrashLoopBackOff"}}}]}}')
KILLED_INIT=$(pod garage garage-3 600 Pending '[{"name":"garage","ready":false,"restartCount":0,"state":{"waiting":{"reason":"PodInitializing"}}}]' \
	'{"status":{"initContainerStatuses":[{"name":"init","ready":false,"restartCount":1,"state":{"terminated":{"exitCode":137}}}]}}')
# 120s, not seconds-granular: a clock tick between building the fixture and the
# report would turn 30s into 31s; 120s and 121s both read 2m.
CREATING=$(pod llama cloud-llm-counter-7d9f-fghij 120 Pending '[{"name":"counter","ready":false,"restartCount":0,"state":{"waiting":{"reason":"ContainerCreating"}}}]')
# A slow crashloop between crashes: Running and ready, last crash minutes ago.
SLOW=$(pod longhorn-system csi-provisioner-85dd8446fb-hbjbw $((19 * 86400)) Running \
	'[{"name":"csi-provisioner","ready":true,"restartCount":94,"state":{"running":{}},"lastState":{"terminated":{"reason":"Error","exitCode":255}}}]')
# A DaemonSet pod restarted by timmy's reboots: also restarted recently.
REBOOTED=$(pod kube-system svclb-traefik-7wkkt $((60 * 86400)) Running \
	'[{"name":"lb-tcp-80","ready":true,"restartCount":48,"state":{"running":{}},"lastState":{"terminated":{"reason":"Error","exitCode":1}}}]')
# Prometheus' answer: pod UIDs whose busiest container restarted
# RESTART_WARN_24H+ times in 24h. prom <uid=count>... (fixture UIDs are uid-<name>)
prom() {
	local kv
	for kv in "$@"; do
		jq -cn --arg uid "${kv%=*}" --arg n "${kv##*=}" '{metric: {uid: $uid}, value: [1762412400, $n]}'
	done | jq -s '{status: "success", data: {resultType: "vector", result: .}}' >"$STUB_PROM"
}

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
	prom
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

reset "$HEALTHY" "$CRASHLOOP" "$PENDING" "$COMPLETED" "$INIT_CRASH" "$EVICTED" "$TERMINATING" "$SIDECAR" "$MIXED" \
	"$NOTREADY" "$STARTING" "$FRESH_CRASH" "$SIDECAR_CRASH" "$KILLED_INIT" "$CREATING"
out=$(run up)
assert_eq "up with a crashlooping pod still succeeds" 0 "${out##*$'\n'}"
assert_eq "up lists the crashlooping pod with ready, restarts and age" \
	"longhorn-system csi-provisioner-6c8b9d7f4-x2k9q 0/1 CrashLoopBackOff 93 19d" "$(row csi-provisioner-6c8b9d7f4-x2k9q "$out")"
assert_eq "a pod with no container started reads Pending" "viking openviking-0 0/1 Pending 0 3m" "$(row openviking-0 "$out")"
assert_eq "a crashing init container reads Init:CrashLoopBackOff, its restarts counted" "garage garage-2 0/1 Init:CrashLoopBackOff 4 2h" "$(row garage-2 "$out")"
assert_eq "an evicted pod reads Evicted" "llama cloud-llm-counter-7d9f-abcde 0/0 Evicted 0 1d" "$(row cloud-llm-counter-7d9f-abcde "$out")"
assert_eq "a deleted pod reads Terminating" "grafana grafana-6b5c-xyz 1/1 Terminating 0 10m" "$(row grafana-6b5c-xyz "$out")"
assert_eq "a pod not Ready past the grace reads NotReady" "omnipendium omnipendium-db-0 0/1 NotReady 0 1h" "$(row omnipendium-db-0 "$out")"
assert_lacks "a pod not Ready for under the grace is left out" "omnipendium-db-1" "$out"
assert_eq "the grace never hides a crashloop" "viking embedder-qwen-cuda-abc 0/1 CrashLoopBackOff 2 1h" "$(row embedder-qwen-cuda-abc "$out")"
assert_eq "a crashing native sidecar is listed, counted in READY and RESTARTS" "viking openviking-2 1/2 Init:CrashLoopBackOff 7 15m" "$(row openviking-2 "$out")"
assert_eq "an init container killed without a reason reads its exit code" "garage garage-3 0/1 Init:ExitCode:137 1 10m" "$(row garage-3 "$out")"
assert_eq "a container still being created reads as its phase" "llama cloud-llm-counter-7d9f-fghij 0/1 Pending 0 2m" "$(row cloud-llm-counter-7d9f-fghij "$out")"
assert_eq "a container that exited 0 beside a running one is not a failure, but the pod is NotReady" \
	"viking ov-vectordb-0 1/2 NotReady 0 15m" "$(row ov-vectordb-0 "$out")"
assert_has "the report has its column header" "NAMESPACE        NAME" "$out"
assert_has "the column header names REASON" "REASON" "$out"
assert_has "up marks the list as a warning" "WARN: pods not healthy (not Running and ready, not Completed, or restarting 5+ times in 24h):" "$out"
assert_lacks "up leaves Running and ready pods out" "garage-0 " "$out"
assert_lacks "up leaves Completed pods out" "k3s-datastore-backup-29371" "$out"
assert_lacks "a running, ready native sidecar is healthy" "openviking-1" "$out"
back=$(grep -n 'timmy is back in service' <<<"$out" | cut -d: -f1)
warned=$(grep -n 'pods not healthy' <<<"$out" | cut -d: -f1)
if [[ -n $back && -n $warned ]] && ((back < warned)); then
	ok "up reports pods last, after restoring service"
else
	bad "pod report order wrong (back in service at '${back:-none}', report at '${warned:-none}')"
fi

reset "$HEALTHY"
out=$(run up)
assert_eq "up on a healthy cluster succeeds" 0 "${out##*$'\n'}"
assert_has "it says every pod is fine" "all pods healthy (Running and ready, or Completed)" "$out"
assert_lacks "it prints no pod warning" "pods not healthy" "$out"

reset "$CRASHLOOP"
out=$(STUB_FAIL_PODS=1 run up)
assert_eq "up whose pod read fails still succeeds" 0 "${out##*$'\n'}"
assert_has "it warns that pod health is unavailable" "WARN: pod health unavailable (The connection to the server was refused)" "$out"

# A warning kubectl prints on stderr beside a successful read is not a pod
# (round 1 reproduced it as one when the table and stderr were parsed together).
reset "$HEALTHY"
out=$(STUB_PODS_WARN='Warning: Use tokens from the TokenRequest API or manually created secret-based tokens instead of auto-generated secret-based tokens.' run up)
assert_has "a stderr warning beside healthy pods reads as no unhealthy pods" "all pods healthy (Running and ready, or Completed)" "$out"
assert_lacks "it lists no pod" "pods not healthy" "$out"

# Anything but pod JSON on stdout is an unavailable report, never a pod.
reset
printf 'NAMESPACE NAME READY STATUS RESTARTS AGE\ndefault x 0/1 Pending 0 1m\n' >"$STUB_PODS"
out=$(run up)
assert_eq "up whose pod read is not JSON still succeeds" 0 "${out##*$'\n'}"
assert_has "it warns that pod health is unavailable" "WARN: pod health unavailable (kubectl did not return pod JSON)" "$out"
assert_lacks "it lists no pod" "pods not healthy" "$out"

# --- restart history (Prometheus) ---------------------------------------------
# A slow crashloop reads Running and ready between crashes; only the restart
# count over a day separates it from a pod a node reboot restarted.
decode() { printf '%b\n' "${1//%/\\x}"; } # percent-decoding (jq's @uri encodes every reserved byte)
reset "$HEALTHY" "$SLOW" "$REBOOTED" "$CRASHLOOP"
# garage-0's predecessor (same name, another UID) crashed 20 times before it was
# recreated; the current garage-0 has not restarted.
prom uid-csi-provisioner-85dd8446fb-hbjbw=42 uid-csi-provisioner-6c8b9d7f4-x2k9q=12 \
	uid-garage-1-gone=9 uid-garage-0-predecessor=20
out=$(run up)
assert_eq "up with a slow crashloop still succeeds" 0 "${out##*$'\n'}"
assert_eq "a Running pod restarting 5+ times a day is listed with its count" \
	"longhorn-system csi-provisioner-85dd8446fb-hbjbw 1/1 Running (42 restarts in 24h) 94 19d" "$(row csi-provisioner-85dd8446fb-hbjbw "$out")"
assert_lacks "a pod restarted only by node reboots is left out" "svclb-traefik-7wkkt" "$out"
assert_eq "a crashlooping pod keeps its status, not the count" \
	"longhorn-system csi-provisioner-6c8b9d7f4-x2k9q 0/1 CrashLoopBackOff 93 19d" "$(row csi-provisioner-6c8b9d7f4-x2k9q "$out")"
assert_lacks "a pod Prometheus remembers but the cluster no longer has is left out" "garage-1-gone" "$out"
assert_lacks "a recreated pod does not inherit its predecessor's restarts" "garage-0 " "$out"
queries=$(sed -n 's|^get --raw /api/v1/namespaces/grafana/services/prom-prometheus:9090/proxy/api/v1/query?query=||p' "$STUB_LOG")
assert_eq "it asks Prometheus for timmy's uptime, the window's scrape coverage, then each pod UID's busiest container, init containers included, at the threshold over the window" \
	"time() - max(node_boot_time_seconds and on (namespace, pod) kube_pod_info{node=\"timmy\"})
sum(sum_over_time(up{job=\"kube-state-metrics\"}[86400s]))
max by (uid) (changes(kube_pod_container_status_restarts_total[86400s]) or changes(kube_pod_init_container_status_restarts_total[86400s])) >= 5" \
	"$(while IFS= read -r q; do decode "$q"; done <<<"$queries")"

reset "$HEALTHY" "$REBOOTED"
out=$(run up)
assert_has "with history read and nothing flagged, it says so" \
	"all pods healthy (Running and ready, or Completed), none restarting 5+ times in 24h" "$out"

reset "$HEALTHY" "$SLOW"
prom uid-csi-provisioner-85dd8446fb-hbjbw=3
out=$(RESTART_WARN_24H=3 run up)
assert_has "RESTART_WARN_24H sets the threshold in the warning" "or restarting 3+ times in 24h" "$out"
query=$(sed -n 's|^get --raw .*query?query=||p' "$STUB_LOG" | grep -v -e node_boot_time -e sum_over_time)
assert_has "and in the query" ">= 3" "$(decode "$query")"

# E1: after an outage, restarts count from RESTART_BOOT_GRACE_SECONDS after
# timmy's boot, so the controllers the outage restarted are not crashloops.
reset "$HEALTHY" "$SLOW"
prom uid-csi-provisioner-85dd8446fb-hbjbw=42
out=$(STUB_UPTIME=11700 run up) # 3h15m: a 3h window
query=$(sed -n 's|^get --raw .*query?query=||p' "$STUB_LOG" | grep -v -e node_boot_time -e sum_over_time)
assert_has "a boot 3h15m ago gives a 3h window in the query" "[10800s]" "$(decode "$query")"
assert_eq "and the row says so" \
	"longhorn-system csi-provisioner-85dd8446fb-hbjbw 1/1 Running (42 restarts in 3h (since 15m after timmy's boot)) 94 19d" "$(row csi-provisioner-85dd8446fb-hbjbw "$out")"
assert_has "the warning names the window" "or restarting 5+ times in 3h (since 15m after timmy's boot)):" "$out"

reset "$HEALTHY"
out=$(STUB_UPTIME=11700 run up)
assert_has "nothing flagged in a short window says which window" \
	"none restarting 5+ times in 3h (since 15m after timmy's boot)" "$out"

reset "$HEALTHY" "$SLOW" "$CRASHLOOP"
prom uid-csi-provisioner-85dd8446fb-hbjbw=42
out=$(STUB_UPTIME=600 run up) # 10m: inside the 15m grace
assert_eq "up inside the grace still succeeds" 0 "${out##*$'\n'}"
assert_has "it says the history starts after the grace, and when to rerun" \
	"WARN: restart history skipped: timmy booted 10m ago, and restarts count from 15m after its boot (the outage restarted every controller that needs the API server) over at least 5m; listing pods by status only. For crashloops, rerun $OUTAGE status in 10m" "$out"
assert_eq "it asks Prometheus only for the boot time" 1 "$(grep -c -- '--raw' "$STUB_LOG")"
assert_has "it still lists pods by status" "CrashLoopBackOff" "$out"
assert_lacks "and no restart count" "hbjbw" "$out"
assert_has "the warning claims no restart rule" "WARN: pods not healthy (not Running and ready, and not Completed):" "$out"

reset "$HEALTHY"
out=$(STUB_UPTIME=none run up)
assert_has "no boot time for timmy is an unavailable history" "WARN: restart history unavailable (Prometheus has no boot time for timmy)" "$out"

reset "$HEALTHY"
out=$(RESTART_BOOT_GRACE_SECONDS=15m run up)
assert_has "a grace that is not whole seconds is refused" "RESTART_BOOT_GRACE_SECONDS must be a whole number of seconds, got '15m'" "$out"
assert_eq "and never reaches Prometheus" 0 "$(grep -c -- '--raw' "$STUB_LOG")"

# Past the grace but short of five scrapes: still skipped, and the wait rounds up.
reset "$HEALTHY"
out=$(STUB_UPTIME=1100 run up) # a 200s window; 100s still to wait
assert_has "a window under 5m is skipped, its wait rounded up" "rerun $OUTAGE status in 2m" "$out"
assert_eq "and only the boot time is asked" 1 "$(grep -c -- '--raw' "$STUB_LOG")"

reset "$HEALTHY"
out=$(STUB_UPTIME=1200 run up) # exactly 5m
query=$(sed -n 's|^get --raw .*query?query=||p' "$STUB_LOG" | grep -v -e node_boot_time -e sum_over_time)
assert_has "a 5m window is read" "[300s]" "$(decode "$query")"

# Elapsed time is not history: a window Prometheus barely scraped (it, or
# kube-state-metrics, came back late) is incomplete, not "none restarting".
reset "$HEALTHY" "$SLOW" "$CRASHLOOP"
prom uid-csi-provisioner-85dd8446fb-hbjbw=42
out=$(STUB_UPTIME=11700 STUB_SCRAPES=1 run up) # a 3h window, ~180 scrapes expected
assert_eq "up with an incomplete history still succeeds" 0 "${out##*$'\n'}"
assert_has "it says the history is incomplete, with the counts" \
	"WARN: restart history incomplete: Prometheus scraped kube-state-metrics 1 of ~180 times in the last 3h; listing pods by status only." "$out"
assert_eq "it asks for no restart counts" 0 "$(grep -c 'changes' "$STUB_LOG")"
assert_lacks "and claims none restarting nowhere" "none restarting" "$out"
assert_has "it still lists pods by status" "CrashLoopBackOff" "$out"

reset "$HEALTHY"
out=$(STUB_UPTIME=11700 STUB_SCRAPES=161 run up) # one short of 90%
assert_has "one scrape short of 90% is incomplete" "scraped kube-state-metrics 161 of ~180 times" "$out"
reset "$HEALTHY"
out=$(STUB_UPTIME=11700 STUB_SCRAPES=162 run up)
assert_has "90% is enough" "none restarting 5+ times in 3h" "$out"

reset "$HEALTHY"
out=$(STUB_SCRAPES=none run up)
assert_has "no coverage series is incomplete" "scraped kube-state-metrics 0 of ~1440 times in the last 24h" "$out"

# A leading zero would be octal to bash arithmetic (0600 is 384s).
reset "$HEALTHY"
out=$(RESTART_BOOT_GRACE_SECONDS=0600 run up)
assert_has "a grace with a leading zero is refused" "RESTART_BOOT_GRACE_SECONDS must be a whole number of seconds, got '0600'" "$out"
assert_eq "before any query" 0 "$(grep -c -- '--raw' "$STUB_LOG")"

# restart_counts checks its own inputs, whoever calls it.
reset "$HEALTHY"
out=$(RESTART_WARN_24H=nope bash -c 'source "$1"; restart_counts 86400' _ "$OUTAGE" 2>&1)
assert_has "restart_counts refuses a bad threshold itself" "RESTART_WARN_24H must be a positive integer, got 'nope'" "$out"
out=$(bash -c 'source "$1"; restart_counts 60' _ "$OUTAGE" 2>&1)
assert_has "and a window under 5m" "restart window must be 300-86400 seconds, got '60'" "$out"
assert_eq "neither reaches Prometheus" 0 "$(grep -c -- '--raw' "$STUB_LOG")"

reset "$HEALTHY"
out=$(RESTART_BOOT_GRACE_SECONDS=0 STUB_UPTIME=90000 run up)
query=$(sed -n 's|^get --raw .*query?query=||p' "$STUB_LOG" | grep -v -e node_boot_time -e sum_over_time)
assert_has "the window never reaches past 24h" "[86400s]" "$(decode "$query")"

reset "$HEALTHY" "$SLOW" "$CRASHLOOP"
out=$(STUB_FAIL_PROM=1 run up)
assert_eq "up without Prometheus still succeeds" 0 "${out##*$'\n'}"
assert_has "it warns that the restart history is unavailable" \
	'WARN: restart history unavailable (Error from server (ServiceUnavailable): no endpoints available for service "prom-prometheus:9090"); listing pods by status only' "$out"
assert_has "it names the command to rerun once Prometheus is back" "for crashloops, rerun $OUTAGE status once it answers" "$out"
assert_has "it still lists pods by status" "CrashLoopBackOff" "$out"
assert_lacks "and lists no slow crashloop it cannot see" "hbjbw" "$out"

reset "$HEALTHY"
echo '{"status":"error","errorType":"bad_data","error":"parse error"}' >"$STUB_PROM"
out=$(run up)
assert_has "a Prometheus error answer is an unavailable history" "WARN: restart history unavailable (Prometheus returned no query result)" "$out"
assert_has "the status-only report still runs" "==> all pods healthy (Running and ready, or Completed)" "$out"
assert_lacks "and claims nothing about restarts" "none restarting" "$out"

reset "$HEALTHY"
out=$(RESTART_WARN_24H='5) or vector(1' run up)
assert_has "a threshold that is not a positive integer is refused" "RESTART_WARN_24H must be a positive integer" "$out"
assert_eq "and never reaches Prometheus" 0 "$(grep -c -- '--raw' "$STUB_LOG")"

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
