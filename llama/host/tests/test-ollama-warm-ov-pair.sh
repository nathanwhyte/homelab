#!/usr/bin/env bash
# Regression tests for the OV/editor pair preparation (IDEA-1105, homelab #130).
#
# Finding this covers: prepare_ov_pair originally used create_if_missing, which
# returns early on ANY existing tag. `qwen2.5-coder:fim` is a name this repo has
# used before at num_ctx 16384, so a host carrying the old tag would silently
# keep 16384 and never receive the 8192 recipe the change exists to deliver.
#
# Mocks `ollama` so no daemon is touched. Asserts on the COMMAND LOG rather than
# only the exit status, because the bug is a silent skip: the old code exits 0
# having done nothing, which no return-code check can distinguish from success.
set -u
SCRIPT=${1:?usage: test-ollama-warm-ov-pair.sh <path to ollama-warm.sh>}
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
export PATH="$TMP/bin:$PATH"
mkdir -p "$TMP/bin" "$TMP/modelfiles"
pass=0 fail=0

# Reference recipes prepare_ov_pair copies verbatim.
printf 'FROM qwen2.5-coder:3b-base\nPARAMETER num_ctx 8192\n' \
	>"$TMP/modelfiles/qwen2.5-coder-fim.Modelfile"
printf 'FROM gemma4:12b-it-qat\nPARAMETER num_ctx 16384\n' \
	>"$TMP/modelfiles/gemma4-vlm.Modelfile"

mock() { # $1 = existing qwen num_ctx ("" = tag missing), $2 = existing gemma num_ctx
	cat >"$TMP/bin/ollama" <<EOF
#!/usr/bin/env bash
QWEN_CTX="$1"; GEMMA_CTX="$2"
echo "\$*" >>"$TMP/calls.log"
case "\$1" in
show)
  tag=\$2
  [[ \$tag == qwen2.5-coder:fim && -z \$QWEN_CTX ]] && exit 1
  [[ \$tag == gemma4:vlm && -z \$GEMMA_CTX ]] && exit 1
  if [[ \${3:-} == --parameters ]]; then
    [[ \$tag == qwen2.5-coder:fim ]] && echo "num_ctx \$QWEN_CTX"
    [[ \$tag == gemma4:vlm ]] && echo "num_ctx \$GEMMA_CTX"
  fi
  exit 0 ;;
create)
  # Rebuild succeeds: flip the recorded ctx so the post-check sees the new value.
  tag=\$2
  [[ \$tag == qwen2.5-coder:fim ]] && QWEN_CTX=8192
  [[ \$tag == gemma4:vlm ]] && GEMMA_CTX=16384
  sed -i.bak "s/^QWEN_CTX=.*/QWEN_CTX=\"\$QWEN_CTX\"/;s/^GEMMA_CTX=.*/GEMMA_CTX=\"\$GEMMA_CTX\"/" "\$0"
  exit 0 ;;
esac
exit 0
EOF
	chmod +x "$TMP/bin/ollama"
	printf '#!/usr/bin/env bash\nexit 0\n' >"$TMP/bin/curl"
	chmod +x "$TMP/bin/curl"
	: >"$TMP/calls.log"
}

run_prepare() {
	# --ov-pair-only reconciles the pair and exits; it warms nothing and never
	# touches RESIDENT_TAG, which is what makes it safe to drive here.
	MODELFILE_DIR="$TMP/modelfiles" OLLAMA_URL="http://127.0.0.1:1" \
		bash "$SCRIPT" --ov-pair-only >>"$TMP/out" 2>&1
}

check() { # $1 label, $2 expected-substring-present(y/n), $3 pattern
	local found=n
	grep -q -- "$3" "$TMP/calls.log" 2>/dev/null && found=y
	if [[ $found == "$2" ]]; then
		echo "  PASS  $1"
		pass=$((pass + 1))
	else
		echo "  FAIL  $1 (expected present=$2, got $found)"
		echo "        calls: $(tr '\n' ';' <"$TMP/calls.log")"
		fail=$((fail + 1))
	fi
}

echo "test 1: stale qwen tag at 16384 is REBUILT, not skipped"
mock 16384 16384
run_prepare
check "rebuilds qwen2.5-coder:fim" y "create qwen2.5-coder:fim"

echo "test 2: tags already at the wanted num_ctx are left alone"
mock 8192 16384
run_prepare
check "no rebuild of qwen2.5-coder:fim" n "create qwen2.5-coder:fim"
check "no rebuild of gemma4:vlm" n "create gemma4:vlm"

echo "test 3: missing tags are created"
mock "" ""
run_prepare
check "creates qwen2.5-coder:fim" y "create qwen2.5-coder:fim"
check "creates gemma4:vlm" y "create gemma4:vlm"

echo "test 4: the two halves reconcile to DIFFERENT contexts"
mock 16384 8192
run_prepare
check "rebuilds qwen (16384 -> 8192)" y "create qwen2.5-coder:fim"
check "rebuilds gemma (8192 -> 16384)" y "create gemma4:vlm"

echo "test 5: the adoption points agree across files"
# The hazard this guards is real and was caught in review: the live host had
# MAX_LOADED_MODELS=2 while the tracked drop-in still said 1, so reapplying the
# repo's own config would have silently restored single-model capacity and
# undone co-residency. Four places encode the posture and must not drift.
REPO=$(cd "$(dirname "$SCRIPT")/../.." && pwd) # llama/host -> llama -> repo root
agree() { # $1 label, $2 file, $3 grep pattern
	if grep -qE -- "$3" "$REPO/$2" 2>/dev/null; then
		echo "  PASS  $1"
		pass=$((pass + 1))
	else
		echo "  FAIL  $1 ($2 does not match /$3/)"
		fail=$((fail + 1))
	fi
}
agree "drop-in allows two resident models" \
	"llama/host/ollama.service.d/homelab.conf" '^Environment="OLLAMA_MAX_LOADED_MODELS=2"'
agree "RESIDENT_TAG defaults to the OV FIM tag" \
	"llama/host/ollama-warm.sh" '^RESIDENT_TAG=\$\{RESIDENT_TAG:-\$OV_FIM_TAG\}'
agree "recovery CronJob re-pins the same tag" \
	"llama/ollama-jobs.yaml" '^ +value: qwen2\.5-coder:fim$'
agree "OV_FIM_TAG is the tag the editors request" \
	"llama/host/ollama-warm.sh" '^OV_FIM_TAG=qwen2\.5-coder:fim$'

echo
echo "passed=$pass failed=$fail"
[[ $fail -eq 0 ]]
