#!/usr/bin/env bash
# Regression tests for the two review findings in homelab #127.
# Mocks `ollama` and `curl` so no daemon is touched.
set -u
SCRIPT=${1:?usage: test-warm.sh <path to ollama-warm.sh>}
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
export PATH="$TMP/bin:$PATH"
mkdir -p "$TMP/bin"
pass=0 fail=0

mock() { # $1 = fim num_ctx ("" = missing), $2 = instruct num_ctx, $3 = "block" to hang pulls
	cat >"$TMP/bin/ollama" <<EOF
#!/usr/bin/env bash
FIM_CTX="$1"; INS_CTX="$2"; BLOCK="${3:-}"
case "\$1" in
show)
  tag=\$2
  [[ \$tag == *:fim && -z \$FIM_CTX ]] && exit 1
  [[ \$tag == *:instruct && -z \$INS_CTX ]] && exit 1
  if [[ \${3:-} == --parameters ]]; then
    [[ \$tag == *:fim ]] && echo "num_ctx \$FIM_CTX"
    [[ \$tag == *:instruct ]] && echo "num_ctx \$INS_CTX"
  fi
  exit 0 ;;
create) exit 1 ;;   # rebuilds always FAIL -- the finding-1 scenario
pull)   [[ \$BLOCK == block ]] && sleep 600; exit 0 ;;
esac
exit 0
EOF
	chmod +x "$TMP/bin/ollama"
	printf '#!/usr/bin/env bash\nexit 0\n' >"$TMP/bin/curl"
	chmod +x "$TMP/bin/curl"
}

check() { # $1 label, $2 expected rc, $3 actual rc
	if [[ $2 == "$3" ]]; then
		echo "  PASS  $1 (rc=$3)"
		pass=$((pass + 1))
	else
		echo "  FAIL  $1 (want rc=$2, got $3)"
		fail=$((fail + 1))
	fi
}

echo "== finding 1: --reconcile-only must FAIL when the standby :fim is stuck at 16384 =="
mock 16384 8192
OLLAMA_URL=http://127.0.0.1:1 RESIDENT_TAG=deepseek-coder-v2:instruct \
	bash "$SCRIPT" --reconcile-only >"$TMP/out1" 2>&1
check "stale standby blocks the installer gate" 1 $?

echo "== control: both tags correct -> gate passes =="
mock 8192 8192
OLLAMA_URL=http://127.0.0.1:1 RESIDENT_TAG=deepseek-coder-v2:instruct \
	bash "$SCRIPT" --reconcile-only >"$TMP/out2" 2>&1
check "clean state passes" 0 $?

echo "== finding 2: a hanging standby pull must not delay warming the resident =="
mock 8192 "" block # instruct MISSING -> pull; but instruct is the STANDBY here
start=$(date +%s)
OLLAMA_URL=http://127.0.0.1:1 RESIDENT_TAG=deepseek-coder-v2:fim STANDBY_TIMEOUT=3 \
	timeout 60 bash "$SCRIPT" >"$TMP/out3" 2>&1
rc=$?
elapsed=$(($(date +%s) - start))
echo "  (elapsed ${elapsed}s, rc=$rc)"
if grep -q 'warming deepseek-coder-v2:fim' "$TMP/out3"; then
	echo "  PASS  resident warmed despite hanging standby"
	pass=$((pass + 1))
else
	echo "  FAIL  resident never warmed"
	fail=$((fail + 1))
	sed 's/^/        /' "$TMP/out3" | head -20
fi
if grep -q 'exceeded 3s; terminating' "$TMP/out3"; then
	echo "  PASS  standby bounded by STANDBY_TIMEOUT"
	pass=$((pass + 1))
else
	echo "  FAIL  standby not bounded"
	fail=$((fail + 1))
fi

echo
echo "pass=$pass fail=$fail"
[[ $fail -eq 0 ]]
