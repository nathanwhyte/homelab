#!/usr/bin/env bash
# install-sudo-deploy.sh — one-time, password-gated bootstrap that lets noot
# deploy the ollama host config without a password. Installs:
#
#   /usr/local/sbin/homelab-ollama-deploy.sh   root:root 0755  (the wrapper)
#   /etc/sudoers.d/homelab-deploy              root:root 0440  (the grant)
#
# RUN IT FROM A ROOT-OWNED CHECKOUT OF MAIN, NEVER FROM ~/code/homelab:
#
#   sudo git clone https://github.com/nathanwhyte/homelab.git /opt/homelab-src
#   sudo /opt/homelab-src/llama/host/install-sudo-deploy.sh
#
# and to pick up a later change to the wrapper or the grant:
#
#   sudo git -C /opt/homelab-src pull --ff-only
#   sudo /opt/homelab-src/llama/host/install-sudo-deploy.sh
#
# The script ENFORCES this and refuses to run from a tree that anyone but root
# can write. The whole design rests on noot's editable checkout never being an
# input to anything that runs as root; a bootstrap that installed privileged
# files from ~/code/homelab would reopen exactly that hole on day one.
#
#   install-sudo-deploy.sh             install / refresh
#   install-sudo-deploy.sh --uninstall remove the wrapper and the grant
#   install-sudo-deploy.sh --check     report what is installed; change nothing
#
# SAFETY: a malformed sudoers include can break sudo for everyone, and fixing
# that needs a root console. So the fragment is validated with `visudo -cf`
# BEFORE it goes in place, the full config is re-validated AFTER, and the
# fragment is removed again if that second check fails.
set -euo pipefail

TARGET_USER=noot
WRAPPER_DST=/usr/local/sbin/homelab-ollama-deploy.sh
SUDOERS_DST=/etc/sudoers.d/homelab-deploy # NO DOT: sudo ignores dotted names
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
WRAPPER_SRC=$HERE/homelab-ollama-deploy.sh
SUDOERS_SRC=$HERE/homelab-deploy.sudoers

die() {
	echo "install-sudo-deploy: $*" >&2
	exit 1
}
log() { echo "install-sudo-deploy: $*"; }

stat_owner() { stat -c %u "$1" 2>/dev/null || stat -f %u "$1"; }
stat_mode() { stat -c %a "$1" 2>/dev/null || stat -f %Lp "$1"; }

report() {
	for f in "$WRAPPER_DST" "$SUDOERS_DST"; do
		if [[ -e $f ]]; then
			log "present: $f ($(stat_owner "$f"):$(stat_mode "$f"))"
		else
			log "absent:  $f"
		fi
	done
	if sudo -l -U "$TARGET_USER" 2>/dev/null | grep -qF "$WRAPPER_DST"; then
		log "grant ACTIVE for $TARGET_USER"
	else
		log "grant NOT active for $TARGET_USER"
	fi
}

mode=install
case ${1-} in
"") ;;
--uninstall) mode=uninstall ;;
--check) mode=check ;;
*) die "unknown argument: $1" ;;
esac

[[ $EUID -eq 0 ]] || die "run with sudo"

if [[ $mode == check ]]; then
	report
	exit 0
fi

if [[ $mode == uninstall ]]; then
	rm -f "$SUDOERS_DST" "$WRAPPER_DST"
	visudo -c >/dev/null || die "sudoers no longer validates after removal — fix from a root shell NOW"
	log "removed the wrapper and the grant (left /opt/homelab-src in place)"
	report
	exit 0
fi

# --- the invariant: never install privileged files from a user-writable tree -
for p in "$REPO" "$HERE" "$WRAPPER_SRC" "$SUDOERS_SRC"; do
	[[ -e $p ]] || die "missing $p"
	[[ $(stat_owner "$p") == 0 ]] ||
		die "$p is not root-owned. Run this from a root-owned clone of main (see header), not from ~/code/homelab"
	if ((8#$(stat_mode "$p") & 8#022)); then
		die "$p is group/world-writable; refusing to install privileged files from it"
	fi
done

# --- the grant: validate before it can take effect ---------------------------
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
install -m 0440 "$SUDOERS_SRC" "$tmp"
visudo -cf "$tmp" >/dev/null || die "sudoers fragment failed visudo -cf; nothing installed"

# --- install: wrapper first, so the grant never points at a missing file -----
install -o root -g root -m 0755 "$WRAPPER_SRC" "$WRAPPER_DST"
install -o root -g root -m 0440 "$tmp" "$SUDOERS_DST.new"
mv -f "$SUDOERS_DST.new" "$SUDOERS_DST"

# --- re-validate the WHOLE config; roll back rather than leave sudo broken ---
if ! visudo -c >/dev/null; then
	rm -f "$SUDOERS_DST"
	die "full sudoers config failed validation with the fragment in place; ROLLED BACK"
fi

# --- prove it took: a dotted name, a wrong path or a typo all fail silently --
sudo -l -U "$TARGET_USER" 2>/dev/null | grep -qF "$WRAPPER_DST" ||
	die "installed, but sudo -l -U $TARGET_USER does not list the wrapper — the grant is NOT active"

log "installed from $(git -C "$REPO" rev-parse --short=12 HEAD 2>/dev/null || echo "$REPO")"
report
