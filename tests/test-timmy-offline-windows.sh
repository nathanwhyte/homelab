#!/usr/bin/env bash
# test-timmy-offline-windows.sh — hermetic tests for `timmy-offline.sh windows`:
# an ESP preflight, then down, grub-reboot of the pinned Windows entry, a
# grubenv check, and the reboot, each through ssh (sudo -n for the two root
# commands). No cluster or timmy needed: down is replaced by a recorder and ssh
# by a stub that runs the remote command with a fake sudo, so the argv sudo
# would see (what the sudoers grant matches) is checked exactly.
# Run: bash tests/test-timmy-offline-windows.sh

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
OUTAGE=$REPO_ROOT/scripts/timmy-offline.sh
TMPDIR_TEST=$(mktemp -d)
trap 'rm -rf "$TMPDIR_TEST"' EXIT
export STUB_LOG=$TMPDIR_TEST/calls.log STUB_GRUBENV=$TMPDIR_TEST/grubenv \
	STUB_LOADER=$TMPDIR_TEST/bootmgfw.efi

# ssh [-o opt]... <host> <command>: logs `ssh <host>: <command>`, then runs the
# command in bash with sudo and findmnt replaced. sudo logs `sudo: [arg]...` and
# exits STUB_SUDO_RC (default 0; for /usr/sbin/reboot, STUB_REBOOT_RC, default
# 0). findmnt prints STUB_ESP (default /dev/nvme0n1p1). /boot/grub/grubenv reads
# $STUB_GRUBENV and the Windows loader path is $STUB_LOADER.
# STUB_PREFLIGHT_NO_CONNECT=1 fails the preflight with 255 before running it.
# STUB_GRUB_SSH_EXIT exits the grub-reboot ssh with that status after running
# it (a dropped connection after the write). For the reboot command, ssh exits
# 255 as the dropped connection does (STUB_REBOOT_SSH_EXIT overrides), and
# STUB_REBOOT_NO_CONNECT=1 fails it with 255 before running anything.
cat >"$TMPDIR_TEST/ssh" <<'STUB'
#!/usr/bin/env bash
while [[ $1 == -o ]]; do shift 2; done
host=$1 cmd=$2
echo "ssh $host: $cmd" >>"$STUB_LOG"
no_connect() {
	echo "ssh: connect to host $host port 22: Connection refused" >&2
	exit 255
}
reboot=0 grub=0
[[ $cmd == *'/usr/sbin/reboot'* ]] && reboot=1
[[ $cmd == *'/usr/sbin/grub-reboot'* ]] && grub=1
[[ $cmd == findmnt* && ${STUB_PREFLIGHT_NO_CONNECT:-0} == 1 ]] && no_connect
((reboot)) && [[ ${STUB_REBOOT_NO_CONNECT:-0} == 1 ]] && no_connect
sudo() {
	printf 'sudo:' >>"$STUB_LOG"
	printf ' [%s]' "$@" >>"$STUB_LOG"
	echo >>"$STUB_LOG"
	if [[ $* == *'/usr/sbin/reboot'* ]]; then return "${STUB_REBOOT_RC:-0}"; fi
	return "${STUB_SUDO_RC:-0}"
}
findmnt() { [[ -z ${STUB_ESP-x} ]] || echo "${STUB_ESP-/dev/nvme0n1p1}"; } # STUB_ESP= prints nothing, as for an unmounted path
cmd=${cmd//\/boot\/grub\/grubenv/$STUB_GRUBENV}
cmd=${cmd//\/boot\/efi\/EFI\/Microsoft\/Boot\/bootmgfw.efi/$STUB_LOADER}
eval "$cmd"
rc=$?
((reboot)) && exit "${STUB_REBOOT_SSH_EXIT:-255}"
((grub)) && [[ -n ${STUB_GRUB_SSH_EXIT:-} ]] && exit "$STUB_GRUB_SSH_EXIT"
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
PREFLIGHT="ssh timmy-lan: findmnt -no SOURCE /boot/efi; if test -f /boot/efi/EFI/Microsoft/Boot/bootmgfw.efi; then echo loader-ok; fi"
# The way back every failure after down prints: either target, and up after.
WAY_WINDOWS="either boot Windows: ssh -t timmy-lan 'sudo grub-reboot \"$ENTRY\" && sudo reboot'"
WAY_UBUNTU="or return to Ubuntu: ssh -t timmy-lan 'sudo grub-editenv - unset next_entry && sudo reboot'"
WAY_UP="up needs a reboot since down (it checks the boot ID): run it once timmy is back on Ubuntu."
assert_way_back() { # desc output
	assert_has "$1: the Windows way" "$WAY_WINDOWS" "$2"
	assert_has "$1: the Ubuntu way clears next_entry" "$WAY_UBUNTU" "$2"
	assert_has "$1: up after the reboot" "$WAY_UP" "$2"
}

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
	: >"$STUB_LOADER"
}

set +e # assertions below inspect failures themselves

reset
out=$(run)
calls=$(<"$STUB_LOG")
assert_eq "windows succeeds when the reboot drops the connection" 0 "${out##*$'\n'}"
assert_eq "it runs the ESP preflight, down, grub-reboot, the grubenv check and the reboot, in order" \
	"$PREFLIGHT
down windows
ssh timmy-lan: sudo -n /usr/sbin/grub-reboot Windows\\ Boot\\ Manager\\ \\(on\\ /dev/nvme0n1p1\\)
sudo: [-n] [/usr/sbin/grub-reboot] [$ENTRY]
ssh timmy-lan: test -r /boot/grub/grubenv && { grep '^next_entry=' /boot/grub/grubenv || true; }
ssh timmy-lan: sudo -n /usr/sbin/reboot; echo reboot-rc=\$?
sudo: [-n] [/usr/sbin/reboot]" "$calls"
assert_has "it says next_entry is set to Windows and names up" "timmy is rebooting with next_entry set to Windows. After the Windows session, reboot it back to Ubuntu, then:  " "$out"
assert_has "and what an unmatched title looks like" "If it comes back on Ubuntu without Windows, the title matched no menu entry" "$out"
assert_has "the progress line says the entry is being set, not that it is set" "setting timmy's next boot (once) to: $ENTRY" "$out"

reset
out=$(run STUB_REBOOT_SSH_EXIT=0)
assert_eq "a reboot whose connection closes cleanly also succeeds" 0 "${out##*$'\n'}"

# A2: the preflight checks what the pinned title was built from, before down.
reset
out=$(run STUB_ESP=/dev/nvme1n1p1)
assert_eq "an ESP on another device fails windows" 1 "${out##*$'\n'}"
assert_has "it says the entry may be stale and what timmy reports" "the Windows GRUB entry may be stale, so nothing was changed: '$ENTRY' names /dev/nvme0n1p1 and /boot/efi/EFI/Microsoft/Boot/bootmgfw.efi, but timmy reports /boot/efi on '/dev/nvme1n1p1'." "$out"
assert_has "and how to check the menu" "ssh -t timmy-lan sudo grep menuentry /boot/grub/grub.cfg" "$out"
assert_eq "it stops before down" "$PREFLIGHT" "$(<"$STUB_LOG")"

reset
rm -f "$STUB_LOADER"
out=$(run)
assert_eq "a missing Windows loader fails windows" 1 "${out##*$'\n'}"
assert_has "it says the loader is missing" "reports /boot/efi on '/dev/nvme0n1p1' without that loader" "$out"
assert_eq "it stops before down" "$PREFLIGHT" "$(<"$STUB_LOG")"

# Unmounted: findmnt prints nothing, and the loader is usually gone too (empty
# output); a stray loader on the root filesystem leaves `loader-ok` alone.
for loader in absent present; do
	reset
	[[ $loader == present ]] || rm -f "$STUB_LOADER"
	out=$(run STUB_ESP=)
	assert_eq "an unmounted /boot/efi fails windows (loader $loader)" 1 "${out##*$'\n'}"
	assert_has "it says to mount it, not to edit the entry (loader $loader)" "/boot/efi is not mounted on timmy, so the Windows boot entry cannot be checked; nothing was changed. Mount it (ssh -t timmy-lan sudo mount /boot/efi)" "$out"
	assert_lacks "and does not call the entry stale (loader $loader)" "may be stale" "$out"
	assert_eq "it stops before down (loader $loader)" "$PREFLIGHT" "$(<"$STUB_LOG")"
done

# The comparison is exact: a second findmnt line fails it, and the message
# names every source it saw.
reset
out=$(run STUB_ESP=$'/dev/nvme0n1p1\n/dev/sda1')
assert_eq "extra findmnt output fails windows" 1 "${out##*$'\n'}"
assert_has "it names both sources" "timmy reports /boot/efi on '/dev/nvme0n1p1, /dev/sda1'." "$out"
assert_eq "it stops before down" "$PREFLIGHT" "$(<"$STUB_LOG")"

reset
out=$(run STUB_PREFLIGHT_NO_CONNECT=1)
assert_eq "an unreachable timmy fails the preflight" 1 "${out##*$'\n'}"
assert_has "it says nothing was changed" "cannot reach timmy over ssh to check the Windows boot entry; nothing was changed." "$out"
assert_eq "it stops before down" "$PREFLIGHT" "$(<"$STUB_LOG")"

reset
out=$(run STUB_DOWN_RC=1)
assert_eq "a failed down fails windows" 1 "${out##*$'\n'}"
assert_eq "and only the preflight reached timmy" "$PREFLIGHT
down windows" "$(<"$STUB_LOG")"

# B1: every failure after down ends with the same way back, whatever
# next_entry holds.
reset
out=$(run STUB_SUDO_RC=1)
assert_eq "a grub-reboot sudo refuses fails windows" 1 "${out##*$'\n'}"
assert_has "it says timmy is drained and still on Ubuntu" "not rebooting: it is drained and still on Ubuntu." "$out"
assert_has "it points at the sudoers grant" "GUIDE-1070 § sudoers" "$out"
assert_way_back "grub-reboot refused" "$out"
assert_lacks "it never reboots" "/usr/sbin/reboot;" "$(<"$STUB_LOG")"

# The write lands, then the connection drops: a plain reboot would boot Windows.
reset ""
out=$(run STUB_GRUB_SSH_EXIT=255)
assert_eq "a grub-reboot whose ssh drops after the write fails windows" 1 "${out##*$'\n'}"
assert_eq "the write did land (the stub ran it)" "sudo: [-n] [/usr/sbin/grub-reboot] [$ENTRY]" "$(grep -F 'sudo: [-n] [/usr/sbin/grub-reboot]' "$STUB_LOG")"
assert_way_back "grub-reboot ssh dropped" "$out"
assert_lacks "it never offers a plain reboot into Ubuntu" "reboot into Ubuntu: ssh -t timmy-lan sudo reboot" "$out"

reset "0"
out=$(run)
assert_eq "a next_entry that is not the Windows entry fails windows" 1 "${out##*$'\n'}"
assert_has "it names what grubenv holds" "next boot entry reads 'next_entry=0'" "$out"
assert_has "and how to inspect it" "Inspect: ssh timmy-lan cat /boot/grub/grubenv." "$out"
assert_way_back "next_entry mismatch" "$out"
assert_lacks "it never reboots" "/usr/sbin/reboot;" "$(<"$STUB_LOG")"

# The comparison is exact: a next_entry that merely contains the title fails.
reset "$ENTRY 2"
out=$(run)
assert_eq "a next_entry that only contains the Windows title fails windows" 1 "${out##*$'\n'}"
assert_lacks "it never reboots" "/usr/sbin/reboot;" "$(<"$STUB_LOG")"

reset
printf '# GRUB Environment Block\nsaved_entry=0\n' >"$STUB_GRUBENV"
out=$(run)
assert_eq "a grubenv with no next_entry line fails windows" 1 "${out##*$'\n'}"
assert_has "it says the line is missing, not that the file is unreadable" "next boot entry reads 'no next_entry line'" "$out"
assert_way_back "next_entry missing" "$out"

reset
rm -f "$STUB_GRUBENV"
out=$(run)
assert_eq "an unreadable grubenv fails windows" 1 "${out##*$'\n'}"
assert_has "it says the file cannot be read" "cannot read timmy's /boot/grub/grubenv after grub-reboot" "$out"
assert_way_back "grubenv unreadable" "$out"
assert_lacks "it never reboots" "/usr/sbin/reboot;" "$(<"$STUB_LOG")"

reset
out=$(run STUB_REBOOT_RC=1)
assert_eq "a reboot sudo refuses fails windows" 1 "${out##*$'\n'}"
assert_has "it says the reboot was refused and next_entry is Windows" "reboot refused on timmy (reboot-rc=1); not rebooted: it is drained and still on Ubuntu, with next_entry set to Windows." "$out"
assert_way_back "reboot refused" "$out"

# ssh exits 255 for a connection that never ran the reboot too.
reset
out=$(run STUB_REBOOT_NO_CONNECT=1)
assert_eq "a reboot ssh that never connected fails windows" 1 "${out##*$'\n'}"
assert_has "it says the reboot is unconfirmed" "could not confirm timmy accepted the reboot (ssh exit 255: ssh: connect to host timmy-lan port 22: Connection refused)" "$out"
assert_has "it says how to check and what a reboot that went through boots" "Check whether it is going down: ssh timmy-lan uptime. If it went down, it boots next_entry (Windows, unless the title matches no menu entry)" "$out"
assert_way_back "reboot unconfirmed" "$out"
assert_lacks "it does not claim the reboot started" "timmy is rebooting" "$out"

help=$(bash "$OUTAGE" --help 2>&1)
assert_has "--help lists windows" "timmy-offline.sh windows down, then boot timmy into Windows once" "$help"
assert_has "--help still ends with status" "unhealthy pods" "$help"
# down's closing lines, run for real (cmd_down is stubbed above).
done_plain=$(bash -c 'source "$1"; down_done' _ "$OUTAGE" 2>&1)
done_windows=$(bash -c 'source "$1"; down_done windows' _ "$OUTAGE" 2>&1)
assert_has "down's closing hint names windows" "or boot Windows once:  " "$done_plain"
assert_has "and still gives the power-off command" "ssh -t timmy-lan sudo shutdown -h now" "$done_plain"
assert_has "down called by windows says timmy is drained" "timmy is parked, cordoned and drained." "$done_windows"
assert_lacks "without the power-off hint" "shutdown -h now" "$done_windows"
assert_has "cmd_down passes its argument to down_done" 'down_done "${1:-}"' "$(sed -n '/^cmd_down()/,/^}/p' "$OUTAGE")"
assert_has "cmd_windows calls down with windows" "cmd_down windows" "$(sed -n '/^cmd_windows()/,/^}/p' "$OUTAGE")"

echo
echo "$PASS passed, $FAIL failed"
((FAIL == 0))
