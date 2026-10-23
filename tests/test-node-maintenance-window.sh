#!/usr/bin/env bash
# test-node-maintenance-window.sh — hermetic tests for the maintenance windows
# scripts/node-maintenance.sh writes for scripts/maintenance-active.py
# (IDEA-1150): which commands open, keep and close them. No cluster or node
# needed: kubectl and ssh are stubs on PATH.
# Run: bash tests/test-node-maintenance-window.sh

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
NM=$REPO_ROOT/scripts/node-maintenance.sh
HELPER=$REPO_ROOT/scripts/maintenance-active.py
TMPDIR_TEST=$(mktemp -d)
trap 'rc=$?; rm -rf "$TMPDIR_TEST"; exit $rc' EXIT

# Cordons live in $XDG_STATE_HOME/cordoned. `cordon` records whether the cycle
# window was already open when the first cluster change ran.
cat >"$TMPDIR_TEST/kubectl" <<'STUB'
#!/usr/bin/env bash
echo "kubectl $*" >>"$STUB_LOG"
C=$XDG_STATE_HOME/cordoned
touch "$C"
case "$*" in
"get nodes -o json")
	printf '{"items":['
	sep=
	for n in wemby manu timmy; do
		u=false
		grep -qx "$n" "$C" && u=true
		printf '%s{"metadata":{"name":"%s"},"spec":{"unschedulable":%s},"status":{"allocatable":{"memory":"64Gi"}}}' "$sep" "$n" "$u"
		sep=,
	done
	printf ']}\n'
	;;
"get pdb -A -o json" | get\ pods\ -A\ -o\ json* | "get volumes.longhorn.io -n longhorn-system -o json") echo '{"items":[]}' ;;
get\ jobs\ -n\ compendium*) printf '%s' "${STUB_ACTIVE_JOB:-}" ;;
cordon\ *)
	cat "$XDG_STATE_HOME/homelab-node-maintenance/window" >"$XDG_STATE_HOME/window-at-cordon" 2>/dev/null ||
		echo absent >"$XDG_STATE_HOME/window-at-cordon"
	echo "$2" >>"$C"
	;;
uncordon\ *)
	grep -vx "$2" "$C" >"$C.n" || true
	mv "$C.n" "$C"
	;;
get\ deployment\ *jsonpath*) printf 1 ;;
get\ deployment\ *-o\ json*) echo '{"spec":{"selector":{"matchLabels":{"app":"x"}}}}' ;;
get\ pods\ -n\ *) echo '{"items":[]}' ;;
"get node "*) printf True ;;
*"amtool"*) echo '[]' ;;
*) exit 0 ;;
esac
STUB

# Answers the boot-ID, reboot and apt probes. probe_ssh needs its sentinel
# after the output. The first apt-era call records the apt window as seen
# mid-run; STUB_SSH_FAIL fails every call, STUB_APT_RC is the remote apt's code.
# STUB_APT_HANG keeps the remote run going until $XDG_STATE_HOME/apt-done exists;
# STUB_BLOCK_WINDOW puts a directory where the apt window goes.
cat >"$TMPDIR_TEST/ssh" <<'STUB'
#!/usr/bin/env bash
cmd=${*: -1}
echo "ssh $cmd" >>"$STUB_LOG"
W=$XDG_STATE_HOME/homelab-node-maintenance/apt-window
[[ -e $XDG_STATE_HOME/apt-window-seen ]] || cat "$W" >"$XDG_STATE_HOME/apt-window-seen" 2>/dev/null || echo absent >"$XDG_STATE_HOME/apt-window-seen"
[[ -z ${STUB_BLOCK_WINDOW:-} || -d $W ]] || { rm -f "$W" && mkdir "$W"; }
[[ -z ${STUB_SSH_FAIL:-} ]] || exit 255
B=$XDG_STATE_HOME/bootid
[[ -s $B ]] || echo aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee >"$B"
probe() { printf '%s\n\n__PROBE_OK__\n' "$1"; }
case $cmd in
*setsid*) exit 0 ;;
*"grep -c APT_STARTED"*) probe 1 ;;
*APT_DONE_RC=//p*) if [[ -n ${STUB_APT_HANG:-} && ! -e $XDG_STATE_HOME/apt-done ]]; then probe ""; else probe "${STUB_APT_RC:-0}"; fi ;;
*"dpkg --audit"*) probe "" ;;
*reboot-required*) probe current ;;
*boot_id*) cat "$B" ;;
"sudo reboot")
	echo bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee >"$B"
	exit 255
	;;
"uname -r") echo 6.8.0 ;;
*) exit 0 ;;
esac
STUB
# Records whether the maintenance lock was still held when the apt window was
# removed: releasing it first would let another apt run take this window as kept.
cat >"$TMPDIR_TEST/rm" <<'STUB'
#!/usr/bin/env bash
for a; do
	if [[ $a == */homelab-node-maintenance/apt-window ]]; then
		if [[ -d ${a%/apt-window}/lock ]]; then echo held; else echo released; fi >>"$XDG_STATE_HOME/lock-at-settle"
	fi
done
exec /bin/rm "$@"
STUB
# STUB_TERM_AT_WINDOW sends the script a TERM while it writes its apt window.
cat >"$TMPDIR_TEST/mv" <<'STUB'
#!/usr/bin/env bash
if [[ -n ${STUB_TERM_AT_WINDOW:-} && ${*: -1} == */apt-window ]]; then kill -TERM "$PPID"; fi
exec /bin/mv "$@"
STUB
chmod +x "$TMPDIR_TEST/kubectl" "$TMPDIR_TEST/ssh" "$TMPDIR_TEST/rm" "$TMPDIR_TEST/mv"
export PATH=$TMPDIR_TEST:$PATH KUBECTL=$TMPDIR_TEST/kubectl
export REBOOT_TIMEOUT_SECONDS=30 SPIN_DOWN_TIMEOUT_SECONDS=5
# The bounds asserted below are the script's defaults, not the caller's.
unset APT_TIMEOUT_SECONDS APT_LAUNCH_GRACE_SECONDS SSH_CMD_TIMEOUT_SECONDS MAINTENANCE_WINDOW_SECONDS
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

fresh() {
	export XDG_STATE_HOME=$TMPDIR_TEST/state.$((++RUN)) STUB_LOG
	mkdir -p "$XDG_STATE_HOME"
	STUB_LOG=$XDG_STATE_HOME/calls.log
	: >"$STUB_LOG"
	ND=$XDG_STATE_HOME/homelab-node-maintenance
}
RUN=0
nm() { bash "$NM" "$@" >"$XDG_STATE_HOME/out" 2>&1; }
# Seconds left in a window file, or "absent".
left() { if [[ -e $1 ]]; then echo $(($(cut -d' ' -f1 "$1") - $(date +%s))); else echo absent; fi; }
open_now() { [[ $(left "$1") =~ ^[0-9]+$ ]] && (($(left "$1") > 0)) && echo open || echo "not open"; }
helper() {
	python3 "$HELPER" viking >/dev/null 2>&1 && echo 0 || echo $?
}

# --- reboot cycle ----------------------------------------------------------------
fresh
nm reboot wemby
rc=$?
assert_eq "a full reboot cycle completes" 0 "$rc"
if [[ $(<"$XDG_STATE_HOME/window-at-cordon") =~ ^[0-9]+\ reboot\ cycle\ on\ wemby$ ]]; then
	ok "the cycle window is open before the cordon"
else
	bad "the cycle window is open before the cordon (got '$(<"$XDG_STATE_HOME/window-at-cordon")')"
fi
assert_eq "finish closes the cycle window" absent "$(left "$ND/window")"

fresh
nm reboot wemby --no-reboot
assert_eq "reboot --no-reboot stops with the node cordoned" wemby "$(<"$XDG_STATE_HOME/cordoned")"
assert_eq "the cycle window stays open until finish" open "$(open_now "$ND/window")"
assert_eq "it covers watches" 0 "$(helper)"
w=$(left "$ND/window")
if [[ $w =~ ^[0-9]+$ ]] && ((w > 7100 && w <= 7200)); then ok "the cycle window lasts MAINTENANCE_WINDOW_SECONDS"; else bad "the cycle window lasts MAINTENANCE_WINDOW_SECONDS (left ${w}s)"; fi

# The documented back-out: uncordon by hand. The saved boot ID stays, so
# restore-services refuses and the window is left to its end.
fresh
MAINTENANCE_WINDOW_SECONDS=2 nm reboot wemby --no-reboot
"$KUBECTL" uncordon wemby
nm restore-services
assert_eq "restore-services refuses while a boot ID is saved" 1 "$?"
assert_eq "the backed-out cycle's window is still recorded" open "$(open_now "$ND/window")"
sleep 3
assert_eq "it stops covering at its end" 1 "$(helper)"

# Refusals before the first change open nothing.
fresh
"$KUBECTL" cordon manu
nm reboot wemby
assert_eq "a refused reboot fails" 1 "$?"
assert_eq "a refused reboot opens no window" absent "$(left "$ND/window")"

# A refusal after the spin-down leaves services at 0: the window stays, and
# restore-services closes it once it has brought them back.
fresh
STUB_ACTIVE_JOB=sync-1 nm reboot wemby --no-reboot --spin-down
assert_eq "a reboot refused after its spin-down fails" 1 "$?"
assert_eq "its window stays open" open "$(open_now "$ND/window")"
nm restore-services
assert_eq "restore-services succeeds" 0 "$?"
assert_eq "restore-services closes the stopped cycle's window" absent "$(left "$ND/window")"

# --- apt -------------------------------------------------------------------------
fresh
t0=$(date +%s)
nm apt wemby manu
assert_eq "apt completes against the stubs" 0 "$?"
t1=$(date +%s)
# Per node: APT_LAUNCH_GRACE_SECONDS + APT_TIMEOUT_SECONDS + 2 x SSH_CMD_TIMEOUT_SECONDS.
if [[ $(<"$XDG_STATE_HOME/apt-window-seen") =~ ^([0-9]+)\ apt\ upgrade\ on\ wemby\ manu$ ]] &&
	((BASH_REMATCH[1] >= t0 + 2 * 3900 && BASH_REMATCH[1] <= t1 + 2 * 3900)); then
	ok "the apt window is open, with apt's own bound for each node, while apt runs"
else
	bad "the apt window is open, with apt's own bound for each node, while apt runs (saw '$(<"$XDG_STATE_HOME/apt-window-seen")', started $t0)"
fi
assert_eq "a finished apt run closes its window" absent "$(left "$ND/apt-window")"
assert_eq "it closes it before releasing the lock" held "$(cat "$XDG_STATE_HOME/lock-at-settle" 2>/dev/null)"

fresh
STUB_APT_RC=100 nm apt wemby
assert_eq "a failed remote apt fails the run" 1 "$?"
assert_eq "the remote run has ended, so its window is closed" absent "$(left "$ND/apt-window")"

# A launch failure means no remote run started, so nothing is upgrading.
fresh
STUB_SSH_FAIL=1 nm apt wemby
assert_eq "an apt launch failure fails the run" 1 "$?"
assert_eq "it closes the apt window" absent "$(left "$ND/apt-window")"

# A timeout (or an interrupt) after the remote run started leaves it going, so
# the window keeps one more APT_TIMEOUT_SECONDS for that node only.
fresh
STUB_APT_HANG=1 APT_TIMEOUT_SECONDS=5 nm apt wemby manu
assert_eq "an apt timeout fails the run" 1 "$?"
w=$(left "$ND/apt-window")
if [[ $(cat "$ND/apt-window" 2>/dev/null) =~ ^[0-9]+\ apt\ upgrade\ on\ wemby\ \(stopped\ early ]] &&
	((w >= 3 && w <= 5)); then
	ok "it keeps the window for the started run only, for APT_TIMEOUT_SECONDS"
else
	bad "it keeps the window for the started run only, for APT_TIMEOUT_SECONDS (got '$(cat "$ND/apt-window" 2>/dev/null)', ${w}s left)"
fi
assert_eq "manu's upgrade never started" 1 "$(grep -c 'setsid' "$STUB_LOG")"

# Round 6 P2: a TERM to apt's own pid (the one its lock names) was held until
# the whole wait returned, so apt kept polling, saw the run end, and still kept
# a "stopped early" window. It now stops at its next step, without polling again.
fresh
STUB_APT_HANG=1 bash "$NM" apt wemby >"$XDG_STATE_HOME/out" 2>&1 &
pid=$!
# The poll, not the launch command, which also names APT_DONE_RC.
poll='APT_DONE_RC=//p'
for _ in $(seq 1 100); do grep -qF "$poll" "$STUB_LOG" && break; sleep 0.1; done
polls=$(grep -cF "$poll" "$STUB_LOG")
kill -TERM "$pid"
touch "$XDG_STATE_HOME/apt-done" # the remote run ends right after
wait "$pid"
assert_eq "a TERM to the apt pid stops it" 143 "$?"
assert_eq "it stops without polling the remote run again" "$polls" "$(grep -cF "$poll" "$STUB_LOG")"
w=$(left "$ND/apt-window")
if [[ $(cat "$ND/apt-window" 2>/dev/null) =~ \ apt\ upgrade\ on\ wemby\ \(stopped\ early ]] && ((w > 3500 && w <= 3600)); then
	ok "the run it did not see end keeps the window for APT_TIMEOUT_SECONDS"
else
	bad "the run it did not see end keeps the window for APT_TIMEOUT_SECONDS (got '$(cat "$ND/apt-window" 2>/dev/null)', ${w}s left)"
fi
assert_eq "the lock is released" absent "$([[ -e $ND/lock ]] && echo present || echo absent)"

# Round 6: a later apt run must not shorten the window an earlier one kept.
fresh
mkdir -p "$ND"
kept="$(($(date +%s) + 600)) apt upgrade on wemby (stopped early; its remote run may continue)"
printf '%s\n' "$kept" >"$ND/apt-window"
nm apt manu
assert_eq "apt on another node completes" 0 "$?"
assert_eq "the earlier run's kept window is restored afterwards" "$kept" "$(cat "$ND/apt-window" 2>/dev/null)"
if [[ $(<"$XDG_STATE_HOME/apt-window-seen") =~ \ apt\ upgrade\ on\ manu$ ]]; then ok "the longer window covers the run"; else bad "the longer window covers the run (saw '$(<"$XDG_STATE_HOME/apt-window-seen")')"; fi

# A kept window that ends after this run's own bound stays in force throughout.
fresh
mkdir -p "$ND"
kept="$(($(date +%s) + 99999)) apt upgrade on wemby (stopped early; its remote run may continue)"
printf '%s\n' "$kept" >"$ND/apt-window"
nm apt manu
assert_eq "a kept window longer than the run covers it" "$kept" "$(<"$XDG_STATE_HOME/apt-window-seen")"
assert_eq "and stays afterwards" "$kept" "$(cat "$ND/apt-window" 2>/dev/null)"

# Round 7: only a window in exactly the helper's grammar is carried over; apt
# must not turn one the helper rejects into a valid window.
for text in "  $(($(date +%s) + 600))   apt upgrade on wemby  " "$(($(date +%s) + 600)) apt upgrade on wemby"$'\nsecond line'; do
	fresh
	mkdir -p "$ND"
	printf '%s\n' "$text" >"$ND/apt-window"
	nm apt manu
	assert_eq "apt does not carry over a malformed window ('${text:0:14}…')" absent "$(left "$ND/apt-window")"
done

# Round 7: a TERM while apt writes its window, before any launch, leaves none.
fresh
STUB_TERM_AT_WINDOW=1 nm apt wemby manu
assert_eq "a TERM during the opening write stops apt" 143 "$?"
assert_eq "with no window left behind" absent "$(left "$ND/apt-window")"
assert_eq "and no SSH call" 0 "$(grep -c '^ssh ' "$STUB_LOG" || true)"

# mv would put the window inside a directory and succeed.
fresh
mkdir -p "$ND/apt-window"
nm apt wemby
assert_eq "apt refuses a directory at its window path" 1 "$?"
assert_eq "before any SSH call" 0 "$(grep -c '^ssh ' "$STUB_LOG" || true)"

# Round 6: a window that cannot be settled must not keep the lock (set -e used
# to abort the EXIT trap before the release).
fresh
STUB_BLOCK_WINDOW=1 STUB_APT_RC=100 nm apt wemby
assert_eq "apt with an unwritable window path still fails as before" 1 "$?"
assert_eq "it says the window could not be settled" 1 "$(grep -c 'could not settle' "$XDG_STATE_HOME/out")"
assert_eq "it releases the lock" absent "$([[ -e $ND/lock ]] && echo present || echo absent)"

fresh
nm apt wemby nosuchnode
assert_eq "apt refuses an unknown node" 1 "$?"
assert_eq "it refuses before any SSH call" 0 "$(grep -c '^ssh ' "$STUB_LOG" || true)"
assert_eq "and before opening a window" absent "$(left "$ND/apt-window")"

# apt keeps its own file, so it never closes a reboot cycle's window.
fresh
nm reboot wemby --no-reboot
assert_eq "a cycle is under way" open "$(open_now "$ND/window")"
nm apt manu
assert_eq "apt during a cycle completes" 0 "$?"
assert_eq "the cycle's window survives the apt run" open "$(open_now "$ND/window")"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
((FAIL == 0))
