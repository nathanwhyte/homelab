#!/usr/bin/env bash
# homelab-ollama-deploy.sh — the ONE deploy command noot may run as root on
# timmy without a password. Installed to /usr/local/sbin (root:root 0755) by
# install-sudo-deploy.sh; granted by /etc/sudoers.d/homelab-deploy.
#
#   sudo homelab-ollama-deploy.sh [--check | --sync-models | --sync-identity]
#
# WHY A WRAPPER, AND NOT A SUDO RULE ON THE INSTALLER ITSELF.
# install-host-ollama.sh, and everything it copies into /etc/systemd and
# /opt/ollama-host, lives in ~/code/homelab, owned noot:noot. A NOPASSWD rule on
# that path is root for anything that can edit noot's home: change the script,
# or change the drop-in it installs, and the next passwordless run executes it
# as root. That includes every agent session running as noot.
#
# So this deploys ONLY what is on GitHub main. It keeps its own root-owned
# checkout at /opt/homelab-src, resets it to origin/main from the public
# remote, and runs the installer from THERE with REPO_DIR pinned to it. Editing
# ~/code/homelab changes nothing this can see: getting code deployed without a
# password means getting it merged, which is the PR review gate.
#
# The privilege definition — this file and the sudoers fragment — is not
# self-updating. It changes only when a human re-runs install-sudo-deploy.sh
# with their password.
set -euo pipefail

SRC=/opt/homelab-src
REMOTE=https://github.com/nathanwhyte/homelab.git
LOG=/var/log/homelab-deploy.log
INSTALLER_REL=llama/host/install-host-ollama.sh

# Test seams, honoured ONLY when not running as root. Under sudo, env_reset
# strips these anyway; the EUID check is the second lock, so a future
# `Defaults env_keep` change cannot turn a test hook into a root redirect.
if [[ ${HOMELAB_DEPLOY_TEST:-0} == 1 && $EUID -ne 0 ]]; then
	SRC=${TEST_SRC:?} REMOTE=${TEST_REMOTE:?} LOG=${TEST_LOG:?}
elif [[ $EUID -ne 0 ]]; then
	echo "homelab-ollama-deploy: run with sudo" >&2
	exit 1
fi

# Exactly zero or one argument, from the installer's own mode list.
if [[ $# -gt 1 ]]; then
	echo "homelab-ollama-deploy: at most one argument" >&2
	exit 2
fi
mode=${1-}
case $mode in
"" | --check | --sync-models | --sync-identity) ;;
*)
	echo "homelab-ollama-deploy: refusing argument '$mode'" >&2
	exit 2
	;;
esac

# Nothing from the caller's environment steers git or the installer.
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_CONFIG GIT_CONFIG_GLOBAL \
	GIT_CONFIG_SYSTEM GIT_SSH GIT_SSH_COMMAND REPO_DIR

# The checkout must stay root-owned and unwritable by anyone else. If someone
# loosened it, the "only what is on main" guarantee no longer holds: stop.
stat_owner() { stat -c %u "$1" 2>/dev/null || stat -f %u "$1"; }
stat_mode() { stat -c %a "$1" 2>/dev/null || stat -f %Lp "$1"; }
if [[ -e $SRC ]]; then
	if [[ $EUID -eq 0 && $(stat_owner "$SRC") != 0 ]]; then
		echo "homelab-ollama-deploy: $SRC is not root-owned; refusing" >&2
		exit 1
	fi
	if ((8#$(stat_mode "$SRC") & 8#022)); then
		echo "homelab-ollama-deploy: $SRC is group/world-writable; refusing" >&2
		exit 1
	fi
else
	git clone --quiet "$REMOTE" "$SRC"
fi

git -C "$SRC" fetch --quiet origin main
git -C "$SRC" checkout --quiet --detach origin/main
git -C "$SRC" reset --quiet --hard origin/main
git -C "$SRC" clean -fdxq
sha=$(git -C "$SRC" rev-parse --short=12 HEAD)

printf '%s user=%s sha=%s mode=%s\n' "$(date -u +%FT%TZ)" \
	"${SUDO_USER:-$USER}" "$sha" "${mode:-install}" >>"$LOG"
echo "homelab-ollama-deploy: main @ $sha, mode ${mode:-install}"

if [[ -n $mode ]]; then
	REPO_DIR=$SRC exec "$SRC/$INSTALLER_REL" "$mode"
fi
REPO_DIR=$SRC exec "$SRC/$INSTALLER_REL"
