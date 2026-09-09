#!/usr/bin/env bash
# Post-start model preparation for the host Ollama daemon on timmy (IMPR-1075).
#
# Port of the retired pod's startup.sh (llama/ollama-configmap.yaml, PR #48):
# wait for the API, build the edit-prediction tag if it is missing, load it
# with keep_alive=-1, and build (never warm) the agentpair:* tags so they exist
# after every restart. Runs from ollama-warm.service (PartOf=ollama.service), so
# every daemon restart re-runs it; the in-cluster CronJob in
# llama/ollama-jobs.yaml re-asserts the warm every 15 minutes on top of this.
#
# Two independent concerns, kept apart on purpose:
#   1. SERVER readiness is the daemon's own business — this script never gates
#      it, and a failure here leaves the API serving (OV's cloud VLM route
#      through ollama.llama.svc needs only the API, no local runner).
#   2. LOCAL model prep is best-effort with retry, and /run/ollama/models-ready
#      is a separate observable signal for "the edit-prediction tag is warm".
set -u

OLLAMA_URL=${OLLAMA_URL:-http://127.0.0.1:11434}
READY_MARKER=${READY_MARKER:-/run/ollama/models-ready}
FIM_TAG=deepseek-coder-v2:fim
export OLLAMA_HOST=$OLLAMA_URL

log() { printf '%s ollama-warm: %s\n' "$(date -Is)" "$*"; }

wait_for_server() {
	local _tick
	for _tick in $(seq 1 120); do
		if curl -fsS -m 3 "$OLLAMA_URL/api/version" >/dev/null 2>&1; then
			return 0
		fi
		sleep 2
	done
	return 1
}

# Load-only warm: /api/generate with an empty prompt loads the model and
# returns done_reason "load" without generating. (`ollama run … "// warmup"`
# on the base model generated for minutes and held a slot — IDEA-1090.)
warm_load_only() {
	curl -fsS -m 300 -X POST "$OLLAMA_URL/api/generate" \
		-H 'Content-Type: application/json' \
		-d "{\"model\":\"$1\",\"keep_alive\":-1}" >/dev/null
}

create_if_missing() {
	# $1 tag, $2 base model to pull, $3 Modelfile path
	if ollama show "$1" >/dev/null 2>&1; then
		return 0
	fi
	ollama pull "$2" || true
	ollama create "$1" -f "$3" || true
}

prepare_edit_prediction_model() {
	# deepseek-coder-v2:fim is the sole resident model (restored 2026-09-04):
	# Zed (prompt_format "deepseek_coder"), Minuet suffix FIM, remote VSCode
	# FIM (TASK-1156). ~2-3x faster than qwen2.5-coder:14b-base (BUG-1037).
	local attempt mf
	mf=$(mktemp)
	printf '%s\n' \
		'FROM deepseek-coder-v2:16b-lite-base-q4_0' \
		'PARAMETER num_ctx 16384' \
		'PARAMETER temperature 0' \
		'PARAMETER repeat_penalty 1.0' \
		'PARAMETER stop "<|EOT|>"' \
		>"$mf"
	for attempt in 1 2 3 4 5; do
		create_if_missing "$FIM_TAG" deepseek-coder-v2:16b-lite-base-q4_0 "$mf"
		if ollama show "$FIM_TAG" >/dev/null 2>&1; then
			log "warming $FIM_TAG (load-only)"
			if warm_load_only "$FIM_TAG"; then
				touch "$READY_MARKER"
				log "edit-prediction model ready (attempt $attempt)"
				rm -f "$mf"
				return 0
			fi
		fi
		log "WARN: edit-prediction model incomplete (attempt $attempt/5); retrying in 30s"
		sleep 30
	done
	rm -f "$mf"
	log "WARN: edit-prediction model not prepared after 5 attempts; server stays up — check registry/model store"
	return 1
}

prepare_agent_pair() {
	# IDEA-1090 paired tags — BUILD-IF-MISSING ONLY since the 2026-09-04
	# rollback; nothing here is warmed. Re-entering the pair posture means
	# raising OLLAMA_MAX_LOADED_MODELS to 2 in the drop-in AND warming both
	# halves. Modelfiles are the reference copies in llama/ollama/.
	local dir
	dir=${MODELFILE_DIR:-/opt/ollama-host/modelfiles}
	[[ -d $dir ]] || {
		log "WARN: $dir missing; agentpair tags not built"
		return 0
	}
	create_if_missing agentpair:fim qwen2.5-coder:3b-base "$dir/agentpair-fim.Modelfile"
	create_if_missing agentpair:agent qwen3.5:9b-q4_K_M "$dir/agentpair-agent.Modelfile"
	create_if_missing agentpair:agent-gemma4-e4b gemma4:e4b-it-qat "$dir/agentpair-agent-gemma4-e4b.Modelfile"
	create_if_missing agentpair:agent-gemma4-12b gemma4:12b-it-qat "$dir/agentpair-agent-gemma4-12b.Modelfile"
}

mkdir -p "$(dirname "$READY_MARKER")"
rm -f "$READY_MARKER"
log "waiting for $OLLAMA_URL"
if ! wait_for_server; then
	log "ERROR: server did not answer within 240s"
	exit 1
fi
log "server ready ($(curl -fsS -m 3 "$OLLAMA_URL/api/version"))"
# Both preparations run concurrently, as the pod's startup.sh did (`… &`), so
# a cold agentpair build never queues behind the FIM retry loop. Only the
# edit-prediction result decides the unit's exit status.
warm_status=0
prepare_agent_pair &
agent_pair_pid=$!
prepare_edit_prediction_model || warm_status=$?
wait "$agent_pair_pid" || log "WARN: agentpair preparation exited non-zero (tags may be missing)"
exit "$warm_status"
