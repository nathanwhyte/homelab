#!/usr/bin/env bash
# Tests for the passwordless deploy path: homelab-ollama-deploy.sh, the
# sudoers fragment, and install-sudo-deploy.sh.
#
# The property under test is one sentence: NOTHING noot can edit reaches root.
# So these assert on what the wrapper actually ran, from where, and with what —
# not only on exit codes. The wrapper runs as a normal user through its test
# seam (honoured only when EUID != 0), against a real throwaway git remote, so
# the fetch/reset/clean behaviour is exercised for real rather than mocked.
#
#   bash test-homelab-ollama-deploy.sh <path to llama/host>
set -u
HOST=${1:?usage: test-homelab-ollama-deploy.sh <path to llama/host>}
HOST=$(cd "$HOST" && pwd)
WRAPPER=$HOST/homelab-ollama-deploy.sh
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
pass=0 fail=0
ok() {
	echo "  PASS  $1"
	pass=$((pass + 1))
}
bad() {
	echo "  FAIL  $1"
	fail=$((fail + 1))
}

# --- a throwaway "GitHub main" whose installer records how it was invoked ----
REMOTE=$TMP/remote.git
git init -q --bare "$REMOTE"
git -C "$REMOTE" symbolic-ref HEAD refs/heads/main
work=$TMP/work
git clone -q "$REMOTE" "$work" 2>/dev/null
mkdir -p "$work/llama/host"
cat >"$work/llama/host/install-host-ollama.sh" <<'EOF'
#!/usr/bin/env bash
printf 'args=[%s] repo_dir=%s rev=%s\n' "$*" "$REPO_DIR" "$(cat "$REPO_DIR/REV")" >>"$CALLS"
EOF
chmod +x "$work/llama/host/install-host-ollama.sh"
echo v1 >"$work/REV"
git -C "$work" add -A
git -C "$work" -c user.email=t@t -c user.name=t commit -qm v1
git -C "$work" push -q origin HEAD:main 2>/dev/null

export CALLS=$TMP/calls.log
SRC=$TMP/src
run() { # runs the wrapper through its test seam; extra env via "$@" before --
	: >"$CALLS"
	env HOMELAB_DEPLOY_TEST=1 TEST_SRC="$SRC" TEST_REMOTE="$REMOTE" \
		TEST_LOG="$TMP/deploy.log" CALLS="$CALLS" "$@" >"$TMP/out" 2>&1
}

echo "wrapper: argument whitelist"
run bash "$WRAPPER"
grep -q 'args=\[\]' "$CALLS" && ok "no argument -> installer runs with none" || bad "no-arg install"
run bash "$WRAPPER" --check
grep -q 'args=\[--check\]' "$CALLS" && ok "--check passed through" || bad "--check"
run bash "$WRAPPER" --sync-models
grep -q 'args=\[--sync-models\]' "$CALLS" && ok "--sync-models passed through" || bad "--sync-models"
run bash "$WRAPPER" --evil
rc=$?
[[ $rc -eq 2 && ! -s $CALLS ]] && ok "unknown argument refused (exit 2), installer NOT run" || bad "unknown arg (rc=$rc)"
run bash "$WRAPPER" --check --check
rc=$?
[[ $rc -eq 2 && ! -s $CALLS ]] && ok "two arguments refused" || bad "two args (rc=$rc)"
run bash "$WRAPPER" "--check; id"
rc=$?
[[ $rc -eq 2 && ! -s $CALLS ]] && ok "shell-metachar argument refused" || bad "metachar arg (rc=$rc)"

echo "wrapper: the caller cannot steer what runs"
run env REPO_DIR=/tmp/evil bash "$WRAPPER"
grep -q "repo_dir=$SRC " "$CALLS" && ok "REPO_DIR from the environment is ignored; pinned to the root checkout" ||
	bad "REPO_DIR override leaked: $(cat "$CALLS")"
echo tampered >"$SRC/REV"
echo 'echo pwned' >"$SRC/llama/host/extra.sh"
run bash "$WRAPPER"
grep -q 'rev=v1' "$CALLS" && ok "local edits in the checkout are reset to origin/main" || bad "tamper survived: $(cat "$CALLS")"
[[ ! -e $SRC/llama/host/extra.sh ]] && ok "untracked files in the checkout are cleaned" || bad "untracked file survived"

echo "wrapper: it deploys whatever main is NOW"
echo v2 >"$work/REV"
git -C "$work" -c user.email=t@t -c user.name=t commit -qam v2
git -C "$work" push -q origin HEAD:main 2>/dev/null
run bash "$WRAPPER"
grep -q 'rev=v2' "$CALLS" && ok "a new commit on main is picked up" || bad "stale deploy: $(cat "$CALLS")"
sha=$(git -C "$work" rev-parse --short=12 HEAD)
grep -q "sha=$sha mode=install" "$TMP/deploy.log" && ok "audit log records the deployed sha" || bad "audit log missing sha $sha"

echo "wrapper: it refuses a loosened checkout"
chmod g+w "$SRC"
run bash "$WRAPPER"
rc=$?
[[ $rc -ne 0 && ! -s $CALLS ]] && ok "group-writable checkout refused, installer NOT run" || bad "loosened checkout accepted (rc=$rc)"
chmod g-w "$SRC"

echo "wrapper: the test seam does not open without its flag"
: >"$CALLS"
env TEST_SRC="$SRC" TEST_REMOTE="$REMOTE" TEST_LOG="$TMP/deploy.log" bash "$WRAPPER" >"$TMP/out" 2>&1
rc=$?
[[ $rc -eq 1 && ! -s $CALLS ]] && grep -q 'run with sudo' "$TMP/out" &&
	ok "without HOMELAB_DEPLOY_TEST a non-root caller is refused" || bad "seam opened without flag (rc=$rc)"

echo "sudoers fragment"
if command -v visudo >/dev/null 2>&1; then
	if visudo -cf "$HOST/homelab-deploy.sudoers" >/dev/null 2>&1; then
		ok "fragment passes visudo -cf"
	else
		bad "fragment FAILS visudo -cf: $(visudo -cf "$HOST/homelab-deploy.sudoers" 2>&1 | head -2)"
	fi
else
	echo "  SKIP  visudo not installed here"
fi
grep -qE '^noot ALL=\(root\) NOPASSWD: HOMELAB_OLLAMA_DEPLOY, HOMELAB_OLLAMA_UNITS$' "$HOST/homelab-deploy.sudoers" &&
	ok "grant is scoped to the two aliases" || bad "grant line changed shape"
# Directives only: the fragment's own comments name SETENV and env_keep while
# explaining their absence, and the first version of this check matched them.
directives=$(grep -vE '^[[:space:]]*#' "$HOST/homelab-deploy.sudoers")
grep -qE 'NOPASSWD: *ALL|SETENV|env_keep' <<<"$directives" &&
	bad "fragment grants ALL, SETENV or env_keep" || ok "no ALL, SETENV or env_keep in any directive"
grep -qE '^[[:space:]]*/usr/local/sbin/homelab-ollama-deploy\.sh[[:space:]]*,' <<<"$directives" &&
	bad "wrapper listed WITHOUT an argument spec (would allow any args)" || ok "every wrapper form pins its arguments"

echo "installer: the traps it exists to prevent"
dst=$(sed -n 's/^SUDOERS_DST=\([^ ]*\).*/\1/p' "$HOST/install-sudo-deploy.sh")
[[ ${dst##*/} != *.* && $dst != *~ ]] && ok "installed sudoers name '${dst##*/}' has no dot (sudo would ignore it)" ||
	bad "installed name '$dst' would be SILENTLY IGNORED by sudo"
out=$(bash "$HOST/install-sudo-deploy.sh" 2>&1)
rc=$?
[[ $rc -ne 0 && $out == *"run with sudo"* ]] && ok "bootstrap refuses to run unprivileged" || bad "bootstrap ran unprivileged (rc=$rc)"
grep -q 'is not root-owned' "$HOST/install-sudo-deploy.sh" && ok "bootstrap refuses a user-owned source tree" ||
	bad "bootstrap lost its source-ownership check"

echo
echo "passed=$pass failed=$fail"
[[ $fail -eq 0 ]]
