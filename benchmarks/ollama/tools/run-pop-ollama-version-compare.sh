#!/usr/bin/env bash
# Ollama version A/B comparison on pop (PROJ-1003, TASK-1190).
#
# Runs the same probe set against two Ollama binaries, one at a time, by
# repointing /opt/homebrew/bin/ollama and kickstarting the launchd agent
# between arms. Repointing (rather than standing up a scratch server per arm)
# is deliberate: com.user.ollama-serve owns every OLLAMA_* setting on this
# machine, so reusing it guarantees both arms see a byte-identical server
# environment (NUM_PARALLEL=1, flash attention, q8_0 KV cache, 128K ctx). A
# scratch server would have to re-declare that env by hand, and any drift
# would land silently in the version delta.
#
# ORDERING. Default is AB/BA (each version runs twice, in both positions).
# A single A-then-B pass cannot separate the version effect from run order:
# run-pop-gemma4-authoritative.sh documents 8.3% endpoint drift on a
# BYTE-IDENTICAL repeated config, and that "differences under ~8% are not
# resolvable". With AB/BA the two passes of the same version bracket the run,
# so their spread IS the drift control and is reported next to the delta. Set
# PASSES=1 to opt out, accepting that any result under ~8% is unresolvable.
#
# ⚠ ONE LOCAL-MODEL CONSUMER AT A TIME. Every probe here runs strictly
# serially, and nothing in this script may be parallelized — the global cap
# in claude/CLAUDE.md keys on what the work drives, not what runs it.
# Exclusivity is re-checked before EVERY probe, not once at launch: workbook
# and the hermes cluster can reach this server over the LAN at any time, and
# a multi-hour run gives them plenty of opportunity.
#
# Usage:
#   run-pop-ollama-version-compare.sh <label-a> <prefix-a> [<label-b> <prefix-b>]
# where <prefix-*> is a cmake --install prefix containing bin/ollama.
#
# Example:
#   run-pop-ollama-version-compare.sh \
#     0.32.15   ~/.local/opt/ollama-0.32.15-src \
#     0.33.0-rc1 ~/.local/opt/ollama-0.33.0-rc1
#
# Env:
#   PASSES=2          AB/BA (default). 1 = single pass, no drift control.
#   ARM_COOLDOWN=120  idle seconds between probes and arms.
#   ALLOW_CONTENDED=1 proceed despite an external client (records the breach).

set -euo pipefail

if [[ $# -ne 2 && $# -ne 4 ]]; then
	sed -n '29,39p' "$0" >&2
	exit 2
fi

BENCH_DIR="$HOME/code/homelab/main/benchmarks"
TOOLS="$BENCH_DIR/ollama/tools"
CONFIGS="$BENCH_DIR/ollama/configs"
CORPUS="$BENCH_DIR/ollama/corpus"
SYMLINK="/opt/homebrew/bin/ollama"
AGENT="gui/$(id -u)/com.user.ollama-serve"
URL="http://localhost:11434"
MLX_MODEL="qwen3.8:27b-mlx"
GGUF_MODEL="qwen3.8:27b-q4_K_M"

PASSES="${PASSES:-2}"
ARM_COOLDOWN="${ARM_COOLDOWN:-120}"
ALLOW_CONTENDED="${ALLOW_CONTENDED:-0}"

STAMP="$(date +%Y-%m-%d-%H%M)"
OUT="$BENCH_DIR/results/ollama-version-compare-$STAMP"
mkdir -p "$OUT"

MANIFEST="$OUT/run-manifest.json"
LEDGER="$OUT/completion-ledger.jsonl"
: >"$LEDGER"

ORIGINAL_TARGET="$(readlink "$SYMLINK")"

log() { echo "[$(date +%H:%M:%S)] $*"; }

# ---------------------------------------------------------------------------
# Restoration. The old version only relinked and kickstarted; it never
# confirmed the server actually came back, so a failed restore looked
# identical to a good one.
# ---------------------------------------------------------------------------
restore() {
	local rc=$?
	log "restoring $SYMLINK -> $ORIGINAL_TARGET"
	ln -sfn "$ORIGINAL_TARGET" "$SYMLINK"
	launchctl kickstart -k "$AGENT" || true
	if wait_healthy; then
		local v
		v="$(server_version || echo unknown)"
		log "restored server reports version: $v"
		echo "{\"event\":\"restore\",\"target\":\"$ORIGINAL_TARGET\",\"version\":\"$v\"}" >>"$LEDGER"
	else
		log "ERROR: server did NOT come healthy after restore — check it by hand"
		echo '{"event":"restore","healthy":false}' >>"$LEDGER"
	fi
	exit $rc
}
trap restore EXIT

wait_healthy() {
	for _ in $(seq 1 60); do
		if curl -fsS "$URL/api/version" >/dev/null 2>&1; then return 0; fi
		sleep 2
	done
	return 1
}

server_version() {
	curl -fsS "$URL/api/version" |
		python3 -c 'import json,sys; print(json.load(sys.stdin)["version"])'
}

# ---------------------------------------------------------------------------
# Exclusivity. Any established non-loopback connection to :11434 means another
# machine is driving this GPU; its inference lands in our numbers and evicts
# our model under MAX_LOADED_MODELS=1.
# ---------------------------------------------------------------------------
external_clients() {
	lsof -nP -iTCP:11434 -sTCP:ESTABLISHED 2>/dev/null |
		awk 'NR>1 && $9 !~ /127\.0\.0\.1|\[::1\]/ {print $9}' || true
}

require_exclusive() {
	local probe="$1" peers
	peers="$(external_clients)"
	if [[ -n "$peers" ]]; then
		log "CONTENTION before ${probe}: $(echo "$peers" | tr '\n' ' ')"
		echo "{\"event\":\"contention\",\"probe\":\"$probe\",\"peers\":\"$(echo "$peers" | tr '\n' ' ')\"}" >>"$LEDGER"
		if [[ "$ALLOW_CONTENDED" != "1" ]]; then
			echo "FATAL: external client(s) on :11434 before ${probe}. These numbers would be" >&2
			echo "contaminated and would evict our model. Re-run when idle, or set" >&2
			echo "ALLOW_CONTENDED=1 to proceed and have the breach recorded." >&2
			return 1
		fi
		log "ALLOW_CONTENDED=1 — proceeding with a contaminated window (recorded)"
	fi
}

cool() {
	log "cooldown ${ARM_COOLDOWN}s"
	sleep "$ARM_COOLDOWN"
}

unload() {
	ollama stop "$1" >/dev/null 2>&1 || log "warning: ollama stop $1 returned nonzero"
}

ledger() { # probe, pass, label, status, artifact
	echo "{\"event\":\"probe\",\"probe\":\"$1\",\"pass\":$2,\"arm\":\"$3\",\"status\":\"$4\",\"artifact\":\"$5\"}" >>"$LEDGER"
}

# ---------------------------------------------------------------------------
# Run manifest: everything needed to reproduce or discard this run.
# ---------------------------------------------------------------------------
write_manifest() {
	python3 - "$MANIFEST" "$@" <<'PY'
import hashlib, json, os, subprocess, sys

out = sys.argv[1]
pairs = sys.argv[2:]

def sh(*cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout.strip()
    except Exception as e:
        return f"<error: {e}>"

def sha(p):
    try:
        h = hashlib.sha256()
        with open(p, "rb") as fh:
            for blk in iter(lambda: fh.read(1 << 20), b""):
                h.update(blk)
        return h.hexdigest()
    except OSError as e:
        return f"<error: {e}>"

arms = []
for i in range(0, len(pairs), 2):
    label, prefix = pairs[i], pairs[i + 1]
    binary = os.path.join(prefix, "bin", "ollama")
    arms.append({
        "label": label,
        "prefix": prefix,
        "binary": binary,
        "sha256": sha(binary),
        "reported_version": sh(binary, "--version"),
    })

models = {}
for tag in (os.environ.get("MLX_MODEL", ""), os.environ.get("GGUF_MODEL", "")):
    if tag:
        models[tag] = sh("ollama", "show", "--modelfile", tag)[:400]

json.dump({
    "task": "TASK-1190",
    "arms": arms,
    "server_env": sh("launchctl", "print", f"gui/{os.getuid()}/com.user.ollama-serve"),
    "model_digests": sh("ollama", "list"),
    "model_modelfiles": models,
    "thermal": sh("pmset", "-g", "therm"),
    "uname": sh("uname", "-a"),
    "passes": os.environ.get("PASSES", "2"),
    "arm_cooldown_s": os.environ.get("ARM_COOLDOWN", "120"),
}, open(out, "w"), indent=2)
print(f"wrote {out}")
PY
}

# ---------------------------------------------------------------------------
# One arm = one version, one pass.
# ---------------------------------------------------------------------------
run_arm() {
	local label="$1" prefix="$2" pass="$3"
	local bin="$prefix/bin/ollama"
	local arm_out="$OUT/pass${pass}/${label}"
	mkdir -p "$arm_out"

	echo "=============================================================="
	log "ARM ${label} (pass ${pass}) — ${bin}"
	echo "=============================================================="

	[[ -x "$bin" ]] || {
		echo "FATAL: $bin missing or not executable" >&2
		return 1
	}

	ln -sfn "$bin" "$SYMLINK"
	launchctl kickstart -k "$AGENT"
	wait_healthy || {
		echo "FATAL: server did not come healthy within 120s" >&2
		return 1
	}

	# EXACT version match. Rejecting only "0.0.0" would happily accept a
	# stale server still serving the other arm's build, which silently turns
	# an A/B into an A/A.
	local reported
	reported="$(server_version)"
	echo "$reported" >"$arm_out/reported-version.txt"
	log "server reports version: $reported"
	if [[ "$reported" != "$label" ]]; then
		echo "FATAL: server reports '$reported' but this arm is '$label'." >&2
		echo "Either the binary was built without -DOLLAMA_VERSION or the kickstart did not" >&2
		echo "pick up the new symlink. Refusing to attribute these numbers to '$label'." >&2
		return 1
	fi

	# There is no pyproject.toml in homelab/main, so every harness invocation
	# must carry `--with aiohttp` exactly as run-pop-qwen38-matrix.sh does.
	# `--no-gpu-sampling` matches that runner too: these configs set an empty
	# [prometheus] url, so sampling would have nothing to scrape.

	# --- Probe 1: cancel/resume — the ONLY probe that tests #17901 ----------
	require_exclusive "cancel-resume"
	log "[$label p$pass] prefill cancel/resume"
	if uv run --with aiohttp python "$TOOLS/prefill-cancel-resume.py" \
		--model "$MLX_MODEL" \
		--corpus-dir "$CORPUS" \
		--tier 77k \
		--output "$arm_out/cancel-resume.json" 2>&1 |
		tee "$arm_out/cancel-resume.log"; then
		ledger cancel-resume "$pass" "$label" ok "$arm_out/cancel-resume.json"
	else
		ledger cancel-resume "$pass" "$label" FAILED "$arm_out/cancel-resume.json"
		echo "FATAL: cancel/resume probe failed — see $arm_out/cancel-resume.log" >&2
		return 1
	fi
	unload "$MLX_MODEL"
	cool

	# --- Probe 2: MLX agentic ----------------------------------------------
	require_exclusive "mlx-agentic"
	log "[$label p$pass] MLX agentic"
	uv run --with aiohttp python "$TOOLS/concurrency-bench.py" \
		--config "$CONFIGS/pop-qwen38-27b-mlx-agentic.toml" \
		--output-dir "$arm_out/mlx-agentic" \
		--no-gpu-sampling 2>&1 | tee "$arm_out/mlx-agentic.log"
	gate "$arm_out/mlx-agentic" mlx-agentic "$pass" "$label"
	unload "$MLX_MODEL"
	cool

	# --- Probe 3: GGUF agentic (control; expected flat) ---------------------
	require_exclusive "gguf-agentic"
	log "[$label p$pass] GGUF agentic"
	uv run --with aiohttp python "$TOOLS/concurrency-bench.py" \
		--config "$CONFIGS/pop-qwen38-27b-gguf-agentic.toml" \
		--output-dir "$arm_out/gguf-agentic" \
		--no-gpu-sampling 2>&1 | tee "$arm_out/gguf-agentic.log"
	gate "$arm_out/gguf-agentic" gguf-agentic "$pass" "$label"
	unload "$GGUF_MODEL"
	cool

	# --- Probe 4: prefill warm-prefix ---------------------------------------
	require_exclusive "prefill-warm"
	log "[$label p$pass] prefill warm-prefix (MLX)"
	uv run --with aiohttp python "$TOOLS/prefill-size-breakdown.py" \
		--model "$MLX_MODEL" \
		--corpus-dir "$CORPUS" \
		--mode warm-prefix \
		--output "$arm_out/prefill-warm-prefix.json" 2>&1 |
		tee "$arm_out/prefill-warm-prefix.log"
	ledger prefill-warm "$pass" "$label" ok "$arm_out/prefill-warm-prefix.json"
	unload "$MLX_MODEL"
	cool

	# --- Probe 5: prefill cold ----------------------------------------------
	require_exclusive "prefill-cold"
	log "[$label p$pass] prefill cold (MLX)"
	uv run --with aiohttp python "$TOOLS/prefill-size-breakdown.py" \
		--model "$MLX_MODEL" \
		--corpus-dir "$CORPUS" \
		--mode cold \
		--output "$arm_out/prefill-cold.json" 2>&1 |
		tee "$arm_out/prefill-cold.log"
	ledger prefill-cold "$pass" "$label" ok "$arm_out/prefill-cold.json"
	unload "$MLX_MODEL"

	log "ARM ${label} (pass ${pass}) complete"
}

gate() { # results_dir, probe, pass, label
	local rj="$1/results.json"
	if python3 "$TOOLS/bench-quality-gate.py" "$rj"; then
		ledger "$2" "$3" "$4" ok "$rj"
	else
		ledger "$2" "$3" "$4" QUALITY_FAIL "$rj"
		echo "FATAL: quality gate rejected $rj — the arm's responses are not usable." >&2
		exit 1
	fi
}

cd "$HOME/code/homelab/main"

export MLX_MODEL GGUF_MODEL PASSES ARM_COOLDOWN
write_manifest "$@"
log "original $SYMLINK -> $ORIGINAL_TARGET"
echo "{\"event\":\"start\",\"original\":\"$ORIGINAL_TARGET\",\"passes\":$PASSES}" >>"$LEDGER"

if [[ $# -eq 2 ]]; then
	run_arm "$1" "$2" 1
else
	# Pass 1: A then B. Pass 2: B then A. Each version therefore runs once
	# early and once late, so order is balanced and the same-version spread
	# is a measured drift control rather than an assumption.
	run_arm "$1" "$2" 1
	cool
	run_arm "$3" "$4" 1
	if [[ "$PASSES" -ge 2 ]]; then
		cool
		run_arm "$3" "$4" 2
		cool
		run_arm "$1" "$2" 2
	fi
fi

echo
echo "=============================================================="
echo "Run complete. Results:  $OUT"
echo "Manifest:               $MANIFEST"
echo "Completion ledger:      $LEDGER"
if [[ $# -eq 4 && "$PASSES" -ge 2 ]]; then
	echo
	echo "Compare same-version passes FIRST: their spread is the drift floor."
	echo "A cross-version delta smaller than that spread is not resolvable."
fi
echo "=============================================================="
