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
cat >"$TMPDIR_TEST/ssh" <<'STUB'
#!/usr/bin/env bash
cmd=${*: -1}
echo "ssh $cmd" >>"$STUB_LOG"
W=$XDG_STATE_HOME/homelab-node-maintenance/apt-window
[[ -e $XDG_STATE_HOME/apt-window-seen ]] || cat "$W" >"$XDG_STATE_HOME/apt-window-seen" 2>/dev/null || echo absent >"$XDG_STATE_HOME/apt-window-seen"
[[ -z ${STUB_SSH_FAIL:-} ]] || exit 255
B=$XDG_STATE_HOME/bootid
[[ -s $B ]] || echo aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee >"$B"
probe() { printf '%s\n\n__PROBE_OK__\n' "$1"; }
case $cmd in
*setsid*) exit 0 ;;
*"grep -c APT_STARTED"*) probe 1 ;;
*APT_DONE_RC=//p*) probe "${STUB_APT_RC:-0}" ;;
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
chmod +x "$TMPDIR_TEST/kubectl" "$TMPDIR_TEST/ssh"
export PATH=$TMPDIR_TEST:$PATH KUBECTL=$TMPDIR_TEST/kubectl
export REBOOT_TIMEOUT_SECONDS=30 SPIN_DOWN_TIMEOUT_SECONDS=5
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
nm apt wemby
assert_eq "apt completes against the stubs" 0 "$?"
if [[ $(<"$XDG_STATE_HOME/apt-window-seen") =~ ^([0-9]+)\ apt\ upgrade\ on\ wemby$ ]] &&
	((BASH_REMATCH[1] > $(date +%s) + 3600)); then
	ok "the apt window is open, with apt's own bound, while apt runs"
else
	bad "the apt window is open while apt runs (saw '$(<"$XDG_STATE_HOME/apt-window-seen")')"
fi
assert_eq "a finished apt run closes its window" absent "$(left "$ND/apt-window")"

fresh
STUB_APT_RC=100 nm apt wemby
assert_eq "a failed remote apt fails the run" 1 "$?"
assert_eq "the remote run has ended, so its window is closed" absent "$(left "$ND/apt-window")"

# A launch failure (or a timeout, or an interrupt) may leave the detached
# remote upgrade running, so the window is left to its end.
fresh
STUB_SSH_FAIL=1 nm apt wemby
assert_eq "an apt launch failure fails the run" 1 "$?"
assert_eq "it leaves the apt window to its end" open "$(open_now "$ND/apt-window")"

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
