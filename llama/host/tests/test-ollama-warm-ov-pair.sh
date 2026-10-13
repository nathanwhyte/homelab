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
printf 'FROM hf.co/unsloth/gemma-4-E4B-it-GGUF:Q8_0\nPARAMETER num_ctx 65536\n' \
	>"$TMP/modelfiles/gemma4-vlm.Modelfile"
# The weights blob the script pins for gemma4:vlm, read from the script so the
# mock cannot drift from it.
PIN=$(grep -oE '^OV_VLM_BASE_SHA256=[0-9a-f]+' "$SCRIPT" | cut -d= -f2)

mock() { # $1 = existing qwen num_ctx ("" = tag missing), $2 = existing gemma num_ctx, $3 = gemma weights blob (default: the pin)
	cat >"$TMP/bin/ollama" <<EOF
#!/usr/bin/env bash
# One assignment per line: the create branch rewrites each with sed, and a
# shared line lost GEMMA_CTX on the first rewrite (masked while the tests only
# asserted on calls.log).
QWEN_CTX="$1"
GEMMA_CTX="$2"
GEMMA_BLOB="${3:-$PIN}"
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
  if [[ \${3:-} == --modelfile && \$tag == gemma4:vlm ]]; then
    echo "FROM /blobs/sha256-\$GEMMA_BLOB"
    echo "FROM /blobs/sha256-projector"
  fi
  exit 0 ;;
create)
  # Rebuild succeeds: record the num_ctx the SUPPLIED recipe bakes (-f file),
  # so a stale recipe directory shows up as the wrong value, the way it would
  # on the host (the installer's pre-restart reconcile, IMPR-1303).
  tag=\$2
  ctx=\$(awk '\$1 == "PARAMETER" && \$2 == "num_ctx" {print \$3}' "\$4")
  [[ \$tag == qwen2.5-coder:fim ]] && QWEN_CTX=\$ctx
  [[ \$tag == gemma4:vlm ]] && GEMMA_CTX=\$ctx
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

run_reconcile() { # $1 recipe directory
	# --reconcile-only is the installer's pre-restart gate: resident + standby
	# must end at their recipes' num_ctx or it exits 1.
	MODELFILE_DIR="$1" OLLAMA_URL="http://127.0.0.1:1" \
		bash "$SCRIPT" --reconcile-only >>"$TMP/out" 2>&1
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
mock 8192 65536
run_prepare
check "no rebuild of qwen2.5-coder:fim" n "create qwen2.5-coder:fim"
check "no rebuild of gemma4:vlm" n "create gemma4:vlm"

echo "test 3: missing tags are created"
mock "" ""
run_prepare
check "creates qwen2.5-coder:fim" y "create qwen2.5-coder:fim"
check "creates gemma4:vlm" y "create gemma4:vlm"

echo "test 4: the two halves reconcile to DIFFERENT contexts"
mock 16384 32768
run_prepare
check "rebuilds qwen (16384 -> 8192)" y "create qwen2.5-coder:fim"
check "rebuilds gemma (32768 -> 65536)" y "create gemma4:vlm"

echo "test 6: the installer's pre-restart reconcile must read the REPO recipes"
# Caught by Codex on homelab#211: install-host-ollama.sh runs --reconcile-only
# before step 3 copies the Modelfiles, so the installed recipe dir still bakes
# the old num_ctx. Reconciling against it rebuilds the old value and the gate
# refuses the restart; against the repo recipes it migrates the tag.
REPO=$(cd "$(dirname "$SCRIPT")/../.." && pwd) # llama/host -> llama -> repo root
mkdir -p "$TMP/stale"
printf 'FROM qwen2.5-coder:3b-base\nPARAMETER num_ctx 8192\n' >"$TMP/stale/qwen2.5-coder-fim.Modelfile"
printf 'FROM gemma4:12b-it-qat\nPARAMETER num_ctx 32768\n' >"$TMP/stale/gemma4-vlm.Modelfile"
mock 8192 32768
if run_reconcile "$TMP/stale"; then
	echo "  FAIL  stale recipe dir: --reconcile-only passed although gemma stays at 32768"
	fail=$((fail + 1))
else
	echo "  PASS  stale recipe dir: --reconcile-only refuses (gemma rebuilt to the stale 32768)"
	pass=$((pass + 1))
fi
mock 8192 32768
if run_reconcile "$REPO/llama/ollama" && [[ $(ollama show gemma4:vlm --parameters | awk '{print $2}') == 65536 ]]; then
	echo "  PASS  repo recipe dir: --reconcile-only migrates gemma 32768 -> 65536 and passes"
	pass=$((pass + 1))
else
	echo "  FAIL  repo recipe dir: --reconcile-only did not migrate gemma to 65536 (got $(ollama show gemma4:vlm --parameters))"
	fail=$((fail + 1))
fi
if grep -qE 'MODELFILE_DIR="\$REPO_DIR/llama/ollama" \\$' "$REPO/llama/host/install-host-ollama.sh"; then
	echo "  PASS  installer passes the repo recipe dir to the pre-restart reconcile"
	pass=$((pass + 1))
else
	echo "  FAIL  installer does not pass MODELFILE_DIR=\$REPO_DIR/llama/ollama to --reconcile-only"
	fail=$((fail + 1))
fi

echo "test 7: the gemma4:vlm weights blob is pinned (hf.co base is a moving name)"
# Unsloth re-uploads quants under the same file name, so a fresh host's pull
# can land different weights with no log line. The pre-restart gate refuses a
# tag whose first FROM blob is not the pinned sha256; the Modelfile's PIN block
# must carry the same value.
mock 8192 65536 0000000000000000000000000000000000000000000000000000000000000000
if run_reconcile "$REPO/llama/ollama"; then
	echo "  FAIL  mismatched weights blob: --reconcile-only passed"
	fail=$((fail + 1))
else
	echo "  PASS  mismatched weights blob: --reconcile-only refuses"
	pass=$((pass + 1))
fi
mock 8192 65536
if run_reconcile "$REPO/llama/ollama"; then
	echo "  PASS  pinned weights blob: --reconcile-only passes"
	pass=$((pass + 1))
else
	echo "  FAIL  pinned weights blob: --reconcile-only refused"
	fail=$((fail + 1))
fi
if [[ -n $PIN ]] && grep -q "sha256 $PIN" "$REPO/llama/ollama/gemma4-vlm.Modelfile"; then
	echo "  PASS  Modelfile PIN block carries the script's pinned sha256"
	pass=$((pass + 1))
else
	echo "  FAIL  Modelfile PIN block does not carry OV_VLM_BASE_SHA256 ($PIN)"
	fail=$((fail + 1))
fi

echo "test 5: the adoption points agree across files"
# The hazard this guards is real and was caught in review: the live host had
# MAX_LOADED_MODELS=2 while the tracked drop-in still said 1, so reapplying the
# repo's own config would have silently restored single-model capacity and
# undone co-residency. Since 2026-10-13 (IMPR-1303) the posture is the reverse
# — gemma4:vlm sole resident, MAX_LOADED_MODELS=1 — and the same three places
# must agree on it.
REPO=$(cd "$(dirname "$SCRIPT")/../.." && pwd) # llama/host -> llama -> repo root
agree() {                                      # $1 label, $2 file, $3 grep pattern
	if grep -qE -- "$3" "$REPO/$2" 2>/dev/null; then
		echo "  PASS  $1"
		pass=$((pass + 1))
	else
		echo "  FAIL  $1 ($2 does not match /$3/)"
		fail=$((fail + 1))
	fi
}
agree "drop-in pins one resident model" \
	"llama/host/ollama.service.d/homelab.conf" '^Environment="OLLAMA_MAX_LOADED_MODELS=1"'
agree "RESIDENT_TAG defaults to the OV VLM tag" \
	"llama/host/ollama-warm.sh" '^RESIDENT_TAG=\$\{RESIDENT_TAG:-\$OV_VLM_TAG\}'
agree "recovery CronJob re-pins the same tag" \
	"llama/ollama-jobs.yaml" '^ +value: gemma4:vlm$'
agree "OV_VLM_TAG is the tag OpenViking requests" \
	"llama/host/ollama-warm.sh" '^OV_VLM_TAG=gemma4:vlm$'

# Added 2026-09-21 (BUG-1155). reconcile_tag compares the live tag against
# OV_VLM_NUM_CTX, not against the Modelfile, so raising num_ctx in the recipe
# alone makes every warm run rebuild the tag, fail the post-check and return 1
# — silently, on a path nothing else asserts. This pair must move together.
want_const=$(sed -n 's/^OV_VLM_NUM_CTX=\([0-9]*\).*/\1/p' "$REPO/llama/host/ollama-warm.sh")
want_recipe=$(awk '$1 == "PARAMETER" && $2 == "num_ctx" {print $3}' \
	"$REPO/llama/ollama/gemma4-vlm.Modelfile")
if [[ -n $want_const && $want_const == "$want_recipe" ]]; then
	echo "  PASS  gemma4:vlm num_ctx agrees: recipe $want_recipe == OV_VLM_NUM_CTX $want_const"
	pass=$((pass + 1))
else
	echo "  FAIL  gemma4:vlm num_ctx DRIFT: recipe ${want_recipe:-unset} vs OV_VLM_NUM_CTX ${want_const:-unset}"
	fail=$((fail + 1))
fi

echo
echo "passed=$pass failed=$fail"
[[ $fail -eq 0 ]]
