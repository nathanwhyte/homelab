#!/usr/bin/env bash
# Full performance benchmark for $MODEL on ollama
# Tracks GPU VRAM + system RAM at each step
set -euo pipefail

MODEL="${1:-$MODEL}"
API="http://localhost:11434"

# ── VRAM + Memory helpers ─────────────────────────────────────────
GPU_CARD="/sys/class/drm/card1/device"  # RX 9070 XT (PCI 1002:7550)

vram_used_mb() {
  echo $(( $(cat "$GPU_CARD/mem_info_vram_used") / 1048576 ))
}

vram_total_mb() {
  echo $(( $(cat "$GPU_CARD/mem_info_vram_total") / 1048576 ))
}

sys_used_mb() {
  free -m | awk '/Mem:/{print $3}'
}

sys_avail_mb() {
  free -m | awk '/Mem:/{print $7}'
}

runner_rss_mb() {
  local pid=$(pgrep -f "ollama runner" 2>/dev/null | head -1)
  if [ -n "$pid" ]; then
    echo $(( $(ps -p "$pid" -o rss= 2>/dev/null) / 1024 ))
  else
    echo 0
  fi
}

mem_snapshot() {
  local label="$1"
  printf "  %-22s VRAM: %5dMB / %5dMB  |  SysRAM: %5dMB used, %5dMB avail  |  Runner RSS: %5dMB\n" \
    "$label" "$(vram_used_mb)" "$(vram_total_mb)" "$(sys_used_mb)" "$(sys_avail_mb)" "$(runner_rss_mb)"
}

sep() { printf '%.0s━' {1..80}; echo; }

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  $MODEL — Full Benchmark + VRAM Tracking          ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "Host:   $(hostname) — $(grep 'model name' /proc/cpuinfo | head -1 | cut -d: -f2 | xargs)"
echo "Cores:  $(nproc) logical"
echo "RAM:    $(free -h | awk '/Mem:/{print $2}') total"
echo "GPU:    RX 9070 XT — $(vram_total_mb)MB VRAM"
echo "Ollama: $(ollama --version 2>&1)"
echo ""

#───────────────────────────────────────────────────────────────────
# 0. BASELINE — unload model
#───────────────────────────────────────────────────────────────────
sep
echo "0. BASELINE (model unloaded)"
sep

curl -sf "$API/api/generate" -d '{"model":"'"$MODEL"'","keep_alive":0}' >/dev/null 2>&1
sleep 3
mem_snapshot "model unloaded"
baseline_vram=$(vram_used_mb)
baseline_sys=$(sys_used_mb)
echo ""

#───────────────────────────────────────────────────────────────────
# 1. COLD START
#───────────────────────────────────────────────────────────────────
sep
echo "1. COLD START (model load + first inference)"
sep

cold_start=$(date +%s%N)
response=$(curl -sf "$API/api/generate" -d '{
  "model":"'"$MODEL"'","prompt":"Hi","stream":false,"options":{"num_predict":1}
}')
cold_end=$(date +%s%N)

load_ns=$(echo "$response" | jq -r '.load_duration // 0')
prompt_ns=$(echo "$response" | jq -r '.prompt_eval_duration // 0')
total_ns=$(echo "$response" | jq -r '.total_duration // 0')

printf "  Model load:     %6d ms\n" $((load_ns / 1000000))
printf "  Prompt eval:    %6d ms\n" $((prompt_ns / 1000000))
printf "  Wall clock:     %6d ms\n" $(( (cold_end - cold_start) / 1000000 ))
echo ""
mem_snapshot "after cold start"
loaded_vram=$(vram_used_mb)
loaded_sys=$(sys_used_mb)
printf "\n  VRAM delta from baseline: +%dMB\n" $((loaded_vram - baseline_vram))
printf "  SysRAM delta from baseline: +%dMB\n" $((loaded_sys - baseline_sys))
echo ""

#───────────────────────────────────────────────────────────────────
# 2. PROMPT EVAL SCALING
#───────────────────────────────────────────────────────────────────
sep
echo "2. PROMPT EVAL (prefill) SCALING"
sep

base="The quick brown fox jumps over the lazy dog. "
printf "  %-12s %8s %10s %8s %8s\n" "INPUT~TOKS" "TTFT(ms)" "PREFILL T/s" "VRAM(MB)" "SysRAM+"
printf '  %.0s-' {1..56}; echo

for repeat in 1 5 20 50 100; do
  prompt=""
  for ((r=0; r<repeat; r++)); do prompt+="$base"; done
  prompt+="\\nSummarize the above in one word."
  escaped=$(echo "$prompt" | sed 's/"/\\"/g')

  resp=$(curl -sf "$API/api/generate" -d '{
    "model":"'"$MODEL"'","prompt":"'"$escaped"'",
    "stream":false,"options":{"num_predict":1}
  }')

  p_eval_ns=$(echo "$resp" | jq -r '.prompt_eval_duration // 0')
  p_eval_count=$(echo "$resp" | jq -r '.prompt_eval_count // 0')
  ttft_ms=$((p_eval_ns / 1000000))

  if [ "$p_eval_ns" -gt 0 ] && [ "$p_eval_count" -gt 0 ]; then
    prefill_tps=$(echo "scale=1; $p_eval_count * 1000000000 / $p_eval_ns" | bc)
  else
    prefill_tps="N/A"
  fi

  cur_vram=$(vram_used_mb)
  sys_delta=$(($(sys_used_mb) - baseline_sys))

  printf "  %-12s %8d %10s %8d %+7dMB\n" \
    "~${p_eval_count}" "$ttft_ms" "$prefill_tps" "$cur_vram" "$sys_delta"
done
echo ""

#───────────────────────────────────────────────────────────────────
# 3. GENERATION THROUGHPUT
#───────────────────────────────────────────────────────────────────
sep
echo "3. GENERATION THROUGHPUT vs OUTPUT LENGTH"
sep

printf "  %-10s %8s %8s %8s %8s %8s\n" "NUM_PRED" "TOT(ms)" "TOKENS" "TOK/s" "VRAM(MB)" "SysRAM+"
printf '  %.0s-' {1..58}; echo

for np in 32 64 128 256 512; do
  resp=$(curl -sf "$API/api/generate" -d '{
    "model":"'"$MODEL"'",
    "prompt":"Write a detailed essay about the history of computing.",
    "stream":false,"options":{"num_predict":'"$np"'}
  }')

  eval_ns=$(echo "$resp" | jq -r '.eval_duration // 0')
  eval_count=$(echo "$resp" | jq -r '.eval_count // 0')
  total_ns=$(echo "$resp" | jq -r '.total_duration // 0')
  total_ms=$((total_ns / 1000000))

  if [ "$eval_ns" -gt 0 ] && [ "$eval_count" -gt 0 ]; then
    tps=$(echo "scale=1; $eval_count * 1000000000 / $eval_ns" | bc)
  else
    tps="N/A"
  fi

  cur_vram=$(vram_used_mb)
  sys_delta=$(($(sys_used_mb) - baseline_sys))

  printf "  %-10s %8d %8d %8s %8d %+7dMB\n" \
    "$np" "$total_ms" "$eval_count" "$tps" "$cur_vram" "$sys_delta"
done
echo ""

#───────────────────────────────────────────────────────────────────
# 4. CONCURRENT REQUESTS
#───────────────────────────────────────────────────────────────────
sep
echo "4. CONCURRENT REQUEST THROUGHPUT"
sep

run_concurrent() {
  local n=$1
  local pids=()
  local tmpdir=$(mktemp -d)

  start_ns=$(date +%s%N)
  for ((i=0; i<n; i++)); do
    curl -sf "$API/api/generate" -d '{
      "model":"'"$MODEL"'",
      "prompt":"What is the capital of France? Answer in one word.",
      "stream":false,"options":{"num_predict":16}
    }' > "$tmpdir/resp_$i.json" 2>/dev/null &
    pids+=($!)
  done

  for pid in "${pids[@]}"; do wait "$pid" 2>/dev/null; done
  end_ns=$(date +%s%N)

  wall_ms=$(( (end_ns - start_ns) / 1000000 ))
  total_toks=0
  total_eval_ns=0
  for ((i=0; i<n; i++)); do
    if [ -f "$tmpdir/resp_$i.json" ]; then
      tc=$(jq -r '.eval_count // 0' "$tmpdir/resp_$i.json" 2>/dev/null)
      en=$(jq -r '.eval_duration // 0' "$tmpdir/resp_$i.json" 2>/dev/null)
      total_toks=$((total_toks + tc))
      total_eval_ns=$((total_eval_ns + en))
    fi
  done

  if [ "$wall_ms" -gt 0 ]; then
    agg_tps=$(echo "scale=1; $total_toks * 1000 / $wall_ms" | bc)
  else
    agg_tps="N/A"
  fi

  if [ "$total_eval_ns" -gt 0 ] && [ "$total_toks" -gt 0 ]; then
    per_req_tps=$(echo "scale=1; $total_toks * 1000000000 / $total_eval_ns" | bc)
  else
    per_req_tps="N/A"
  fi

  cur_vram=$(vram_used_mb)
  sys_delta=$(($(sys_used_mb) - baseline_sys))

  printf "  %-6s %8dms %6d toks %8s agg T/s %8s per-req %6dMB VRAM %+5dMB sys\n" \
    "${n}req" "$wall_ms" "$total_toks" "$agg_tps" "$per_req_tps" "$cur_vram" "$sys_delta"

  rm -rf "$tmpdir"
}

printf "  %-6s %10s %10s %12s %12s %10s %8s\n" "CONC" "WALL" "TOKENS" "AGG T/s" "PER-REQ" "VRAM" "SysRAM+"
printf '  %.0s-' {1..72}; echo

for c in 1 2 4 8; do
  run_concurrent "$c"
done
echo ""

#───────────────────────────────────────────────────────────────────
# 5. LARGE CONTEXT — push toward VRAM limits
#───────────────────────────────────────────────────────────────────
sep
echo "5. LARGE CONTEXT STRESS TEST (VRAM pressure)"
sep

printf "  %-12s %8s %10s %8s %8s %10s\n" "CTX~TOKS" "TTFT(ms)" "PREFILL T/s" "VRAM(MB)" "SysRAM+" "Runner RSS"
printf '  %.0s-' {1..66}; echo

for repeat in 200 400 600; do
  prompt=""
  for ((r=0; r<repeat; r++)); do prompt+="$base"; done
  prompt+="\\nSummarize everything above in exactly three words."
  escaped=$(echo "$prompt" | sed 's/"/\\"/g')

  resp=$(curl -sf "$API/api/generate" -d '{
    "model":"'"$MODEL"'","prompt":"'"$escaped"'",
    "stream":false,"options":{"num_predict":32}
  }')

  p_eval_ns=$(echo "$resp" | jq -r '.prompt_eval_duration // 0')
  p_eval_count=$(echo "$resp" | jq -r '.prompt_eval_count // 0')
  ttft_ms=$((p_eval_ns / 1000000))

  if [ "$p_eval_ns" -gt 0 ] && [ "$p_eval_count" -gt 0 ]; then
    prefill_tps=$(echo "scale=1; $p_eval_count * 1000000000 / $p_eval_ns" | bc)
  else
    prefill_tps="N/A"
  fi

  cur_vram=$(vram_used_mb)
  sys_delta=$(($(sys_used_mb) - baseline_sys))
  rss=$(runner_rss_mb)

  printf "  %-12s %8d %10s %8d %+7dMB %8dMB\n" \
    "~${p_eval_count}" "$ttft_ms" "$prefill_tps" "$cur_vram" "$sys_delta" "$rss"
done
echo ""

#───────────────────────────────────────────────────────────────────
# 6. REPEATABILITY
#───────────────────────────────────────────────────────────────────
sep
echo "6. REPEATABILITY (5 identical runs, 128 tokens)"
sep

printf "  %-6s %8s %8s %8s %8s\n" "RUN" "TTFT(ms)" "TOT(ms)" "TOK/s" "VRAM(MB)"
printf '  %.0s-' {1..44}; echo

ttft_sum=0; tps_sum=0
for run in 1 2 3 4 5; do
  resp=$(curl -sf "$API/api/generate" -d '{
    "model":"'"$MODEL"'",
    "prompt":"Explain how a hash table works.",
    "stream":false,"options":{"num_predict":128}
  }')

  p_ns=$(echo "$resp" | jq -r '.prompt_eval_duration // 0')
  e_ns=$(echo "$resp" | jq -r '.eval_duration // 0')
  e_count=$(echo "$resp" | jq -r '.eval_count // 0')
  t_ns=$(echo "$resp" | jq -r '.total_duration // 0')

  ttft=$((p_ns / 1000000))
  total=$((t_ns / 1000000))
  tps=$(echo "scale=1; $e_count * 1000000000 / $e_ns" | bc 2>/dev/null || echo "0")
  cur_vram=$(vram_used_mb)

  ttft_sum=$((ttft_sum + ttft))
  tps_sum=$(echo "$tps_sum + $tps" | bc)

  printf "  %-6s %8d %8d %8s %8d\n" "#$run" "$ttft" "$total" "$tps" "$cur_vram"
done

avg_ttft=$((ttft_sum / 5))
avg_tps=$(echo "scale=1; $tps_sum / 5" | bc)
printf "  %-6s %8d %8s %8s\n" "AVG" "$avg_ttft" "—" "$avg_tps"

echo ""

#───────────────────────────────────────────────────────────────────
# 7. FINAL MEMORY SUMMARY
#───────────────────────────────────────────────────────────────────
sep
echo "7. FINAL MEMORY SUMMARY"
sep

echo "  GPU VRAM:"
printf "    Total:     %6dMB\n" "$(vram_total_mb)"
printf "    Used:      %6dMB\n" "$(vram_used_mb)"
printf "    Free:      %6dMB\n" "$(($(vram_total_mb) - $(vram_used_mb)))"
printf "    Model Δ:   %+5dMB (vs unloaded baseline)\n" "$(($(vram_used_mb) - baseline_vram))"

echo ""
echo "  System RAM:"
free -h | awk 'NR<=2{print "    "$0}'
printf "    Δ from baseline: %+dMB\n" "$(($(sys_used_mb) - baseline_sys))"

echo ""
echo "  Ollama processes:"
for proc in $(pgrep -f "ollama" 2>/dev/null); do
  rss=$(ps -p "$proc" -o rss= 2>/dev/null || true)
  cmd=$(ps -p "$proc" -o args= 2>/dev/null | head -c80 || true)
  if [ -n "$rss" ]; then
    printf "    PID %-8s RSS: %6dMB  %s\n" "$proc" "$((rss/1024))" "$cmd"
  fi
done

echo ""
echo "  /proc runner breakdown:"
runner_pid=$(pgrep -f "ollama runner" 2>/dev/null | head -1)
if [ -n "$runner_pid" ] && [ -f "/proc/$runner_pid/status" ]; then
  grep -E "^(VmRSS|VmSize|VmSwap|VmData|RssAnon|RssFile|RssShmem)" "/proc/$runner_pid/status" | sed 's/^/    /'
fi

echo ""
sep
echo "Benchmark complete."
