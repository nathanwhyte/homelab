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
set -euo pipefail
echo "$*" >>"$STUB_LOG"
ns=""
if [[ $1 == -n ]]; then ns=$2; shift 2; fi
case "$1" in
get)
  # get cronjob <name> -o jsonpath='{.spec.suspend}'
  [[ ${STUB_FAIL_GET:-} != "$ns/$3" ]] || exit 1
  grep "^$ns/$3=" "$STUB_SUSPEND" | cut -d= -f2
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
# Order within cmd_down, by line: the preflight call precedes the first mutation
# (silence, suspend, park). 2026-10-21: the refusal came after all three.
down_body=$(declare -f cmd_down)
preflight_line=$(grep -n 'node-maintenance.sh" preflight' <<<"$down_body" | head -1 | cut -d: -f1)
first_mutation_line=$(grep -n -E '^\s*(silence_alerts|suspend_cronjobs|park);?$' <<<"$down_body" | head -1 | cut -d: -f1)
if [[ -n $preflight_line && -n $first_mutation_line ]] && ((preflight_line < first_mutation_line)); then
	ok "down runs node-maintenance.sh preflight before silence/suspend/park"
else
	bad "down must run node-maintenance.sh preflight before silence/suspend/park (preflight at '${preflight_line:-none}', first mutation at '${first_mutation_line:-none}')"
fi

# --- up without cycle state ---------------------------------------------------
# finish_cycle skips node-maintenance.sh finish when `down` never reached the
# drain; it must still refuse a cordoned node rather than unpark onto it.
up_body=$(declare -f finish_cycle)
assert_eq "finish_cycle keys on the node-maintenance boot-id file" 1 "$(grep -c 'NM_STATE_DIR/.NODE.boot-id' <<<"$up_body")"
assert_eq "finish_cycle refuses a cordoned node without cycle state" 1 "$(grep -c 'node_is_cordoned' <<<"$up_body")"
assert_eq "finish_cycle restores the spun-down services it skips finish for" 1 "$(grep -c 'restore-services' <<<"$up_body")"

# --- catch-up backup ------------------------------------------------------------
reset_cluster
catchup_backup >/dev/null
assert_eq "up starts one catch-up backup from the CronJob" 1 \
	"$(grep -c 'create job k3s-datastore-backup-catchup-[0-9]* --from=cronjob/k3s-datastore-backup' "$STUB_LOG")"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
((FAIL == 0))
