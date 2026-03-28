#!/usr/bin/env bash
# Benchmark devstral-small-2 on ollama: latency + memory spillover
set -euo pipefail

MODEL="devstral-small-2"
API="http://localhost:11434"

# Prompts of increasing length to test scaling
declare -a PROMPTS=(
  "What is 2+2?"
  "Write a Python function that checks if a number is prime. Include docstring and type hints."
  "You are a senior software engineer. Review the following Go code and suggest improvements for performance, readability, and error handling:\n\nfunc fetchData(urls []string) ([][]byte, error) {\n\tvar results [][]byte\n\tfor _, url := range urls {\n\t\tresp, err := http.Get(url)\n\t\tif err != nil {\n\t\t\treturn nil, err\n\t\t}\n\t\tbody, err := io.ReadAll(resp.Body)\n\t\tresp.Body.Close()\n\t\tif err != nil {\n\t\t\treturn nil, err\n\t\t}\n\t\tresults = append(results, body)\n\t}\n\treturn results, nil\n}"
)
declare -a LABELS=("short" "medium" "long")

# Collect baseline memory
baseline_mem=$(free -b | awk '/Mem:/{print $3}')

printf "%-8s %8s %8s %10s %10s %10s %10s\n" \
  "PROMPT" "TTFT(ms)" "TOT(ms)" "TOK/s" "TOKENS" "MEM_DELTA" "RSS(MB)"
printf '%.0s-' {1..76}; echo

for i in "${!PROMPTS[@]}"; do
  prompt="${PROMPTS[$i]}"
  label="${LABELS[$i]}"

  # Flush model from memory and reload fresh
  # (skip flush to measure warm-start; uncomment next line for cold-start)
  # curl -s "$API/api/generate" -d '{"model":"'"$MODEL"'","keep_alive":0}' >/dev/null

  # Time the request, capture eval metrics from ollama response
  start_ns=$(date +%s%N)

  response=$(curl -sf "$API/api/generate" \
    -d '{
      "model": "'"$MODEL"'",
      "prompt": "'"$(echo "$prompt" | sed 's/"/\\"/g; s/\t/\\t/g')"'",
      "stream": false,
      "options": {"num_predict": 256}
    }' 2>&1)

  end_ns=$(date +%s%N)

  # Parse ollama metrics (nanoseconds)
  prompt_eval_ns=$(echo "$response" | jq -r '.prompt_eval_duration // 0')
  eval_ns=$(echo "$response" | jq -r '.eval_duration // 0')
  eval_count=$(echo "$response" | jq -r '.eval_count // 0')
  total_ns=$(echo "$response" | jq -r '.total_duration // 0')

  # Calculate
  ttft_ms=$(( prompt_eval_ns / 1000000 ))
  total_ms=$(( total_ns / 1000000 ))
  if [ "$eval_ns" -gt 0 ] && [ "$eval_count" -gt 0 ]; then
    tok_per_sec=$(echo "scale=1; $eval_count * 1000000000 / $eval_ns" | bc)
  else
    tok_per_sec="N/A"
  fi

  # Memory delta
  current_mem=$(free -b | awk '/Mem:/{print $3}')
  mem_delta_mb=$(echo "scale=1; ($current_mem - $baseline_mem) / 1048576" | bc)

  # Ollama process RSS
  ollama_rss=$(ps -C ollama -o rss= 2>/dev/null | awk '{sum+=$1} END{printf "%.0f", sum/1024}')

  printf "%-8s %8d %8d %10s %10d %8sMB %8sMB\n" \
    "$label" "$ttft_ms" "$total_ms" "$tok_per_sec" "$eval_count" "$mem_delta_mb" "$ollama_rss"
done

echo ""
echo "=== System Memory ==="
free -h
echo ""
echo "=== Ollama Process Memory ==="
ps -C ollama -o pid,rss,vsz,comm --no-headers 2>/dev/null | awk '{printf "PID: %s  RSS: %.0fMB  VSZ: %.0fMB  CMD: %s\n", $1, $2/1024, $3/1024, $4}'

# Also check ollama_runner which actually holds the model
echo ""
echo "=== Ollama Runner Process ==="
pgrep -af "ollama_llama_server|ollama-runner" 2>/dev/null | head -5 || true
ps -C ollama_llama_server -o pid,rss,vsz,comm --no-headers 2>/dev/null | awk '{printf "PID: %s  RSS: %.0fMB  VSZ: %.0fMB  CMD: %s\n", $1, $2/1024, $3/1024, $4}' 2>/dev/null || true
# Try alternate runner name
for proc in $(pgrep -f "runners/" 2>/dev/null); do
  rss=$(ps -p "$proc" -o rss= 2>/dev/null)
  vsz=$(ps -p "$proc" -o vsz= 2>/dev/null)
  cmd=$(ps -p "$proc" -o args= 2>/dev/null | head -c80)
  [ -n "$rss" ] && printf "PID: %s  RSS: %.0fMB  VSZ: %.0fMB  CMD: %s\n" "$proc" "$((rss/1024))" "$((vsz/1024))" "$cmd"
done
