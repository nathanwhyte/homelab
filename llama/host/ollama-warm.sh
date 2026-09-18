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
#
#   ollama-warm.sh                   wait, reconcile + warm the FIM tag, build agentpair tags
#   ollama-warm.sh --reconcile-only  wait, rebuild the FIM tag if its num_ctx drifted
#                                    from the recipe, load nothing; exit 1 if it
#                                    cannot be made to match (install-host-ollama.sh
#                                    runs this BEFORE restarting into the new drop-in)
set -u

OLLAMA_URL=${OLLAMA_URL:-http://127.0.0.1:11434}
READY_MARKER=${READY_MARKER:-/run/ollama/models-ready}
FIM_TAG=deepseek-coder-v2:fim
FIM_BASE=deepseek-coder-v2:16b-lite-base-q4_0
INSTRUCT_TAG=deepseek-coder-v2:instruct
INSTRUCT_BASE=deepseek-coder-v2:16b-lite-instruct-q4_0
# num_ctx 8192 pairs with OLLAMA_NUM_PARALLEL=4 (4.5 GiB KV); at 16384 the
# runner would need 9 GiB of KV and would not fit the card (IDEA-1105). Keep in
# step with the drop-in: changing either one alone breaks the VRAM budget.
FIM_NUM_CTX=8192
# Which tag stays pinned for edit prediction. :instruct since 2026-09-18 — both
# tags decode at the same speed (170 vs 160 tok/s, identical TTFT) and measure
# the same through the real minuet stack (28% vs 30% empty after filtering,
# comparable lengths); it was adopted on the quality of its completions, judged
# in the editor. dotfiles nvim/lua/plugins/minuet.lua and zed/settings.json name
# this tag, and OLLAMA_MAX_LOADED_MODELS=1 means a request for the OTHER tag
# evicts this one and pays a ~5 s reload -- so change all three together.
# Both tags are reconciled below, so rolling back is: set this to $FIM_TAG,
# re-run, and flip the two dotfiles entries.
RESIDENT_TAG=${RESIDENT_TAG:-$INSTRUCT_TAG}
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

tag_num_ctx() {
	# Prints the tag's baked num_ctx, or nothing when the tag is missing/unset.
	ollama show "$1" --parameters 2>/dev/null | awk '$1 == "num_ctx" {print $2}'
}

write_fim_modelfile() {
	printf '%s\n' \
		"FROM $FIM_BASE" \
		"PARAMETER num_ctx $FIM_NUM_CTX" \
		'PARAMETER temperature 0' \
		'PARAMETER repeat_penalty 1.0' \
		'PARAMETER stop "<|EOT|>"' \
		>"$1"
}

write_instruct_modelfile() {
	# Template and stops come from the library instruct tag; only num_ctx is
	# baked. A tag WITHOUT a baked num_ctx inherits OLLAMA_CONTEXT_LENGTH
	# (131072 here), projects ~36 GiB of KV, spills to host memory and gets the
	# daemon OOM-killed by the cgroup ceiling -- that is how the first instruct
	# load failed on 2026-09-16 (IDEA-1105). Every tag served here bakes it.
	printf '%s\n' \
		"FROM $INSTRUCT_BASE" \
		"PARAMETER num_ctx $FIM_NUM_CTX" \
		>"$1"
}

reconcile_tag() {
	# $1 tag, $2 base, $3 modelfile writer. Build when missing, and REBUILD when
	# an existing tag's num_ctx differs from the recipe: create_if_missing alone
	# would keep a 16384 tag from the 2-slot posture, and 16384 x 4 slots
	# overflows the card. Loads nothing. Non-zero unless the tag ends at
	# FIM_NUM_CTX.
	local tag=$1 base=$2 writer=$3 mf current
	current=$(tag_num_ctx "$tag")
	if [[ $current == "$FIM_NUM_CTX" ]]; then
		return 0
	fi
	mf=$(mktemp)
	"$writer" "$mf"
	if [[ -n $current ]] || ollama show "$tag" >/dev/null 2>&1; then
		log "rebuilding $tag: num_ctx ${current:-unset} -> $FIM_NUM_CTX"
		ollama create "$tag" -f "$mf" >/dev/null || true
	else
		create_if_missing "$tag" "$base" "$mf"
	fi
	rm -f "$mf"
	current=$(tag_num_ctx "$tag")
	[[ $current == "$FIM_NUM_CTX" ]] || {
		log "WARN: $tag num_ctx is ${current:-unset}, want $FIM_NUM_CTX"
		return 1
	}
}

reconcile_fim_tag() {
	# Reconcile BOTH tags, so the resident one is ready and the other stays a
	# one-command rollback. Only the resident tag's result gates the unit.
	reconcile_tag "$FIM_TAG" "$FIM_BASE" write_fim_modelfile ||
		[[ $RESIDENT_TAG != "$FIM_TAG" ]] || return 1
	reconcile_tag "$INSTRUCT_TAG" "$INSTRUCT_BASE" write_instruct_modelfile ||
		[[ $RESIDENT_TAG != "$INSTRUCT_TAG" ]] || return 1
	return 0
}

prepare_edit_prediction_model() {
	# $RESIDENT_TAG is the sole resident model: Zed, Minuet suffix FIM, and the
	# remote VSCode FIM client (TASK-1156) all request it BY NAME, and
	# OLLAMA_MAX_LOADED_MODELS=1 means a request for any other tag evicts it.
	# ~2-3x faster than qwen2.5-coder:14b-base (BUG-1037).
	local attempt
	for attempt in 1 2 3 4 5; do
		if reconcile_fim_tag; then
			log "warming $RESIDENT_TAG (load-only)"
			if warm_load_only "$RESIDENT_TAG"; then
				touch "$READY_MARKER"
				log "edit-prediction model ready (attempt $attempt)"
				return 0
			fi
		fi
		log "WARN: edit-prediction model incomplete (attempt $attempt/5); retrying in 30s"
		sleep 30
	done
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

mode=warm
case ${1:-} in
"") ;;
--reconcile-only) mode=reconcile ;;
*)
	log "ERROR: unknown argument: $1"
	exit 2
	;;
esac

if [[ $mode == warm ]]; then
	mkdir -p "$(dirname "$READY_MARKER")"
	rm -f "$READY_MARKER"
fi
log "waiting for $OLLAMA_URL"
if ! wait_for_server; then
	log "ERROR: server did not answer within 240s"
	exit 1
fi
log "server ready ($(curl -fsS -m 3 "$OLLAMA_URL/api/version"))"
if [[ $mode == reconcile ]]; then
	reconcile_fim_tag || exit 1
	log "$FIM_TAG and $INSTRUCT_TAG num_ctx match the recipe ($FIM_NUM_CTX); resident=$RESIDENT_TAG"
	exit 0
fi
# Both preparations run concurrently, as the pod's startup.sh did (`… &`), so
# a cold agentpair build never queues behind the FIM retry loop. Only the
# edit-prediction result decides the unit's exit status.
warm_status=0
prepare_agent_pair &
agent_pair_pid=$!
prepare_edit_prediction_model || warm_status=$?
wait "$agent_pair_pid" || log "WARN: agentpair preparation exited non-zero (tags may be missing)"
exit "$warm_status"
