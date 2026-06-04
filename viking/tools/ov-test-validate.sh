#!/usr/bin/env bash
# Validate the non-prod openviking test stack (agfs:s3 + vectordb:http).
#
# This script:
#   1. Creates the openviking-agfs-test S3 bucket (idempotent Job).
#   2. Waits for openviking-test and ov-vectordb-test to be Ready.
#   3. Drives a 10-doc sequential ingest, then a parallel ingest.
#   4. Runs a 4-way concurrent writer burst and watches for the original
#      failure mode (subtree "resource is busy" rejections in the AGFS
#      log) which is what we're trying to confirm has gone away.
#   5. Runs a search to round-trip AGFS+vectordb.
#   6. Prints pass/fail.
#
# Usage:  ./viking/tools/ov-test-validate.sh
#   (must be run from a context with cluster-admin on the viking namespace)
#
# Env overrides:
#   OV_TEST_URL      default: http://openviking-test.viking.svc.cluster.local:1933
#   OV_TEST_BUCKET   default: openviking-agfs-test
#   OV_TEST_API_KEY  default: from openviking-api-key secret
#   N_DOCS           default: 10
#   PARALLELISM      default: 4

# Force line-buffered stdout so the driver doesn't sit silent while a hang
# masks in-flight curl/port-forward churn.
exec 1> >(stdbuf -oL cat)
exec 2> >(stdbuf -oL cat >&2)
set -euo pipefail

NAMESPACE="${NAMESPACE:-viking}"
OV_TEST_URL="${OV_TEST_URL:-http://openviking-test.viking.svc.cluster.local:1933}"
OV_TEST_BUCKET="${OV_TEST_BUCKET:-openviking-agfs-test}"
N_DOCS="${N_DOCS:-10}"
PARALLELISM="${PARALLELISM:-4}"
# When the host can't reach the in-cluster DNS (e.g. running from a MacBook
# without a kube-dns tail), we fall back to kubectl port-forward and use
# http://127.0.0.1:LOCAL_PORT instead. Set USE_PORT_FORWARD=1 to force this.
USE_PORT_FORWARD="${USE_PORT_FORWARD:-1}"
LOCAL_OV_PORT="${LOCAL_OV_PORT:-19333}"
LOCAL_VD_PORT="${LOCAL_VD_PORT:-15000}"

# Read the API key from the secret if not set.
if [[ -z "${OV_TEST_API_KEY:-}" ]]; then
  OV_TEST_API_KEY=$(kubectl -n "$NAMESPACE" get secret openviking-api-key \
    -o jsonpath='{.data.api-key}' | base64 -d)
fi
: "${OV_TEST_API_KEY:?need OV_TEST_API_KEY or openviking-api-key secret}"

# If we need to port-forward, set up the forwards and override OV_TEST_URL.
if [[ "$USE_PORT_FORWARD" == "1" ]]; then
  echo "Setting up kubectl port-forward (USE_PORT_FORWARD=1)..."
  kubectl -n "$NAMESPACE" port-forward svc/openviking-test "${LOCAL_OV_PORT}:1933" \
    >/tmp/ov-test-pf-ov.log 2>&1 &
  PF_OV_PID=$!
  kubectl -n "$NAMESPACE" port-forward svc/ov-vectordb-test "${LOCAL_VD_PORT}:5000" \
    >/tmp/ov-test-pf-vd.log 2>&1 &
  PF_VD_PID=$!
  trap 'kill $PF_OV_PID $PF_VD_PID 2>/dev/null || true' EXIT
  # Wait for forwards to be live
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    sleep 0.5
    curl -fsS -o /dev/null "http://127.0.0.1:${LOCAL_OV_PORT}/health" 2>/dev/null && \
    curl -fsS -o /dev/null "http://127.0.0.1:${LOCAL_VD_PORT}/health" 2>/dev/null && break
  done
  OV_TEST_URL="http://127.0.0.1:${LOCAL_OV_PORT}"
  VDB_URL="http://127.0.0.1:${LOCAL_VD_PORT}"
else
  VDB_URL="http://ov-vectordb-test.${NAMESPACE}.svc.cluster.local:5000"
fi

# Headers for JSON API calls (mirrors tools/index-homelab.py:json_headers).
hdr_json=(
  -H "X-API-Key: ${OV_TEST_API_KEY}"
  -H "X-OpenViking-Account: default"
  -H "X-OpenViking-User: noot"
  -H "Content-Type: application/json"
)
# Headers for multipart uploads (X-Content-Type omitted; httpx sets it).
hdr_up=(
  -H "X-API-Key: ${OV_TEST_API_KEY}"
  -H "X-OpenViking-Account: default"
  -H "X-OpenViking-User: noot"
)

bar() { printf '\n=== %s ===\n' "$*"; }
ok()  { printf '  \033[32mOK\033[0m  %s\n'   "$*"; }
fail(){ printf '  \033[31mFAIL\033[0m %s\n'  "$*"; FAILED=1; }
info(){ printf '  ... %s\n'                 "$*"; }

FAILED=0

bar "1/6  Create S3 bucket ${OV_TEST_BUCKET} (idempotent)"
"$(dirname "$0")/ov-test-bucket-setup.sh" >/dev/null
ok "bucket ready"

bar "2/6  Wait for pods to be Ready"
kubectl -n "$NAMESPACE" wait --for=condition=ready pod \
  -l app=openviking-test --timeout=180s
kubectl -n "$NAMESPACE" wait --for=condition=ready pod \
  -l app=ov-vectordb-test --timeout=180s
ok "openviking-test + ov-vectordb-test Ready"

bar "3/6  Health endpoints"
ov_health=$(curl -fsS -o /dev/null -w '%{http_code}' "${OV_TEST_URL}/health" || echo 000)
if [[ "$ov_health" == "200" ]]; then ok "OV /health -> 200"; else fail "OV /health -> $ov_health"; fi
vd_health=$(curl -fsS -o /dev/null -w '%{http_code}' "${VDB_URL}/health" || echo 000)
if [[ "$vd_health" == "200" ]]; then ok "vectordb /health -> 200"; else fail "vectordb /health -> $vd_health"; fi

# Helper: write one resource (temp_upload + commit). Echoes the OV URI on success.
write_one() {
  local i="$1" parent="$2"
  local body="hello from test doc ${i} (parent=${parent})"
  local tmp; tmp=$(mktemp)
  echo "$body" > "$tmp"
  local resp
  resp=$(curl -fsS --max-time 30 -X POST "${hdr_up[@]}" \
    -F "file=@${tmp};filename=doc-${i}.txt;type=text/plain" \
    "${OV_TEST_URL}/api/v1/resources/temp_upload")
  rm -f "$tmp"
  local tfid; tfid=$(echo "$resp" | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["temp_file_id"])')
  local uri="${parent}/doc-${i}"
  local r2
  r2=$(curl -fsS --max-time 60 -X POST "${hdr_json[@]}" \
    -d "{\"temp_file_id\":\"${tfid}\",\"to\":\"${uri}\"}" \
    "${OV_TEST_URL}/api/v1/resources")
  local st; st=$(echo "$r2" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status","err"))')
  if [[ "$st" == "ok" ]]; then echo "$uri"; else echo "ERR:$r2" >&2; return 1; fi
}

bar "4/6  Sequential ingest (${N_DOCS} docs)"
seq_start=$(date +%s)
seq_uris=()
for i in $(seq 1 "$N_DOCS"); do
  uri=$(write_one "$i" "viking://resources/test/sibling-${i}")
  seq_uris+=("$uri")
done
seq_end=$(date +%s)
seq_dur=$((seq_end - seq_start))
ok "wrote ${#seq_uris[@]} docs in ${seq_dur}s"

bar "5/6  Parallel ingest (${PARALLELISM} writers)"
# Mark a "lock-burst" point in the pod logs so we can grep.
kubectl -n "$NAMESPACE" logs -l app=openviking-test --tail=1 >/dev/null 2>&1 || true
par_start=$(date +%s)
par_pids=()
for i in $(seq $((N_DOCS+1)) $((N_DOCS*2))); do
  (
    if uri=$(write_one "$i" "viking://resources/test/burst-${i}"); then
      echo "  burst ${i}: ok (${uri})"
    else
      echo "  burst ${i}: FAIL"
    fi
  ) &
  par_pids+=($!)
done
# Wait per-job so a single hang doesn't block the rest of the script.
par_rc=0
for p in "${par_pids[@]}"; do
  wait "$p" || par_rc=$?
done
par_end=$(date +%s)
par_dur=$((par_end - par_start))
if [[ "$par_rc" -eq 0 ]]; then
  ok "parallel ingest done in ${par_dur}s"
else
  fail "parallel ingest had write failures (rc=${par_rc})"
fi

# Look for the original failure mode in the openviking log window.
bar "6/6  Search smoke + lock-rejection scan"
# VLM is sequential (max_concurrent=1 by default), so the async semantic
# queue drains slowly. Wait up to 5 minutes for the queue to catch up to
# at least N_DOCS embeddings before searching.
expected=$((N_DOCS * 2))
echo "  waiting for at least ${expected} embeddings to land in vectordb..."
deadline=$((SECONDS + 300))
total=0
while (( SECONDS < deadline )); do
  resp=$(curl -fsS --max-time 10 -X POST "${hdr_json[@]}" \
    -d '{"query":"hello from test doc","limit":50}' \
    "${OV_TEST_URL}/api/v1/search/search" 2>/dev/null || true)
  total=$(echo "$resp" | python3 -c 'import json,sys
try:
    print(json.load(sys.stdin)["result"]["total"])
except Exception:
    print(0)' 2>/dev/null || echo 0)
  if (( total >= expected )); then
    echo "  embeddings ready after $((SECONDS - (deadline - 300)))s (total=${total})"
    break
  fi
  sleep 5
done
if [[ "$total" -ge "$N_DOCS" ]]; then
  ok "search returned ${total} results (>= ${N_DOCS})"
else
  fail "search returned ${total} results (expected >= ${N_DOCS} after 5 min)"
fi

# Scan the pod logs for the failure mode. We grep for two patterns:
#   - "resource is busy"  (the AGFS subtree lock rejection from before)
#   - "Failed to acquire lock" (lower-level python lock logging)
log_window=$(kubectl -n "$NAMESPACE" logs -l app=openviking-test --tail=2000 --since=10m 2>/dev/null || true)
busy_count=$(echo "$log_window" | grep -c "resource is busy" || true)
lock_count=$(echo "$log_window" | grep -c "Failed to acquire lock" || true)
info "log scan: 'resource is busy' x${busy_count}, 'Failed to acquire lock' x${lock_count}"
if [[ "$busy_count" -eq 0 && "$lock_count" -eq 0 ]]; then
  ok "no AGFS subtree lock rejections in ${N_DOCS} writes"
else
  fail "AGFS subtree lock still serializing (busy=${busy_count}, lock=${lock_count})"
fi

bar "Verdict"
if [[ "$FAILED" -eq 0 ]]; then
  printf '\033[32mPASS\033[0m  agfs:s3 + vectordb:http combo is working end-to-end.\n'
  printf '       sequential %ds, parallel %ds, %d lock rejections.\n' \
    "$seq_dur" "$par_dur" "$busy_count"
else
  printf '\033[31mFAIL\033[0m  See lines above.\n'
  exit 1
fi
