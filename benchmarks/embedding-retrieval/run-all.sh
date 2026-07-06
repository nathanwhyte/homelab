#!/usr/bin/env bash
# Overnight full-matrix driver (TASK-1122), run inside the bench runner Job.
# Resilient: every arm is error-isolated (a failure logs + continues) so an
# unattended run finishes as much as possible. Results + this log land on the
# results PVC. Env (set by the Job): BENCH_DB, BENCH_CORPUS, BENCH_GROUND_TRUTH,
# BENCH_RESULTS, and the embedder Service URLs below.
set -u
cd "$(dirname "$0")"
LOG="${BENCH_RESULTS:-results}/run.log"
mkdir -p "$(dirname "$LOG")"
echo "=== overnight matrix start $(date -u +%FT%TZ) ===" | tee -a "$LOG"

QWEN4B_URL="${QWEN4B_URL:-http://embedder-qwen4b.bench.svc:8000/v1/embeddings}"
QWEN06_URL="${QWEN06_URL:-http://embedder-qwen06.bench.svc:8000/v1/embeddings}"
NOMIC_URL="${NOMIC_URL:-http://embedder-nomic.bench.svc:8000/v1/embeddings}"

# DB bootstrap: install the pgvector extension (schema.sql) before any arm —
# the scratch pg comes up bare and benchmark_embedders.py creates vector(...)
# tables that need it. Idempotent; uses the same psycopg the harness pins.
echo "----- schema bootstrap -----" | tee -a "$LOG"
uv run --with "psycopg[binary]>=3.2" python3 -c "
import os, psycopg
with psycopg.connect(os.environ['BENCH_DB']) as c:
    c.execute(open('schema.sql').read())
    c.commit()
print('schema bootstrap ok')" >>"$LOG" 2>&1 || {
	echo "FATAL: schema bootstrap failed" | tee -a "$LOG"
	exit 1
}

arm() { # arm <label> <cmd...>; ARM_TIMEOUT overrides the 90-min default
	local label="$1"
	shift
	echo "----- $label  $(date -u +%T) -----" | tee -a "$LOG"
	if timeout "${ARM_TIMEOUT:-5400}" "$@" >>"$LOG" 2>&1; then
		echo "OK: $label" | tee -a "$LOG"
	else echo "FAILED: $label (rc=$?)" | tee -a "$LOG"; fi
}

# Wait window matches the embedders' startupProbe budget (120 x 10s = 20 min:
# multi-GB model download + ROCm warmup), not an arbitrary 5 min.
wait_url() {
	for _ in $(seq 1 240); do
		code=$(python3 -c "import urllib.request,json,sys;
try:
 urllib.request.urlopen(urllib.request.Request('$1',data=json.dumps({'input':['x']}).encode(),headers={'Content-Type':'application/json'}),timeout=5); print('ok')
except Exception: print('no')" 2>/dev/null)
		[ "$code" = ok ] && return 0
		sleep 5
	done
	echo "WARN: $1 not ready" | tee -a "$LOG"
	return 1
}

# --- served embedder arms (llama.cpp Services) ---
if wait_url "$QWEN4B_URL"; then
	for m in qwen3-4b qwen3-4b-unprefixed qwen3-4b-domain; do
		arm "$m" env EMBED_URL="$QWEN4B_URL" uv run benchmark_embedders.py --model "$m"
	done
fi
if wait_url "$QWEN06_URL"; then
	for m in qwen3-0.6b qwen3-0.6b-unprefixed qwen3-0.6b-domain; do
		arm "$m" env EMBED_URL="$QWEN06_URL" uv run benchmark_embedders.py --model "$m"
	done
fi
# nomic is covered by the in-runner nomic-st-8192 arm below (the fair-context
# upgrade of the already-committed 2048 Mac result) — no served nomic pod needed.

# --- in-runner arms (CPU, no serving) ---
arm "nomic-st-8192" uv run nomic_st.py # fair-context nomic (sentence-transformers, dynamic NTK)
# ColBERT multi-vector over 1097 docs on CPU is the slowest arm by far — give
# it 4h instead of the default 90 min so it can't be the one that times out.
ARM_TIMEOUT=14400 arm "bge-m3-full" uv run benchmark_bge_m3.py --colbert --rrf # dense + sparse + colbert + RRF fusion
# Reranker arm (cross-encoder over top-10) is a Phase-4 follow-up, not in tonight's run.

echo "=== overnight matrix done $(date -u +%FT%TZ) ===" | tee -a "$LOG"
echo "results:" | tee -a "$LOG"
ls -la "${BENCH_RESULTS:-results}" | tee -a "$LOG"
