#!/usr/bin/env bash
# test-timmy-offline-windows.sh — hermetic tests for `timmy-offline.sh windows`:
# down first, then grub-reboot of the pinned Windows entry, a grubenv check, and
# the reboot, each through `sudo -n` over ssh. No cluster or timmy needed: down is
# replaced by a recorder and ssh by a stub that runs the remote command with a
# fake sudo, so the argv sudo would see (what the sudoers grant matches) is
# checked exactly. Run: bash tests/test-timmy-offline-windows.sh

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
OUTAGE=$REPO_ROOT/scripts/timmy-offline.sh
TMPDIR_TEST=$(mktemp -d)
trap 'rm -rf "$TMPDIR_TEST"' EXIT
export STUB_LOG=$TMPDIR_TEST/calls.log STUB_GRUBENV=$TMPDIR_TEST/grubenv

# ssh [-o opt]... <host> <command>: logs `ssh <host>: <command>`, then runs the
# command in bash with sudo replaced: sudo logs `sudo: [arg]...` and exits
# STUB_SUDO_RC (default 0; for /usr/sbin/reboot, STUB_REBOOT_RC, default 0).
# /boot/grub/grubenv reads $STUB_GRUBENV. For the reboot command, ssh then
# exits 255 as the dropped connection does (STUB_REBOOT_SSH_EXIT overrides),
# and STUB_REBOOT_NO_CONNECT=1 fails it with 255 before running anything.
cat >"$TMPDIR_TEST/ssh" <<'STUB'
#!/usr/bin/env bash
while [[ $1 == -o ]]; do shift 2; done
host=$1 cmd=$2
echo "ssh $host: $cmd" >>"$STUB_LOG"
reboot=0
[[ $cmd == *'/usr/sbin/reboot'* ]] && reboot=1
if ((reboot)) && [[ ${STUB_REBOOT_NO_CONNECT:-0} == 1 ]]; then
	echo "ssh: connect to host $host port 22: Connection refused" >&2
	exit 255
fi
sudo() {
	printf 'sudo:' >>"$STUB_LOG"
	printf ' [%s]' "$@" >>"$STUB_LOG"
	echo >>"$STUB_LOG"
	if [[ $* == *'/usr/sbin/reboot'* ]]; then return "${STUB_REBOOT_RC:-0}"; fi
	return "${STUB_SUDO_RC:-0}"
}
cmd=${cmd//\/boot\/grub\/grubenv/$STUB_GRUBENV}
eval "$cmd"
rc=$?
((reboot)) && exit "${STUB_REBOOT_SSH_EXIT:-255}"
exit "$rc"
STUB
chmod +x "$TMPDIR_TEST/ssh"
export SSH=$TMPDIR_TEST/ssh

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

ENTRY="Windows Boot Manager (on /dev/nvme0n1p1)"

# run [env...]: `windows` with down recorded (STUB_DOWN_RC fails it), printing
# the output without colour codes, then the exit status on the last line.
run() {
	local out rc=0
	out=$(env "$@" bash -c '
		source "$1"
		cmd_down() { echo "down $*" >>"$STUB_LOG"; return "${STUB_DOWN_RC:-0}"; }
		cmd_windows
	' _ "$OUTAGE" 2>&1) || rc=$?
	printf '%s\n%s\n' "$(sed $'s/\033\\[[0-9;]*m//g' <<<"$out")" "$rc"
}
reset() {
	: >"$STUB_LOG"
	printf '# GRUB Environment Block\nnext_entry=%s\n' "${1-$ENTRY}" >"$STUB_GRUBENV"
}

set +e # assertions below inspect failures themselves

reset
out=$(run)
calls=$(<"$STUB_LOG")
assert_eq "windows succeeds when the reboot drops the connection" 0 "${out##*$'\n'}"
assert_eq "it runs down, grub-reboot, the grubenv check and the reboot, in order" \
	"down windows
ssh timmy-lan: sudo -n /usr/sbin/grub-reboot Windows\\ Boot\\ Manager\\ \\(on\\ /dev/nvme0n1p1\\)
sudo: [-n] [/usr/sbin/grub-reboot] [$ENTRY]
ssh timmy-lan: grep '^next_entry=' /boot/grub/grubenv
ssh timmy-lan: sudo -n /usr/sbin/reboot; echo reboot-rc=\$?
sudo: [-n] [/usr/sbin/reboot]" "$calls"
assert_has "it says Windows is booting and names up" "timmy is rebooting into Windows" "$out"
assert_has "and the command for the return" "timmy-offline.sh up" "$out"
assert_lacks "down's power-off hint is not printed" "shutdown -h now" "$out"

reset
out=$(run STUB_REBOOT_SSH_EXIT=0)
assert_eq "a reboot whose connection closes cleanly also succeeds" 0 "${out##*$'\n'}"

# ssh exits 255 for a connection that never ran the reboot too.
reset
out=$(run STUB_REBOOT_NO_CONNECT=1)
assert_eq "a reboot ssh that never connected fails windows" 1 "${out##*$'\n'}"
assert_has "it says the reboot is unconfirmed" "could not confirm timmy accepted the reboot (ssh exit 255: ssh: connect to host timmy-lan port 22: Connection refused)" "$out"
assert_has "it says the next boot is Windows and how to check" "Its next boot is Windows. Check whether it is going down (ssh timmy-lan uptime)" "$out"
assert_lacks "it does not claim Windows is booting" "timmy is rebooting into Windows" "$out"

reset
out=$(run STUB_DOWN_RC=1)
assert_eq "a failed down fails windows" 1 "${out##*$'\n'}"
assert_lacks "and nothing reaches timmy" "ssh " "$(<"$STUB_LOG")"

reset
out=$(run STUB_SUDO_RC=1)
assert_eq "a grub-reboot sudo refuses fails windows" 1 "${out##*$'\n'}"
assert_has "it says timmy is drained and still on Ubuntu" "it is drained and still on Ubuntu" "$out"
assert_has "it points at the sudoers grant" "GUIDE-1070 § sudoers" "$out"
assert_has "it prints the manual command" "sudo grub-reboot \"$ENTRY\" && sudo reboot" "$out"
assert_lacks "it never reboots" "/usr/sbin/reboot" "$(<"$STUB_LOG")"

reset "0"
out=$(run)
assert_eq "a next_entry that is not the Windows entry fails windows" 1 "${out##*$'\n'}"
assert_has "it names what grubenv holds" "next boot entry reads 'next_entry=0'" "$out"
assert_has "and what to run next" "Inspect with: ssh timmy-lan cat /boot/grub/grubenv" "$out"
assert_lacks "it never reboots" "/usr/sbin/reboot" "$(<"$STUB_LOG")"

reset
rm -f "$STUB_GRUBENV"
out=$(run)
assert_eq "an unreadable grubenv fails windows" 1 "${out##*$'\n'}"
assert_lacks "it never reboots" "/usr/sbin/reboot" "$(<"$STUB_LOG")"

reset
out=$(run STUB_REBOOT_RC=1)
assert_eq "a reboot sudo refuses fails windows" 1 "${out##*$'\n'}"
assert_has "it says the reboot was refused" "reboot refused on timmy (reboot-rc=1)" "$out"
assert_has "it warns the next boot is Windows" "its NEXT boot, whenever it comes, is Windows" "$out"

help=$(bash "$OUTAGE" --help 2>&1)
assert_has "--help lists windows" "timmy-offline.sh windows down, then boot timmy into Windows once" "$help"
assert_has "--help still ends with status" "unhealthy pods" "$help"
down_hint=$(sed -n '/^cmd_down()/,/^}/p' "$OUTAGE")
assert_has "down's closing hint names windows" 'or boot Windows once:  $SELF windows' "$down_hint"

echo
echo "$PASS passed, $FAIL failed"
((FAIL == 0))
