#!/usr/bin/env bash
# Run the full Vulkan backend + higher-bit quant benchmark matrix on timmy.
# Benchmarks run against the live `ollama` deployment (llama/ollama-deployment.yaml),
# which is the production Vulkan backend — the separate -vulkan variant was retired.
#
# The bench requires single-model isolation at NUM_PARALLEL=8 (the CONFIGS below drive
# concurrency through 8 and downstream reports label these as "np=8"). Production now
# runs lower-concurrency edit-prediction settings, so this script PATCHES the deployment
# for the run and RESTORES the original values on exit (via trap), rather than leaving it
# to a manual step. This bounces the prod ollama pod twice (patch + restore) — expect
# brief local-inference downtime around the run. The bench-scripts-vulkan ConfigMap must
# be current (preflighted below before any patch). Override targets with
# BENCH_MAX_LOADED / BENCH_NUM_PARALLEL.
#
# Usage:
#   ./run-vulkan-benchmark-jobs.sh                       run the default matrix
#   ./run-vulkan-benchmark-jobs.sh <config> [<config>…]  run only those configs
#
# Naming a single config matters for one-off model runs: without it the only way
# to benchmark a new model was to add it to CONFIGS, which re-runs the whole
# gemma4 matrix and extends production downtime for no reason. Config names are
# bare stems (no .toml) and must start with `cluster-vulkan` — the Job copies
# them with `cp /scripts/cluster-vulkan*.toml`.
set -euo pipefail

NS="${NS:-llama}"
DEPLOY="${DEPLOY:-ollama}"
SVC="${SVC:-ollama}"
CONFIG_DIR="${CONFIG_DIR:-benchmarks/ollama/configs}"

# Every artifact of a run — logs, env capture, backend proof, coherence
# transcripts, copied results — lands in one dated directory, so a later run
# cannot overwrite an earlier run's fixed-name files (cluster-vulkan-env.json
# and the per-config logs were both previously clobbered in place).
RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
OUTPUT_DIR="${OUTPUT_DIR:-benchmarks/results/vulkan-${RUN_ID}}"

# Coherence gate: a fixed-prompt correctness Job run before each config's
# throughput job. concurrency-bench.py discards response text, so throughput
# alone cannot distinguish correct output from fluent garbage — which is the
# actual risk for hybrid-SSM models on Vulkan (partial SSM_SCAN support).
# Set SKIP_COHERENCE=1 to bypass (records nothing; only for reruns of a config
# whose transcript was already captured). The timeout covers the cold model
# load on the first probe, not just generation.
SKIP_COHERENCE="${SKIP_COHERENCE:-0}"
COHERENCE_TIMEOUT="${COHERENCE_TIMEOUT:-30m}"

# The configuration the benchmark must run under (single model, 8 slots).
BENCH_MAX_LOADED="${BENCH_MAX_LOADED:-1}"
BENCH_NUM_PARALLEL="${BENCH_NUM_PARALLEL:-8}"

# Populated by snapshot_deployment_config; restored by trap on exit.
ORIG_MAX_LOADED=""
ORIG_NUM_PARALLEL=""
CONFIG_PATCHED=0

# Order: baseline default/agentic first, then higher-bit quants.
# 2026-07-01: Q5_K_M and Q6_K model tags are not available in the Unsloth
# HF repo, so the higher-bit quant configs are skipped. Re-enable them once
# the GGUFs are published or converted locally.
CONFIGS=(
	cluster-vulkan-default
	cluster-vulkan-agentic
	# cluster-vulkan-q5km-default
	# cluster-vulkan-q6k-default
	# cluster-vulkan-q5km-agentic
	# cluster-vulkan-q6k-agentic
)

log() {
	echo "[run-vulkan-benchmark-jobs] $*"
}

# Read an env value from the ollama container of the deployment.
deploy_env() {
	kubectl get deployment "$DEPLOY" -n "$NS" -o jsonpath="{.spec.template.spec.containers[?(@.name=='ollama')].env[?(@.name=='$1')].value}"
}

# Capture the live MAX_LOADED_MODELS / NUM_PARALLEL so the trap can restore them.
# Refuse to run if either is unreadable — without a snapshot we cannot restore.
snapshot_deployment_config() {
	ORIG_MAX_LOADED=$(deploy_env OLLAMA_MAX_LOADED_MODELS)
	ORIG_NUM_PARALLEL=$(deploy_env OLLAMA_NUM_PARALLEL)
	if [ -z "$ORIG_MAX_LOADED" ] || [ -z "$ORIG_NUM_PARALLEL" ]; then
		log "ERROR: could not read current OLLAMA_MAX_LOADED_MODELS/OLLAMA_NUM_PARALLEL from deployment/${DEPLOY}; refusing to patch (cannot guarantee restore)"
		exit 1
	fi
	log "snapshot: MAX_LOADED_MODELS=${ORIG_MAX_LOADED} NUM_PARALLEL=${ORIG_NUM_PARALLEL}"
}

# The Job copies its config out of the bench-scripts-vulkan ConfigMap, and
# rebuild-configmap-vulkan.sh enumerates every entry explicitly — a config
# present on disk but missing from the ConfigMap fails only inside the Job,
# after production has already been patched. Verify membership up front,
# before any patch touches the deployment.
preflight_configmap() {
	local keys missing=0 config
	keys=$(kubectl get configmap bench-scripts-vulkan -n "$NS" -o json 2>/dev/null | jq -r '.data | keys[]')
	if [ -z "$keys" ]; then
		log "ERROR: could not read ConfigMap bench-scripts-vulkan in ${NS}; refusing to patch production"
		exit 1
	fi
	for config in "${CONFIGS[@]}"; do
		if ! printf '%s\n' "$keys" | grep -qx "${config}.toml"; then
			log "ERROR: ${config}.toml is not in ConfigMap bench-scripts-vulkan — add it to rebuild-configmap-vulkan.sh, re-run it, and apply the regenerated manifest"
			missing=1
		fi
	done
	[ "$missing" -eq 0 ] || exit 1
	log "preflight OK: all configs present in ConfigMap bench-scripts-vulkan"
}

# `kubectl wait --for=condition=complete` blocks until the full timeout on a
# Job that has already FAILED, holding the patched production config hostage
# for up to 6h after the run is dead. Poll both terminal conditions instead.
wait_job_done() {
	local job="$1" timeout_s="$2" waited=0 status
	while [ "$waited" -lt "$timeout_s" ]; do
		status=$(kubectl get job "$job" -n "$NS" -o jsonpath='{range .status.conditions[?(@.status=="True")]}{.type}{"\n"}{end}' 2>/dev/null || true)
		case "$status" in
		*Complete*) return 0 ;;
		*Failed*)
			log "ERROR: Job ${job} reached Failed"
			return 1
			;;
		esac
		sleep 15
		waited=$((waited + 15))
	done
	log "ERROR: Job ${job} did not reach a terminal state within ${timeout_s}s"
	return 1
}

duration_to_s() {
	case "$1" in
	*h) echo $((${1%h} * 3600)) ;;
	*m) echo $((${1%m} * 60)) ;;
	*s) echo "${1%s}" ;;
	*) echo "$1" ;;
	esac
}

# Patch the deployment to the benchmark config and wait for the rollout.
apply_bench_config() {
	log "patching deployment/${DEPLOY} to benchmark config MAX_LOADED_MODELS=${BENCH_MAX_LOADED} NUM_PARALLEL=${BENCH_NUM_PARALLEL} (bounces the prod pod)"
	kubectl set env "deployment/${DEPLOY}" -n "$NS" \
		"OLLAMA_MAX_LOADED_MODELS=${BENCH_MAX_LOADED}" \
		"OLLAMA_NUM_PARALLEL=${BENCH_NUM_PARALLEL}" >/dev/null
	CONFIG_PATCHED=1
	kubectl rollout status "deployment/${DEPLOY}" -n "$NS" --timeout=360s
}

# EXIT trap: put the deployment back the way we found it. Best-effort — a failed
# restore is logged loudly rather than masking the original exit status.
restore_deployment_config() {
	[ "$CONFIG_PATCHED" -eq 1 ] || return 0
	log "restoring deployment/${DEPLOY} to MAX_LOADED_MODELS=${ORIG_MAX_LOADED} NUM_PARALLEL=${ORIG_NUM_PARALLEL}"
	if kubectl set env "deployment/${DEPLOY}" -n "$NS" \
		"OLLAMA_MAX_LOADED_MODELS=${ORIG_MAX_LOADED}" \
		"OLLAMA_NUM_PARALLEL=${ORIG_NUM_PARALLEL}" >/dev/null; then
		kubectl rollout status "deployment/${DEPLOY}" -n "$NS" --timeout=360s ||
			log "WARN: restore rollout did not confirm — verify deployment/${DEPLOY} is back to ${ORIG_MAX_LOADED}/${ORIG_NUM_PARALLEL}"
	else
		log "WARN: restore of deployment/${DEPLOY} FAILED — manually set MAX_LOADED_MODELS=${ORIG_MAX_LOADED} NUM_PARALLEL=${ORIG_NUM_PARALLEL}"
	fi
}

# Fail-fast: abort unless the live server is exactly the benchmark config with no
# stray second model resident (which MAX_LOADED_MODELS=1 should preclude, but a
# lingering pinned runner would poison the measurement).
preflight_verify() {
	local pod live_ml live_np loaded
	live_ml=$(deploy_env OLLAMA_MAX_LOADED_MODELS)
	live_np=$(deploy_env OLLAMA_NUM_PARALLEL)
	if [ "$live_ml" != "$BENCH_MAX_LOADED" ] || [ "$live_np" != "$BENCH_NUM_PARALLEL" ]; then
		log "ERROR: preflight failed — deployment is ${live_ml}/${live_np}, expected ${BENCH_MAX_LOADED}/${BENCH_NUM_PARALLEL}"
		exit 1
	fi
	pod=$(kubectl get pod -n "$NS" -l "app=${DEPLOY}" -o jsonpath='{.items[0].metadata.name}')
	# ollama ps header is one line; count model rows after it.
	loaded=$(kubectl exec -n "$NS" "$pod" -c ollama -- ollama ps 2>/dev/null | tail -n +2 | grep -c . || true)
	if [ "${loaded:-0}" -gt 1 ]; then
		log "ERROR: preflight failed — ${loaded} models resident, expected <=1 under MAX_LOADED_MODELS=1:"
		kubectl exec -n "$NS" "$pod" -c ollama -- ollama ps 2>/dev/null | sed 's/^/[preflight] /'
		exit 1
	fi
	log "preflight OK: deployment ${live_ml}/${live_np}, ${loaded:-0} model(s) resident"
}

# Pull the artifacts the Jobs wrote to timmy's hostPath into OUTPUT_DIR.
#
# Not `kubectl cp` from the Job pod: cp shells into the container to run tar,
# and `kubectl wait --for=complete` returns only once that pod is Completed and
# its container gone. That copy reported success and produced an empty
# directory (observed 2026-08-11). A short-lived Running pod on the same
# hostPath gives cp something to talk to.
fetch_results() {
	local config="$1"
	local pod=ollama-benchmark-results-fetch
	local marker="${OUTPUT_DIR}/.job-start-${config}"
	mkdir -p "${OUTPUT_DIR}/pod"
	kubectl delete pod "$pod" -n "$NS" --ignore-not-found=true --wait=true >/dev/null 2>&1 || true
	kubectl apply -f benchmarks/ollama/manifests/results-fetch-pod.yaml >/dev/null
	if ! kubectl wait --for=condition=ready "pod/${pod}" -n "$NS" --timeout=120s >/dev/null 2>&1; then
		log "WARN: results-fetch pod never became ready; artifacts remain on timmy at /var/lib/ollama-benchmark/results"
		kubectl delete pod "$pod" -n "$NS" --ignore-not-found=true --wait=false >/dev/null 2>&1 || true
		return 0
	fi
	if ! kubectl cp "${NS}/${pod}:/results" "${OUTPUT_DIR}/pod" >/dev/null 2>&1; then
		log "WARN: copy from the results-fetch pod failed; artifacts remain on timmy at /var/lib/ollama-benchmark/results"
		kubectl delete pod "$pod" -n "$NS" --ignore-not-found=true --wait=false >/dev/null 2>&1 || true
		return 0
	fi
	kubectl delete pod "$pod" -n "$NS" --ignore-not-found=true --wait=false >/dev/null 2>&1 || true
	# cp exit 0 is not proof this row's artifacts arrived: the pod exposes the
	# whole shared hostPath, so a stale tree from an earlier run also copies
	# "successfully". Require at least one file newer than this row's Job
	# launch marker; a name match on the config is checked best-effort on top.
	if [ -f "$marker" ]; then
		if ! find "${OUTPUT_DIR}/pod" -type f -newer "$marker" 2>/dev/null | grep -q .; then
			log "ERROR: copy succeeded but contains nothing newer than the ${config} Job launch — stale tree, treating the row as failed"
			return 1
		fi
		if ! find "${OUTPUT_DIR}/pod" -type f -name "*${config}*" -newer "$marker" 2>/dev/null | grep -q .; then
			log "WARN: fresh artifacts arrived but none name-match ${config} — verify the row's results.json before trusting the bundle"
		fi
	fi
	log "copied results to ${OUTPUT_DIR}/pod"
}

run_config_job() {
	local config="$1"
	local log_file="${OUTPUT_DIR}/cluster-${config}.log"
	log "launching Job for ${config}"

	kubectl delete job ollama-benchmark-vulkan -n "$NS" --ignore-not-found=true
	touch "${OUTPUT_DIR}/.job-start-${config}"
	sed "s/\${CONFIG}/${config}/g" benchmarks/ollama/manifests/benchmark-vulkan-job.yaml |
		kubectl apply -f - -n "$NS"

	log "waiting for Job to reach a terminal state"
	if wait_job_done ollama-benchmark-vulkan 21600; then
		kubectl logs job/ollama-benchmark-vulkan -n "$NS" >"${log_file}" 2>&1
		fetch_results "$config" || return 1
		log "finished ${config}; log at ${log_file}"
	else
		log "ERROR: Job did not complete successfully"
		kubectl logs job/ollama-benchmark-vulkan -n "$NS" >"${log_file}" 2>&1 || true
		kubectl describe job ollama-benchmark-vulkan -n "$NS" >>"${log_file}" 2>&1 || true
		return 1
	fi
}

capture_env() {
	local out="${OUTPUT_DIR}/cluster-vulkan-env.json"
	kubectl get deployment ollama -n "$NS" -o json |
		jq '{
      num_parallel: .spec.template.spec.containers[] | select(.name=="ollama") | .env[] | select(.name=="OLLAMA_NUM_PARALLEL") | .value,
      context_length: .spec.template.spec.containers[] | select(.name=="ollama") | .env[] | select(.name=="OLLAMA_CONTEXT_LENGTH") | .value,
      kv_cache_type: .spec.template.spec.containers[] | select(.name=="ollama") | .env[] | select(.name=="OLLAMA_KV_CACHE_TYPE") | .value,
      keep_alive: .spec.template.spec.containers[] | select(.name=="ollama") | .env[] | select(.name=="OLLAMA_KEEP_ALIVE") | .value,
      load_timeout: .spec.template.spec.containers[] | select(.name=="ollama") | .env[] | select(.name=="OLLAMA_LOAD_TIMEOUT") | .value,
      max_loaded_models: .spec.template.spec.containers[] | select(.name=="ollama") | .env[] | select(.name=="OLLAMA_MAX_LOADED_MODELS") | .value,
      vk_visible_devices: .spec.template.spec.containers[] | select(.name=="ollama") | .env[] | select(.name=="GGML_VK_VISIBLE_DEVICES") | .value,
      image: .spec.template.spec.containers[] | select(.name=="ollama") | .image,
      timestamp: now | todate
    }
    + {
      # The two env vars that actually select the backend. Their absence from
      # the capture is why a Vulkan-vs-ROCm question had to be settled by an
      # interactive grep instead of by the artifacts of the run itself. The
      # "// null" keeps the field present when the deployment does not set it.
      vulkan: (.spec.template.spec.containers[] | select(.name=="ollama") | .env[]? | select(.name=="OLLAMA_VULKAN") | .value) // null,
      llm_library: (.spec.template.spec.containers[] | select(.name=="ollama") | .env[]? | select(.name=="OLLAMA_LLM_LIBRARY") | .value) // null
    }' >"$out"
	log "captured Vulkan env to ${out}"
}

# Persist the runner lines the server logged while loading the model. The env
# vars above state the *intent*; these lines are the evidence that the Vulkan
# path engaged on the discrete GPU and how many layers were offloaded.
capture_backend_proof() {
	local config="$1" pod out
	out="${OUTPUT_DIR}/backend-proof-${config}.log"
	pod=$(kubectl get pod -n "$NS" -l "app=${DEPLOY}" -o jsonpath='{.items[0].metadata.name}')
	if [ -z "$pod" ]; then
		log "WARN: no ${DEPLOY} pod found; skipping backend proof for ${config}"
		return 0
	fi
	kubectl logs -n "$NS" "$pod" -c ollama --tail=4000 2>/dev/null |
		grep -Ei 'ggml_vulkan|vulkan|rocm|hipblas|gfx[0-9]+|library=|offload|Radeon' \
			>"$out" || true
	if [ -s "$out" ]; then
		log "backend proof for ${config} written to ${out}"
	else
		log "WARN: backend proof for ${config} is empty — inspect ${pod} logs manually"
	fi
}

# Pin down exactly which weights were measured. The image tag and env live in
# the env capture; this adds the model's own digest and parameters, so a result
# can still be attributed after a tag is re-pushed or a Modelfile is edited.
capture_model_provenance() {
	local config="$1" model="$2" pod out
	out="${OUTPUT_DIR}/model-provenance-${config}.txt"
	pod=$(kubectl get pod -n "$NS" -l "app=${DEPLOY}" -o jsonpath='{.items[0].metadata.name}')
	if [ -z "$pod" ]; then
		log "WARN: no ${DEPLOY} pod found; skipping model provenance for ${config}"
		return 0
	fi
	{
		echo "=== ollama list ==="
		kubectl exec -n "$NS" "$pod" -c ollama -- ollama list 2>&1 || true
		echo
		echo "=== ollama show ${model} ==="
		kubectl exec -n "$NS" "$pod" -c ollama -- ollama show "$model" 2>&1 || true
		echo
		# The modelfile's FROM line carries the blob digest — the only field
		# that distinguishes two pulls of the same tag.
		echo "=== ollama show --modelfile ==="
		kubectl exec -n "$NS" "$pod" -c ollama -- \
			ollama show "$model" --modelfile 2>&1 || true
	} >"$out"
	log "model provenance for ${config} written to ${out}"
}

# Read a scalar out of a config toml (bare `key = value` at line start).
config_value() {
	local config="$1" key="$2"
	sed -n "s/^${key}[[:space:]]*=[[:space:]]*//p" "${CONFIG_DIR}/${config}.toml" |
		head -n1 | tr -d '"'"'"' '
}

# Fixed-prompt correctness gate, run as an in-cluster Job against
# ollama.llama.svc — the same request path the throughput Job measures, so a
# failure here is a property of the model on this backend rather than of a
# tunnel. Refuses to continue to the throughput job if a probe fails.
run_coherence_gate() {
	local config="$1" model think rc=0 mode
	# A flag string, not an array: it is spliced into the Job manifest by sed
	# and then word-split by the container's shell.
	local think_flags=""
	local job_log="${OUTPUT_DIR}/coherence-${config}.log"
	if [ "$SKIP_COHERENCE" = "1" ]; then
		log "SKIP_COHERENCE=1 — skipping the coherence gate for ${config} (no transcript will be saved)"
		return 0
	fi
	model=$(config_value "$config" model)
	if [ -z "$model" ]; then
		log "ERROR: could not read model from ${CONFIG_DIR}/${config}.toml"
		return 1
	fi
	# By default the gate probes exactly the thinking policy the throughput run
	# will use, so the transcript describes the same thing that was measured.
	# COHERENCE_THINK overrides it with a space-separated list of modes
	# ("false true", "none", "low") — use that to settle a correctness question
	# the benchmarked policy does not exercise, e.g. whether reasoning-on output
	# garbles on a partially supported op.
	think=$(config_value "$config" think)
	if [ -n "${COHERENCE_THINK:-}" ]; then
		for mode in ${COHERENCE_THINK}; do
			think_flags="${think_flags} --think ${mode}"
		done
		think="${COHERENCE_THINK}"
	elif [ -n "$think" ]; then
		think_flags="--think ${think}"
	fi

	# The gate must probe the sampling the throughput run uses. Its built-in
	# defaults (temperature 1.0 / top_p 1.0) failed qwen3.5's arithmetic probe
	# on 2026-08-14 while the benchmarked temperature 0.3 was never exercised.
	# config_value cannot see through inline comments — keep the sampling lines
	# in cluster configs bare (`temperature = 0.3`, no trailing comment).
	local temp topp
	temp=$(config_value "$config" temperature)
	topp=$(config_value "$config" top_p)
	[ -n "$temp" ] && think_flags="${think_flags} --temperature ${temp}"
	[ -n "$topp" ] && think_flags="${think_flags} --top-p ${topp}"

	log "running coherence gate Job for ${config} (model=${model}${think:+, think=${think}}${temp:+, temp=${temp}}${topp:+, top_p=${topp}})"
	kubectl delete job ollama-coherence-smoke -n "$NS" --ignore-not-found=true
	# `|` as the sed delimiter: model tags contain `/` and `:`.
	sed -e "s|\${CONFIG}|${config}|g" \
		-e "s|\${MODEL}|${model}|g" \
		-e "s|\${THINK_ARGS}|${think_flags}|g" \
		benchmarks/ollama/manifests/coherence-smoke-job.yaml |
		kubectl apply -f - -n "$NS"

	set +e
	wait_job_done ollama-coherence-smoke "$(duration_to_s "$COHERENCE_TIMEOUT")"
	rc=$?
	set -e
	kubectl logs job/ollama-coherence-smoke -n "$NS" >"$job_log" 2>&1 || true

	# The transcript is the artifact that matters — recover it whether or not
	# the gate passed. A failing gate's transcript is the evidence of *how* it
	# failed, so losing it would defeat the point of the gate.
	#
	# It comes out of the Job's stdout, not `kubectl cp`: by the time the Job
	# reports complete its pod is Completed, and cp shells into the container,
	# which no longer exists (observed 2026-08-11 — the copy warned and the
	# transcript was only recoverable from timmy's hostPath afterwards).
	sed -n '/=== BEGIN COHERENCE TRANSCRIPT ===/,/=== END COHERENCE TRANSCRIPT ===/p' "$job_log" |
		sed '1d;$d' >"${OUTPUT_DIR}/coherence-${config}.json"
	if [ -s "${OUTPUT_DIR}/coherence-${config}.json" ]; then
		log "coherence transcript written to ${OUTPUT_DIR}/coherence-${config}.json"
	else
		rm -f "${OUTPUT_DIR}/coherence-${config}.json"
		log "WARN: no transcript in the coherence Job log; it is still on timmy at /var/lib/ollama-benchmark/results/coherence-${config}.json"
	fi

	# The model is resident now, so the server has logged its backend selection.
	capture_backend_proof "${config}-load"
	capture_model_provenance "$config" "$model"

	if [ "$rc" -ne 0 ]; then
		log "ERROR: coherence gate FAILED for ${config} — refusing to run the throughput job"
		log "       job log:    ${job_log}"
		log "       transcript: ${OUTPUT_DIR}/coherence-${config}.json"
		return 1
	fi
	log "coherence gate passed for ${config}"
}

main() {
	# Positional args select the configs to run; with none, the default matrix.
	if [ "$#" -gt 0 ]; then
		CONFIGS=("$@")
	fi
	for config in "${CONFIGS[@]}"; do
		if [ ! -f "${CONFIG_DIR}/${config}.toml" ]; then
			log "ERROR: no such config: ${CONFIG_DIR}/${config}.toml"
			exit 1
		fi
		case "$config" in
		cluster-vulkan*) ;;
		*)
			log "ERROR: ${config} does not match the Job's cluster-vulkan*.toml copy glob; rename it"
			exit 1
			;;
		esac
	done

	mkdir -p "${OUTPUT_DIR}"
	log "run ${RUN_ID}: configs=${CONFIGS[*]}; artifacts in ${OUTPUT_DIR}"
	preflight_configmap
	trap restore_deployment_config EXIT
	snapshot_deployment_config
	apply_bench_config
	preflight_verify
	capture_env
	for config in "${CONFIGS[@]}"; do
		# Keep the exact config that produced the numbers next to them; the
		# tracked file can drift before anyone reads the results.
		cp "${CONFIG_DIR}/${config}.toml" "${OUTPUT_DIR}/${config}.toml"
		run_coherence_gate "$config"
		run_config_job "$config"
		capture_backend_proof "$config"
	done
	log "Vulkan matrix complete; results in ${OUTPUT_DIR}"
}

main "$@"
