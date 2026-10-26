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
# Every call is recorded; cronjob suspend state lives in $STUB_SUSPEND.
# Workload reads answer STUB_REPLICAS for both spec and ready counts so a wait
# returns at once. Node lifecycle reads belong to node-maintenance.sh.
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
  node|nodes) echo "unexpected node read in planned-outage caller" >&2; exit 91 ;;
  volumes.longhorn.io*) echo '{"items":[]}' ;;
  *) printf '%s' "${STUB_REPLICAS:-1}" ;;
  esac
  ;;
scale)
  # Whether the window was open when the first workload went down.
  if [[ -f $XDG_STATE_HOME/homelab-timmy-offline/window ]]; then echo "scale saw a window" >>"$STUB_LOG"; else echo "scale saw no window" >>"$STUB_LOG"; fi
  ;;
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
  [[ ${STUB_FAIL_EXPIRE:-0} == 0 || $* != *" silence expire "* ]] || { echo "connection refused" >&2; exit 1; }
  [[ ${STUB_FAIL_GARAGE:-0} == 0 || $* != *garage-0* ]] || { echo "pod garage-0 is not running" >&2; exit 1; }
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
if [[ $1 == cycle-status ]]; then printf '%s\n' "${STUB_CYCLE_STATUS:-none}"; fi
if [[ $1 == cordon-status ]]; then printf '%s\n' "${STUB_CORDON_STATUS:-uncordoned}"; fi
exit "${!rc_var:-0}"
STUB
chmod +x "$TMPDIR_TEST/node-maintenance.sh"
export NODE_MAINTENANCE=$TMPDIR_TEST/node-maintenance.sh

# Any direct SSH from the planned-outage caller bypasses cycle ownership.
cat >"$TMPDIR_TEST/ssh" <<'STUB'
#!/usr/bin/env bash
echo "ssh: $*" >>"$STUB_LOG"
echo "unexpected SSH in planned-outage caller" >&2
exit 91
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
run_cmd_out() { # the same, printing the command's output
	(
		set -e
		"$@"
	) 2>&1
}

reset_cluster() {
	cat >"$STUB_SUSPEND" <<'EOF'
kube-system/k3s-datastore-backup=false
compendium/compendium-sync=false
viking/ovlock-janitor=true
EOF
	: >"$STUB_LOG"
	rm -rf "$TMPDIR_TEST/state"
	unset STUB_CYCLE_STATUS STUB_CORDON_STATUS
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
assert_eq "a first down drains through the cycle module with spin-down only" 1 "$(grep -c 'nm: drain timmy --spin-down$' "$STUB_LOG")"
assert_eq "a first down runs the strict preflight" 1 "$(grep -c 'nm: preflight timmy$' "$STUB_LOG")"
if [[ $(<"$WINDOW_FILE") =~ ^[0-9]+\ planned\ timmy\ outage ]]; then ok "down opens the maintenance window"; else bad "down opens the maintenance window"; fi
assert_eq "the window is open before the first workload is scaled down" "0 $(grep -c '^-n .* scale ' "$STUB_LOG")" \
	"$(grep -c 'scale saw no window' "$STUB_LOG") $(grep -c 'scale saw a window' "$STUB_LOG")"

# Round 6: a down whose silence and suspend both fail has changed nothing that
# needs a window; the silence, had it been made, counts on its own.
reset_cluster
STUB_FAIL_AMTOOL=1 STUB_FAIL_GET=kube-system/k3s-datastore-backup run_cmd cmd_down
assert_eq "down stops when a CronJob cannot be read" 1 "$?"
if [[ -e $WINDOW_FILE ]]; then bad "it leaves no window"; else ok "it leaves no window"; fi

# Round 6: mv would put the window inside a directory and succeed.
for kind in directory symlink; do
	reset_cluster
	mkdir -p "$STATE_DIR"
	if [[ $kind == directory ]]; then mkdir "$WINDOW_FILE"; else ln -s "$TMPDIR_TEST/nowhere" "$WINDOW_FILE"; fi
	run_cmd cmd_down
	assert_eq "down refuses a $kind at the window path" 1 "$?"
	assert_eq "before any silence, suspend or scale ($kind)" 0 "$(mutations)"
done

# Repeating down delegates both the preflight choice and drain to the cycle
# module. The real-script integration suite exercises an actually cordoned node.
reset_cluster
run_cmd cmd_down
run_cmd cmd_down
# The stub keeps answering uncordoned, so neither down sees its own cordon.
assert_eq "an uncordoned timmy gets the strict preflight each time" 2 "$(grep -c 'nm: preflight timmy$' "$STUB_LOG")"
assert_eq "an uncordoned timmy gets a bare drain each time" 2 "$(grep -c 'nm: drain timmy --spin-down$' "$STUB_LOG")"

# A cordoned timmy is this Mac's outage only if this Mac parked it; otherwise
# (a hand cordon, or down on the other Mac) re-parking would save zeros.
reset_cluster
out=$(STUB_CORDON_STATUS=cordoned run_cmd_out cmd_down)
rc=$?
assert_eq "down refuses a cordoned timmy this Mac never parked" 1 "$rc"
assert_eq "that refusal mutates nothing" 0 "$(mutations)"
assert_eq "that refusal names the starting Mac" 1 "$(grep -c 'the Mac that started the outage' <<<"$out")"
assert_eq "that refusal runs before preflight" 0 "$(grep -c 'nm: preflight' "$STUB_LOG")"

reset_cluster
run_cmd cmd_down
STUB_CORDON_STATUS=cordoned run_cmd cmd_down
rc=$?
assert_eq "down repeats on a cordoned timmy this Mac parked" 0 "$rc"
assert_eq "only the repeat drains with --repeat" 1 "$(grep -c 'nm: drain timmy --spin-down --repeat$' "$STUB_LOG")"
assert_eq "only the repeat allows a repeat in preflight" 1 "$(grep -c 'nm: preflight timmy --allow-repeat$' "$STUB_LOG")"

for case in "rc" "invalid"; do
	reset_cluster
	if [[ $case == rc ]]; then
		STUB_NM_CORDON_STATUS_RC=1 run_cmd cmd_down
	else
		STUB_CORDON_STATUS=maybe run_cmd cmd_down
	fi
	rc=$?
	assert_eq "down fails closed on a cordon-status $case failure" 1 "$rc"
	assert_eq "a cordon-status $case failure mutates nothing" 0 "$(mutations)"
done

# --- up without cycle state ---------------------------------------------------
park_fixture() { # parked state as a completed park would leave it
	mkdir -p "$STATE_DIR"
	printf 'statefulset/garage/garage\t3\n' >"$SCALE_FILE"
}
cycle_fixture() {
	export STUB_CYCLE_STATUS=recorded
}

reset_cluster
park_fixture
cycle_fixture
open_window
run_cmd cmd_up
rc=$?
assert_eq "up with cycle state completes" 0 "$rc"
assert_eq "up with cycle state recovers through node-maintenance.sh" 1 "$(grep -c 'nm: recover timmy' "$STUB_LOG")"
assert_eq "up reads cycle evidence through its public query" 1 "$(grep -c 'nm: cycle-status timmy' "$STUB_LOG")"
if [[ -e $WINDOW_FILE ]]; then bad "up closes the maintenance window"; else ok "up closes the maintenance window"; fi

# IDEA-1150 round 4: a failed expire keeps the silence ID for a retry, but the
# outage is over, so the window must not outlive it.
reset_cluster
park_fixture
cycle_fixture
silence_alerts >/dev/null
open_window
STUB_FAIL_EXPIRE=1 run_cmd cmd_up
rc=$?
assert_eq "up with a failed expire still completes" 0 "$rc"
assert_eq "a failed expire in up keeps the silence ID" "$STUB_SILENCE_ID" "$(cat "$SILENCE_FILE" 2>/dev/null)"
if [[ -e $WINDOW_FILE ]]; then bad "a failed expire in up still closes the window"; else ok "a failed expire in up still closes the window"; fi

reset_cluster
open_window
run_cmd cmd_up
rc=$?
assert_eq "up with only a window recorded refuses" 1 "$rc"
if [[ -e $WINDOW_FILE ]]; then bad "it closes that window"; else ok "it closes that window"; fi

# Round 5: garage_check is a report. A garage-0 that cannot be exec'd used to
# end up silently under pipefail, before the silence and window were cleared.
reset_cluster
park_fixture
cycle_fixture
silence_alerts >/dev/null
open_window
up_out=$(STUB_FAIL_GARAGE=1 run_cmd_out cmd_up)
rc=$?
assert_eq "up completes when garage status cannot be read" 0 "$rc"
assert_eq "it says so" 1 "$(grep -c 'WARN.*garage status unavailable' <<<"$up_out")"
assert_eq "it still expires the silence" 1 "$(grep -c "silence expire $STUB_SILENCE_ID" "$STUB_LOG")"
if [[ -e $WINDOW_FILE ]]; then bad "it still closes the window"; else ok "it still closes the window"; fi

# --- the window's end is the silence's -------------------------------------------
window_left() { echo $(($(cut -d' ' -f1 "$WINDOW_FILE") - $(date +%s))); }
reset_cluster
(SILENCE_DURATION=4h open_window) >/dev/null
left=$(window_left)
if ((left > 14300 && left <= 14400)); then ok "the window follows SILENCE_DURATION"; else bad "the window follows SILENCE_DURATION (left ${left}s)"; fi
end_before=$(cut -d' ' -f1 "$WINDOW_FILE")
sleep 1
open_window >/dev/null
assert_eq "with no silence recorded, a re-run keeps the open window's end" "$end_before" "$(cut -d' ' -f1 "$WINDOW_FILE")"

# Round 6/7: only an open window in exactly the helper's grammar is kept.
for text in "$(($(date +%s) - 1)) planned timmy outage" "now+600 planned timmy outage" "99999999999 planned timmy outage" \
	"$(($(date +%s) + 600))" "  $(($(date +%s) + 600)) planned timmy outage" "$(($(date +%s) + 600)) planned"$'\nsecond line'; do
	reset_cluster
	mkdir -p "$STATE_DIR"
	printf '%s\n' "$text" >"$WINDOW_FILE"
	open_window >/dev/null
	left=$(window_left)
	if ((left > 86300 && left <= 86400)); then ok "down replaces a window '${text%% *}'"; else bad "down replaces a window '${text%% *}' (left ${left}s)"; fi
done

for d in 1h30m 100000d 31d 2592001s 0s 00000001s 18446744073709551617s 99999999999999999999d; do
	reset_cluster
	SILENCE_DURATION=$d run_cmd cmd_down
	assert_eq "down refuses SILENCE_DURATION=$d" 1 "$?"
	assert_eq "it refuses before any silence, suspend or scale ($d)" 0 "$(mutations)"
	if [[ -e $WINDOW_FILE ]]; then bad "and before opening a window ($d)"; else ok "and before opening a window ($d)"; fi
done
assert_eq "30d is the longest window" "2592000 2592000" "$(SILENCE_DURATION=30d silence_seconds) $(SILENCE_DURATION=2592000s silence_seconds)"

# Round 7: the window ends with the silence down made, including on a re-run
# that keeps that silence after the window has gone.
reset_cluster
run_cmd cmd_down
silence_end=$(cut -d' ' -f1 "$SILENCE_END_FILE" 2>/dev/null)
assert_eq "down records the silence's end with its ID" "$STUB_SILENCE_ID" "$(cut -d' ' -f2 "$SILENCE_END_FILE" 2>/dev/null)"
assert_eq "its window ends with that silence" "$silence_end" "$(cut -d' ' -f1 "$WINDOW_FILE")"
rm -f "$WINDOW_FILE"
sleep 1
run_cmd cmd_down
assert_eq "a re-run keeping the silence takes its recorded end" "$silence_end" "$(cut -d' ' -f1 "$WINDOW_FILE" 2>/dev/null)"
printf '%s %s\n' "$(($(date +%s) - 5))" "$STUB_SILENCE_ID" >"$SILENCE_END_FILE"
run_cmd cmd_down
if [[ -e $WINDOW_FILE ]]; then bad "a kept silence that has ended leaves no window"; else ok "a kept silence that has ended leaves no window"; fi

# Round 7: a re-run that makes the silence an earlier down could not takes its end.
reset_cluster
STUB_FAIL_AMTOOL=1 run_cmd cmd_down
assert_eq "down with no silence still opens a window" 1 "$(grep -c 'planned timmy outage' "$WINDOW_FILE" 2>/dev/null)"
printf '%s %s\n' "$(($(date +%s) + 3600))" "planned timmy outage (timmy-offline.sh down)" >"$WINDOW_FILE"
run_cmd cmd_down
left=$(window_left)
if ((left > 86300 && left <= 86400)); then ok "the re-run that made the silence takes its end"; else bad "the re-run that made the silence takes its end (left ${left}s)"; fi
assert_eq "and records it" "$(cut -d' ' -f1 "$SILENCE_END_FILE" 2>/dev/null)" "$(cut -d' ' -f1 "$WINDOW_FILE")"
if (STUB_FAIL_EXPIRE=0 expire_silence) >/dev/null && [[ ! -e $SILENCE_END_FILE ]]; then ok "expire clears the silence's end"; else bad "expire clears the silence's end"; fi

reset_cluster
park_fixture
STUB_NM_CYCLE_STATUS_RC=1 run_cmd cmd_up
rc=$?
assert_eq "up fails closed when the cycle query fails" 1 "$rc"
assert_eq "a failed cycle query scales nothing" 0 "$(grep -c ' scale ' "$STUB_LOG")"

reset_cluster
park_fixture
STUB_CYCLE_STATUS=invalid run_cmd cmd_up
rc=$?
assert_eq "up refuses an unexpected cycle-query result" 1 "$rc"
assert_eq "an invalid cycle query restores and unparks nothing" 0 "$(grep -c -E 'nm: recover| scale ' "$STUB_LOG")"

reset_cluster
park_fixture
out=$(STUB_NM_RECOVER_RC=1 run_cmd_out cmd_up)
rc=$?
assert_eq "up stops when cycle recovery refuses" 1 "$rc"
assert_eq "a refused recovery unparks nothing" 0 "$(grep -c ' scale ' "$STUB_LOG")"
assert_eq "a refused recovery says to re-run up" 1 "$(grep -c 're-run: .*timmy-offline.sh up$' <<<"$out")"

# The caller cannot distinguish a refused preflight from an unreadable cluster;
# both must stop before its first mutation. The cycle tests cover those causes.
reset_cluster
STUB_NM_PREFLIGHT_RC=2 run_cmd cmd_down
rc=$?
assert_eq "down fails closed when the preflight query fails" 1 "$rc"
assert_eq "a failed preflight query mutates nothing" 0 "$(mutations)"

reset_cluster
cycle_fixture
run_cmd cmd_up
rc=$?
assert_eq "saved cycle evidence alone is enough for up to recover" 0 "$rc"
assert_eq "up with only cycle evidence invokes recovery" 1 "$(grep -c 'nm: recover timmy' "$STUB_LOG")"

reset_cluster
park_fixture
run_cmd cmd_up
rc=$?
assert_eq "up without cycle evidence completes after recovery" 0 "$rc"
recover_line=$(line_of 'nm: recover timmy')
unpark_line=$(line_of '-n garage scale statefulset garage --replicas=3')
if [[ -n $recover_line && -n $unpark_line ]] && ((recover_line < unpark_line)); then
	ok "up recovers the cycle before unparking"
else
	bad "recovery order wrong (recover at '${recover_line:-none}', unpark at '${unpark_line:-none}')"
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
