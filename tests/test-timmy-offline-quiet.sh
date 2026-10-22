#!/usr/bin/env bash
# test-timmy-offline-quiet.sh — hermetic tests for the outage-quieting steps in
# scripts/timmy-offline.sh: the Alertmanager silence and the CronJob
# suspend/resume symmetry. No cluster needed: KUBECTL points at a stub.
# Run: bash tests/test-timmy-offline-quiet.sh

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
TMPDIR_TEST=$(mktemp -d)
trap 'rm -rf "$TMPDIR_TEST"' EXIT

STUB_LOG=$TMPDIR_TEST/calls.log
STUB_SUSPEND=$TMPDIR_TEST/suspend-state # lines: ns/name=true|false
STUB_SILENCE_ID=0b5c1f0e-7a3d-4c51-9e2b-1d2f3a4b5c6d

cat >"$TMPDIR_TEST/kubectl" <<'STUB'
#!/usr/bin/env bash
# Every call is recorded; cronjob suspend state lives in $STUB_SUSPEND. Node
# reads answer from STUB_NODE_UNSCHED / STUB_NODE_READY, or fail outright with
# STUB_FAIL_NODE_GET=1; workload reads answer STUB_REPLICAS for both spec and
# ready counts so a wait returns at once.
set -euo pipefail
echo "$*" >>"$STUB_LOG"
ns=""
if [[ $1 == -n ]]; then ns=$2; shift 2; fi
case "$1" in
get)
  case "$2" in
  cronjob)
    # get cronjob <name> -o jsonpath='{.spec.suspend}'
    [[ ${STUB_FAIL_GET:-} != "$ns/$3" ]] || exit 1
    grep "^$ns/$3=" "$STUB_SUSPEND" | cut -d= -f2
    ;;
  node)
    # STUB_FAIL_NODE_GET=1 fails every node read; a jsonpath fragment
    # (unschedulable, Ready) fails only the read that asks for it.
    case "${STUB_FAIL_NODE_GET:-0}" in
    0) ;;
    1) echo "Error from server: temporary read failure" >&2; exit 1 ;;
    *) [[ $* != *"$STUB_FAIL_NODE_GET"* ]] || { echo "Error from server: temporary read failure" >&2; exit 1; } ;;
    esac
    if [[ $* == *unschedulable* ]]; then printf '%s' "${STUB_NODE_UNSCHED:-}"; else printf '%s' "${STUB_NODE_READY:-True}"; fi
    ;;
  volumes.longhorn.io*) echo '{"items":[]}' ;;
  *) printf '%s' "${STUB_REPLICAS:-1}" ;;
  esac
  ;;
scale) ;;
patch)
  # patch cronjob <name> --type=merge -p '{"spec":{"suspend":X}}'
  name=$3; body=${*: -1}
  val=false; [[ $body == *'"suspend":true'* ]] && val=true
  sed "s|^$ns/$name=.*|$ns/$name=$val|" "$STUB_SUSPEND" >"$STUB_SUSPEND.next"
  mv "$STUB_SUSPEND.next" "$STUB_SUSPEND"
  ;;
exec)
  # exec <pod> -c alertmanager -- amtool --alertmanager.url=... silence add|expire ...
  [[ ${STUB_FAIL_AMTOOL:-0} == 0 ]] || { echo "connection refused" >&2; exit 1; }
  # What amtool prints for a silence Alertmanager no longer has (lost on an
  # unclean restart): a 404, exit 1.
  [[ ${STUB_AMTOOL_NOTFOUND:-0} == 0 ]] || { echo "amtool: error: [DELETE /silence/{silenceID}][404] deleteSilenceNotFound" >&2; exit 1; }
  for a in "$@"; do
    if [[ $a == add ]]; then echo "$STUB_SILENCE_ID"; fi
  done
  ;;
create) ;;
esac
STUB
chmod +x "$TMPDIR_TEST/kubectl"

# node-maintenance.sh stand-in: records every call as "nm: <args>" in the same
# log as kubectl, so ordering between the two is one grep -n; exits with the
# per-subcommand code in STUB_NM_<SUBCOMMAND>_RC (default 0).
cat >"$TMPDIR_TEST/node-maintenance.sh" <<'STUB'
#!/usr/bin/env bash
echo "nm: $*" >>"$STUB_LOG"
rc_var=STUB_NM_$(tr 'a-z-' 'A-Z_' <<<"$1")_RC
exit "${!rc_var:-0}"
STUB
chmod +x "$TMPDIR_TEST/node-maintenance.sh"
export NODE_MAINTENANCE=$TMPDIR_TEST/node-maintenance.sh

# redrain reads timmy's boot ID over ssh; answer with a fixed one.
cat >"$TMPDIR_TEST/ssh" <<'STUB'
#!/usr/bin/env bash
echo "ssh: $*" >>"$STUB_LOG"
echo "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
STUB
chmod +x "$TMPDIR_TEST/ssh"
# The stubs shadow the real kubectl and ssh for anything that escapes $KUBECTL.
export PATH=$TMPDIR_TEST:$PATH

# Run a command the way the script runs it (errexit on) and report its status.
# Not `if (set -e; …)`: bash ignores errexit inside an if condition, subshell
# included, so a refusal the script leaves to errexit would look like success.
run_cmd() {
	(
		set -e
		"$@"
	) >/dev/null 2>&1
}

reset_cluster() {
	cat >"$STUB_SUSPEND" <<'EOF'
kube-system/k3s-datastore-backup=false
compendium/compendium-sync=false
viking/ovlock-janitor=true
EOF
	: >"$STUB_LOG"
	rm -rf "$TMPDIR_TEST/state"
}

export KUBECTL=$TMPDIR_TEST/kubectl
export STUB_LOG STUB_SUSPEND STUB_SILENCE_ID
export XDG_STATE_HOME=$TMPDIR_TEST/state

# shellcheck source=../scripts/timmy-offline.sh
source "$REPO_ROOT/scripts/timmy-offline.sh"
set +e # assertions below inspect failures themselves

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
state_of() { grep "^$1=" "$STUB_SUSPEND" | cut -d= -f2; }

assert_eq "state stays in the fixture" "$TMPDIR_TEST/state/homelab-timmy-offline" "$STATE_DIR"

# --- suspend / resume symmetry ------------------------------------------------
reset_cluster
suspend_cronjobs >/dev/null
assert_eq "down suspends k3s-datastore-backup" true "$(state_of kube-system/k3s-datastore-backup)"
assert_eq "down suspends compendium-sync" true "$(state_of compendium/compendium-sync)"
assert_eq "only CronJobs down suspended are recorded" \
	"kube-system/k3s-datastore-backup compendium/compendium-sync" "$(tr '\n' ' ' <"$SUSPEND_FILE" | sed 's/ $//')"

suspend_cronjobs >/dev/null
assert_eq "re-running down does not duplicate entries" 2 "$(wc -l <"$SUSPEND_FILE" | tr -d ' ')"

resume_cronjobs >/dev/null
assert_eq "up resumes k3s-datastore-backup" false "$(state_of kube-system/k3s-datastore-backup)"
assert_eq "up resumes compendium-sync" false "$(state_of compendium/compendium-sync)"
assert_eq "a CronJob suspended before down stays suspended" true "$(state_of viking/ovlock-janitor)"
if [[ -e $SUSPEND_FILE ]]; then bad "resume clears its state file"; else ok "resume clears its state file"; fi

reset_cluster
if (STUB_FAIL_GET=compendium/compendium-sync suspend_cronjobs) >/dev/null 2>&1; then
	bad "an unreadable CronJob should stop down"
else
	ok "an unreadable CronJob stops down (fail closed)"
fi

# --- alert silence ------------------------------------------------------------
reset_cluster
silence_alerts >/dev/null
assert_eq "silence ID is recorded" "$STUB_SILENCE_ID" "$(cat "$SILENCE_FILE" 2>/dev/null)"
assert_eq "silence matches every alert" 1 "$(grep -c 'silence add .*alertname=~".+"' "$STUB_LOG")"
assert_eq "silence has a hard end" 1 "$(grep -c -- '--duration=24h' "$STUB_LOG")"
silence_alerts >/dev/null
assert_eq "re-running down adds no second silence" 1 "$(grep -c 'silence add' "$STUB_LOG")"

STUB_FAIL_AMTOOL=1 expire_silence >/dev/null 2>&1
assert_eq "a failed expire keeps the ID for a retry" "$STUB_SILENCE_ID" "$(cat "$SILENCE_FILE" 2>/dev/null)"
: >"$STUB_LOG" # the stub logs the failed attempt too; count only the retry
expire_silence >/dev/null
assert_eq "up expires exactly the recorded silence" 1 "$(grep -c "silence expire $STUB_SILENCE_ID" "$STUB_LOG")"
if [[ -e $SILENCE_FILE ]]; then bad "expire clears the ID"; else ok "expire clears the ID"; fi

reset_cluster
if STUB_FAIL_AMTOOL=1 silence_alerts >/dev/null 2>&1; then
	ok "an unreachable Alertmanager does not block down"
else
	bad "an unreachable Alertmanager should warn, not fail"
fi
if [[ -s $SILENCE_FILE ]]; then bad "no ID recorded when amtool fails"; else ok "no ID recorded when amtool fails"; fi

# A silence Alertmanager lost (power cut inside its snapshot interval) is reported
# and cleared, not retried forever.
reset_cluster
silence_alerts >/dev/null
expire_out=$(STUB_AMTOOL_NOTFOUND=1 expire_silence 2>&1)
assert_eq "a 404 on expire exits 0" 0 "$?"
if [[ -e $SILENCE_FILE ]]; then bad "a lost silence clears the ID"; else ok "a lost silence clears the ID"; fi
assert_eq "a lost silence is reported as uncovered, not expired" 1 "$(grep -c 'WARN.*no longer exists.*not covered' <<<"$expire_out")"

# --- down asks node-maintenance.sh before it mutates anything ------------------
# The commands run against the stubs with errexit on, as the script runs them.
line_of() { grep -n -F -- "$1" "$STUB_LOG" | head -1 | cut -d: -f1; }
mutations() { grep -c -E '^(-n [^ ]+ (patch|scale) |-n grafana exec .* silence add)' "$STUB_LOG"; }

# 2026-10-21: the refusal came after the silence, the suspends and the park.
reset_cluster
STUB_NM_PREFLIGHT_RC=1 run_cmd cmd_down
rc=$?
assert_eq "down stops when node-maintenance.sh preflight refuses" 1 "$rc"
assert_eq "a refused preflight costs no silence, suspend or scale" 0 "$(mutations)"
assert_eq "a refused preflight leaves no state behind" 0 "$(find "$TMPDIR_TEST/state" -type f 2>/dev/null | wc -l | tr -d ' ')"

reset_cluster
run_cmd cmd_down
rc=$?
assert_eq "down completes against the stubs" 0 "$rc"
preflight_line=$(line_of 'nm: preflight timmy')
silence_line=$(line_of 'silence add')
if [[ -n $preflight_line && -n $silence_line ]] && ((preflight_line < silence_line)); then
	ok "down runs node-maintenance.sh preflight before its first mutation"
else
	bad "down must run preflight before the silence (preflight at '${preflight_line:-none}', silence at '${silence_line:-none}')"
fi
assert_eq "down still drains through node-maintenance.sh reboot --no-reboot --spin-down" 1 "$(grep -c 'nm: reboot timmy --no-reboot --spin-down' "$STUB_LOG")"

# The already-cordoned re-run goes to redrain, which never asks preflight (it
# would refuse on the cordoned node).
reset_cluster
STUB_NODE_UNSCHED=true run_cmd cmd_down
assert_eq "an already-cordoned timmy skips preflight" 0 "$(grep -c 'nm: preflight' "$STUB_LOG")"
assert_eq "an already-cordoned timmy re-drains directly" 1 "$(grep -c '^drain timmy ' "$STUB_LOG")"

# --- up without cycle state ---------------------------------------------------
park_fixture() { # parked state as a completed park would leave it
	mkdir -p "$STATE_DIR"
	printf 'statefulset/garage/garage\t3\n' >"$SCALE_FILE"
}
cycle_fixture() { # the boot ID node-maintenance.sh saves when the drain ran
	mkdir -p "$TMPDIR_TEST/state/homelab-node-maintenance"
	printf 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee\n' >"$TMPDIR_TEST/state/homelab-node-maintenance/timmy.boot-id"
}

reset_cluster
park_fixture
cycle_fixture
run_cmd cmd_up
rc=$?
assert_eq "up with cycle state completes" 0 "$rc"
assert_eq "up with cycle state finishes through node-maintenance.sh" 1 "$(grep -c 'nm: finish timmy' "$STUB_LOG")"
assert_eq "up with cycle state does not take the fallback" 0 "$(grep -c 'nm: restore-services' "$STUB_LOG")"

reset_cluster
park_fixture
STUB_FAIL_NODE_GET=1 run_cmd cmd_up
rc=$?
assert_eq "up without cycle state fails closed when every node read fails" 1 "$rc"
assert_eq "failed node reads scale nothing" 0 "$(grep -c ' scale ' "$STUB_LOG")"

# Each read on its own: a die inside a $( ) comparison would end only the
# subshell and read as "not cordoned" (Codex R2), with the Ready read masking it.
reset_cluster
park_fixture
STUB_FAIL_NODE_GET=unschedulable run_cmd cmd_up
rc=$?
assert_eq "up without cycle state fails closed when only the cordon read fails" 1 "$rc"
assert_eq "a failed cordon read restores and unparks nothing" 0 "$(grep -c -E 'nm: restore-services| scale ' "$STUB_LOG")"

reset_cluster
park_fixture
STUB_FAIL_NODE_GET=Ready run_cmd cmd_up
rc=$?
assert_eq "up without cycle state fails closed when only the Ready read fails" 1 "$rc"
assert_eq "a failed Ready read restores and unparks nothing" 0 "$(grep -c -E 'nm: restore-services| scale ' "$STUB_LOG")"

# down chooses between preflight and redrain on the same read.
reset_cluster
STUB_FAIL_NODE_GET=unschedulable run_cmd cmd_down
rc=$?
assert_eq "down fails closed when the cordon read fails" 1 "$rc"
assert_eq "a failed cordon read in down mutates nothing" 0 "$(mutations)"

reset_cluster
park_fixture
STUB_NODE_UNSCHED=true run_cmd cmd_up
rc=$?
assert_eq "up without cycle state refuses a cordoned timmy" 1 "$rc"
assert_eq "a cordoned timmy is neither restored nor unparked" 0 "$(grep -c -E 'nm: restore-services| scale ' "$STUB_LOG")"

reset_cluster
park_fixture
STUB_NODE_READY=False run_cmd cmd_up
rc=$?
assert_eq "up without cycle state refuses a timmy that is not Ready" 1 "$rc"

reset_cluster
park_fixture
STUB_NM_RESTORE_SERVICES_RC=1 run_cmd cmd_up
rc=$?
assert_eq "up without cycle state stops when restore-services refuses" 1 "$rc"
assert_eq "a refused restore unparks nothing" 0 "$(grep -c ' scale ' "$STUB_LOG")"

reset_cluster
park_fixture
run_cmd cmd_up
rc=$?
assert_eq "up without cycle state completes on a Ready, uncordoned timmy" 0 "$rc"
assert_eq "the fallback never calls finish" 0 "$(grep -c 'nm: finish' "$STUB_LOG")"
restore_line=$(line_of 'nm: restore-services')
longhorn_line=$(line_of 'nm: wait-longhorn')
unpark_line=$(line_of '-n garage scale statefulset garage --replicas=3')
if [[ -n $restore_line && -n $longhorn_line && -n $unpark_line ]] && ((restore_line < longhorn_line && longhorn_line < unpark_line)); then
	ok "the fallback restores services, waits for Longhorn, then unparks"
else
	bad "fallback order wrong (restore at '${restore_line:-none}', longhorn at '${longhorn_line:-none}', unpark at '${unpark_line:-none}')"
fi

# A down that stopped before park (a CronJob it could not suspend) leaves only
# the silence and suspend records; up must still clear them.
reset_cluster
silence_alerts >/dev/null
suspend_cronjobs >/dev/null
: >"$STUB_LOG"
run_cmd cmd_up
rc=$?
assert_eq "up after a down that never parked completes" 0 "$rc"
assert_eq "it resumes the CronJobs down suspended" false "$(state_of kube-system/k3s-datastore-backup)"
assert_eq "it expires the silence down created" 1 "$(grep -c "silence expire $STUB_SILENCE_ID" "$STUB_LOG")"
assert_eq "it scales nothing" 0 "$(grep -c ' scale ' "$STUB_LOG")"

reset_cluster
run_cmd cmd_up
rc=$?
assert_eq "up with nothing recorded refuses rather than running a catch-up backup" 1 "$rc"
assert_eq "it creates no catch-up job" 0 "$(grep -c 'create job' "$STUB_LOG")"

# --- catch-up backup ------------------------------------------------------------
reset_cluster
catchup_backup >/dev/null
assert_eq "up starts one catch-up backup from the CronJob" 1 \
	"$(grep -c 'create job k3s-datastore-backup-catchup-[0-9]* --from=cronjob/k3s-datastore-backup' "$STUB_LOG")"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
((FAIL == 0))
