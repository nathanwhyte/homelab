#!/usr/bin/env bash
# FIM TTFT probe (IMPR-1067): measure p50/p95 time-to-first-token against one
# or more OpenAI-compatible FIM endpoints, using minuet's real request shape
# (openai_fim_compatible: prompt=prefix, suffix, max_tokens=64, top_p=0.9,
# stream). Prompts are salted per-request to defeat prefix caching, matching
# minuet's varying-prefix reality. TTFT = curl %{time_starttransfer} on the
# streamed POST (first SSE chunk, includes queue + prefill + network).
#
# Usage:
#   fim-ttft-probe.sh 'label=url=model' ['label=url=model' ...]
# Example:
#   fim-ttft-probe.sh \
#     'timmy-lan=http://192.168.1.19:11434/v1/completions=qwen2.5-coder:fim' \
#     'pop-mlx=http://localhost:11436/v1/completions=mlx-community/Qwen2.5-Coder-3B-4bit'
#
# Env: REPS (default 8), SIZES (default "1024 6144" — prefix chars per case).
set -euo pipefail

REPS="${REPS:-8}"
SIZES="${SIZES:-1024 6144}"

# Representative code prefix material (lua-ish, repeated to size).
BASE_PREFIX='local function backend_available(backend)
  local cmd = string.format("curl -s --max-time 1 -o /dev/null %s", backend.probe or backend.end_point)
  local code = vim.trim(vim.fn.system(cmd))
  return vim.v.shell_error == 0 and code ~= "" and code ~= "000"
end
'
SUFFIX='
return M
'

build_prefix() { # $1 = target char count
	local out=""
	while ((${#out} < $1)); do out+="$BASE_PREFIX"; done
	printf '%s' "${out:0:$1}"
}

percentile() { # $1 = p (e.g. 50), stdin = sorted values
	awk -v p="$1" '{a[NR]=$1} END {
    if (NR==0) {print "n/a"; exit}
    idx = int((p/100)*(NR-1) + 0.5) + 1
    printf "%.3f", a[idx]
  }'
}

for spec in "$@"; do
	IFS='=' read -r label url model <<<"$spec"
	# Warmup (loads model / opens connection); excluded from stats.
	curl -s --max-time 120 -X POST "$url" -H 'Content-Type: application/json' \
		-d "{\"model\":\"$model\",\"prompt\":\"-- warmup $(date +%s%N)\\nlocal x = 1\",\"suffix\":\"\",\"max_tokens\":8,\"temperature\":0,\"stream\":true}" \
		-o /dev/null || {
		echo "$label: warmup FAILED (unreachable?)"
		continue
	}
	for size in $SIZES; do
		prefix_body="$(build_prefix "$size" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')"
		suffix_json="$(printf '%s' "$SUFFIX" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')"
		times=()
		for ((i = 1; i <= REPS; i++)); do
			salt="req-$i-$(date +%s%N)"
			payload="{\"model\":\"$model\",\"prompt\":$(python3 -c "import json,sys; print(json.dumps(\"-- $salt\\n\" + json.loads(sys.argv[1])))" "$prefix_body"),\"suffix\":$suffix_json,\"max_tokens\":64,\"temperature\":0,\"top_p\":0.9,\"stream\":true}"
			t="$(curl -s --max-time 60 -N -X POST "$url" -H 'Content-Type: application/json' \
				-d "$payload" -o /dev/null -w '%{time_starttransfer}')" || t=""
			[[ -n "$t" ]] && times+=("$t")
		done
		if ((${#times[@]} == 0)); then
			echo "$label size=$size: all requests failed"
			continue
		fi
		sorted="$(printf '%s\n' "${times[@]}" | sort -n)"
		p50="$(printf '%s\n' "$sorted" | percentile 50)"
		p95="$(printf '%s\n' "$sorted" | percentile 95)"
		echo "$label size=${size}ch n=${#times[@]}: TTFT p50=${p50}s p95=${p95}s"
	done
done
