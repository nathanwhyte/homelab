#!/usr/bin/env bash
# Post-start model preparation for the host Ollama daemon on timmy (IMPR-1075).
#
# DESPITE THE NAME, warming is the incidental half. The load-bearing job is
# RECONCILIATION: guaranteeing every tag this host serves exists with a baked
# num_ctx before anything can request it.
#
# Why that matters. OLLAMA_KEEP_ALIVE=-1 is set globally, so a model loaded by
# any request stays resident forever — the warm call only moves the ~5 s cold
# load off a user's first keystroke after a restart. But OLLAMA_CONTEXT_LENGTH
# is 131072 and the unit's MemoryMax is 16G, so a tag WITHOUT a baked num_ctx
# projects ~36 GiB of KV, spills into host memory, and the cgroup OOM-kills the
# whole daemon. "It will load on first request" is true, and that is precisely
# the hazard: what loads must already have num_ctx baked. This is not
# hypothetical — it is the 2026-09-16 crash in IDEA-1105.
#
# Reconciliation also catches DRIFT, not just absence: the 16384 -> 8192
# migration would otherwise have left 16384 x 4 slots and overflowed the card,
# which is why install-host-ollama.sh runs --reconcile-only BEFORE restarting
# into a new drop-in.
#
# Port of the retired pod's startup.sh (llama/ollama-configmap.yaml, PR #48).
# Runs from ollama-warm.service (PartOf=ollama.service), so every daemon restart
# re-runs it. The in-cluster CronJob in llama/ollama-jobs.yaml re-asserts the
# pin every 15 minutes, but SKIPS whenever any model is already resident — it is
# eviction recovery, not a competing pinner. Its MODEL must name the same tag as
# RESIDENT_TAG below, or a failed warm unit silently reverts the resident tag
# within 15 minutes.
#
# Two independent concerns, kept apart on purpose:
#   1. SERVER readiness is the daemon's own business — this script never gates
#      it, and a failure here leaves the API serving (OV's cloud VLM route
#      through ollama.llama.svc needs only the API, no local runner).
#   2. LOCAL model prep is best-effort with retry. /run/ollama/models-ready is
#      written as an observable signal; nothing outside this script reads it.
#
#   ollama-warm.sh                   wait, reconcile both tags, pin RESIDENT_TAG,
#                                    build the agentpair:* tags (never warm them)
#   ollama-warm.sh --reconcile-only  wait, rebuild either tag whose num_ctx drifted
#                                    from the recipe, load nothing; exit 1 if the
#                                    resident tag cannot be made to match
#                                    (install-host-ollama.sh runs this BEFORE
#                                    restarting into the new drop-in)
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
# IDEA-1105 OpenViking/editor pair. Not resident by default; see prepare_ov_pair.
# The FIM half sits at 8192 like the deepseek tag above (2.7 GiB vs 3.6 at
# 16384); the VLM half needs 16384 to hold an OV entry plus a batched
# overview_generation over semantic.overview_batch_size summaries.
OV_FIM_TAG=qwen2.5-coder:fim
OV_FIM_BASE=qwen2.5-coder:3b-base
OV_FIM_NUM_CTX=8192
OV_VLM_TAG=gemma4:vlm
OV_VLM_BASE=gemma4:12b-it-qat
# 32768 since 2026-09-21 (BUG-1155). MUST track PARAMETER num_ctx in
# llama/ollama/gemma4-vlm.Modelfile: reconcile_tag compares the live tag
# against THIS value, so a Modelfile-only change makes every warm run log
# "WARN: gemma4:vlm num_ctx is 32768, want 16384" and return 1, failing
# --reconcile-only on each install and daemon restart while the in-cluster
# CronJob re-asserts every 15 minutes.
OV_VLM_NUM_CTX=32768
# Which tag stays pinned for edit prediction. :instruct since 2026-09-18 — both
# tags decode at the same speed (170 vs 160 tok/s, identical TTFT) and measure
# the same through the real minuet stack (28% vs 30% empty after filtering,
# comparable lengths); it was adopted on the quality of its completions, judged
# in the editor. dotfiles nvim/lua/plugins/minuet.lua and zed/settings.json name
# this tag, and OLLAMA_MAX_LOADED_MODELS=1 means a request for the OTHER tag
# evicts this one and pays a ~5 s reload -- so change all three together.
# Both tags are reconciled below, so rolling back is: set this to $FIM_TAG,
# re-run, and flip the two dotfiles entries.
# $OV_FIM_TAG since 2026-09-18 (IDEA-1105). Rolling back is: set this to
# $INSTRUCT_TAG, drop OLLAMA_MAX_LOADED_MODELS to 1 in the drop-in, re-run, and
# flip the two dotfiles entries — all four together, or the clients and the host
# disagree about which tag is resident.
RESIDENT_TAG=${RESIDENT_TAG:-$OV_FIM_TAG}
# Cap on best-effort standby reconciliation (a cold store can pull ~9 GB).
STANDBY_TIMEOUT=${STANDBY_TIMEOUT:-600}
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
	# $1 tag, $2 base, $3 modelfile writer, $4 wanted num_ctx (default
	# FIM_NUM_CTX). Build when missing, and REBUILD when an existing tag's
	# num_ctx differs from the recipe: create_if_missing alone would keep a
	# 16384 tag from the 2-slot posture, and 16384 x 4 slots overflows the card.
	# Loads nothing. Non-zero unless the tag ends at the wanted num_ctx.
	local tag=$1 base=$2 writer=$3 want=${4:-$FIM_NUM_CTX} mf current
	current=$(tag_num_ctx "$tag")
	if [[ $current == "$want" ]]; then
		return 0
	fi
	mf=$(mktemp)
	"$writer" "$mf"
	if [[ -n $current ]] || ollama show "$tag" >/dev/null 2>&1; then
		log "rebuilding $tag: num_ctx ${current:-unset} -> $want"
		ollama create "$tag" -f "$mf" >/dev/null || true
	else
		create_if_missing "$tag" "$base" "$mf"
	fi
	rm -f "$mf"
	current=$(tag_num_ctx "$tag")
	[[ $current == "$want" ]] || {
		log "WARN: $tag num_ctx is ${current:-unset}, want $want"
		return 1
	}
}

reconcile_one() {
	# Reconcile a single tag by name, picking its base and recipe.
	local dir
	dir=${MODELFILE_DIR:-/opt/ollama-host/modelfiles}
	case $1 in
	"$FIM_TAG") reconcile_tag "$FIM_TAG" "$FIM_BASE" write_fim_modelfile ;;
	"$INSTRUCT_TAG") reconcile_tag "$INSTRUCT_TAG" "$INSTRUCT_BASE" write_instruct_modelfile ;;
	# The OV pair is reconcilable by name so RESIDENT_TAG can point at
	# $OV_FIM_TAG without this failing "no recipe for tag" — adopting the pair
	# is one variable change, not a code change.
	"$OV_FIM_TAG")
		OV_RECIPE="$dir/qwen2.5-coder-fim.Modelfile" \
			reconcile_tag "$OV_FIM_TAG" "$OV_FIM_BASE" copy_modelfile "$OV_FIM_NUM_CTX"
		;;
	"$OV_VLM_TAG")
		OV_RECIPE="$dir/gemma4-vlm.Modelfile" \
			reconcile_tag "$OV_VLM_TAG" "$OV_VLM_BASE" copy_modelfile "$OV_VLM_NUM_CTX"
		;;
	*)
		log "ERROR: no recipe for tag $1"
		return 1
		;;
	esac
}

standby_tag() {
	# The tag a rollback would make resident, kept reconciled so the flip is one
	# variable rather than a cold ~9 GB pull. Under the OV pair posture that is
	# the deepseek tag the editors used before 2026-09-18.
	case $RESIDENT_TAG in
	"$FIM_TAG") printf '%s' "$INSTRUCT_TAG" ;;
	"$OV_FIM_TAG") printf '%s' "$INSTRUCT_TAG" ;;
	*) printf '%s' "$FIM_TAG" ;;
	esac
}

reconcile_all_strict() {
	# EVERY servable tag must match the recipe. --reconcile-only is the
	# installer's gate (install-host-ollama.sh) before it restarts into a new
	# OLLAMA_NUM_PARALLEL, and the hazard it exists to catch is a tag left at
	# num_ctx 16384: 16384 x 4 slots overflows the card. A stale client can
	# still request the STANDBY tag, so letting the standby fail here would let
	# an unsafe tag through the gate. Best-effort belongs in the boot path, not
	# in installation.
	local rc=0
	reconcile_one "$RESIDENT_TAG" || rc=1
	reconcile_one "$(standby_tag)" || rc=1
	return "$rc"
}

run_bounded() {
	# $1 seconds, rest command. The standby path can hit an unbounded
	# `ollama pull` on a cold store, and the unit runs with
	# TimeoutStartSec=infinity -- so a stalled download must not be able to hold
	# up anything. Used only for best-effort work.
	local secs=$1 pid waited=0
	shift
	"$@" &
	pid=$!
	while kill -0 "$pid" 2>/dev/null && ((waited < secs)); do
		sleep 1
		waited=$((waited + 1))
	done
	if kill -0 "$pid" 2>/dev/null; then
		log "WARN: '$*' exceeded ${secs}s; terminating"
		kill -TERM "$pid" 2>/dev/null || true
		return 1
	fi
	wait "$pid"
}

prepare_standby_tag() {
	# Best-effort, and deliberately AFTER the resident tag is warm: keeping a
	# rollback tag ready must never delay edit prediction.
	local tag
	tag=$(standby_tag)
	if run_bounded "$STANDBY_TIMEOUT" reconcile_one "$tag"; then
		log "standby tag $tag ready at num_ctx $FIM_NUM_CTX"
	else
		log "WARN: standby tag $tag not reconciled; rollback would need a rebuild"
	fi
}

prepare_edit_prediction_model() {
	# $RESIDENT_TAG is the sole resident model: Zed, Minuet suffix FIM, and the
	# remote VSCode FIM client (TASK-1156) all request it BY NAME, and
	# OLLAMA_MAX_LOADED_MODELS=1 means a request for any other tag evicts it.
	# ~2-3x faster than qwen2.5-coder:14b-base (BUG-1037).
	local attempt
	for attempt in 1 2 3 4 5; do
		# ONLY the resident tag gates warming. Reconciling the standby here
		# would put a possible `ollama pull` in front of edit prediction.
		if reconcile_one "$RESIDENT_TAG"; then
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

warm_ov_vlm() {
	# Pin OpenViking's summarizer beside the resident FIM tag. Runs AFTER
	# prepare_edit_prediction_model so a slow VLM load can never delay edit
	# prediction, and best-effort: OV degrades to missing abstracts, while a
	# failed autocomplete is what the user feels. Needs
	# OLLAMA_MAX_LOADED_MODELS=2 — at 1 this warm simply evicts the tag that was
	# just pinned, which is why the drop-in and this function move together.
	[[ $RESIDENT_TAG == "$OV_FIM_TAG" ]] || return 0
	if reconcile_one "$OV_VLM_TAG"; then
		log "warming $OV_VLM_TAG (OpenViking summarizer)"
		warm_load_only "$OV_VLM_TAG" ||
			log "WARN: $OV_VLM_TAG did not warm; OpenViking will generate no abstracts until it does"
	else
		log "WARN: $OV_VLM_TAG not reconciled; skipping warm"
	fi
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

copy_modelfile() {
	# reconcile_tag writer that emits a reference Modelfile verbatim, so the
	# checked-in recipe in llama/ollama/ stays the single source of truth rather
	# than being restated as a heredoc the way write_fim_modelfile must be.
	cat "$OV_RECIPE" >"$1"
}

prepare_ov_pair() {
	# IDEA-1105 OpenViking/editor pair. Warms NOTHING and leaves RESIDENT_TAG
	# alone, so running this cannot change what is loaded — same contract as
	# prepare_agent_pair. Adopting the pair is a separate, deliberate change:
	# point RESIDENT_TAG at $OV_FIM_TAG, warm $OV_VLM_TAG alongside it, and keep
	# OLLAMA_MAX_LOADED_MODELS at 2.
	#
	# RECONCILES rather than create-if-missing. qwen2.5-coder:fim is a name this
	# repo has used before at num_ctx 16384 (the agentpair/IDEA-1071 recipe), and
	# create_if_missing returns early on any existing tag — so a host carrying
	# the old tag would silently keep 16384 and never get the 0.9 GB this recipe
	# is here to save. Same class of miss as the 2-slot tag in #110.
	local dir
	dir=${MODELFILE_DIR:-/opt/ollama-host/modelfiles}
	[[ -d $dir ]] || {
		log "WARN: $dir missing; ov-pair tags not built"
		return 0
	}
	OV_RECIPE="$dir/qwen2.5-coder-fim.Modelfile" \
		reconcile_tag "$OV_FIM_TAG" "$OV_FIM_BASE" copy_modelfile "$OV_FIM_NUM_CTX" ||
		log "WARN: $OV_FIM_TAG not reconciled"
	OV_RECIPE="$dir/gemma4-vlm.Modelfile" \
		reconcile_tag "$OV_VLM_TAG" "$OV_VLM_BASE" copy_modelfile "$OV_VLM_NUM_CTX" ||
		log "WARN: $OV_VLM_TAG not reconciled"
}

mode=warm
case ${1:-} in
"") ;;
--reconcile-only) mode=reconcile ;;
# Build/reconcile the IDEA-1105 OV pair and stop. Loads nothing and does not
# touch RESIDENT_TAG, so it is safe to run against a live daemon; it is also the
# seam the pair's regression tests drive.
--ov-pair-only) mode=ovpair ;;
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
	reconcile_all_strict || exit 1
	log "$FIM_TAG and $INSTRUCT_TAG num_ctx match the recipe ($FIM_NUM_CTX); resident=$RESIDENT_TAG"
	exit 0
fi
if [[ $mode == ovpair ]]; then
	prepare_ov_pair
	log "$OV_FIM_TAG=$(tag_num_ctx "$OV_FIM_TAG") $OV_VLM_TAG=$(tag_num_ctx "$OV_VLM_TAG")"
	exit 0
fi
# Both preparations run concurrently, as the pod's startup.sh did (`… &`), so
# a cold agentpair build never queues behind the FIM retry loop. Only the
# edit-prediction result decides the unit's exit status.
warm_status=0
prepare_agent_pair &
agent_pair_pid=$!
prepare_ov_pair &
ov_pair_pid=$!
prepare_edit_prediction_model || warm_status=$?
# After edit prediction, never before it: a slow VLM load must not delay the tag
# the editor is waiting on. Does not gate warm_status — OV losing its summarizer
# is degraded abstracts, not a broken editor.
warm_ov_vlm
# Standby reconciliation runs AFTER the resident tag is warm, so a cold-store
# `ollama pull` for the rollback tag cannot delay edit prediction. Best-effort
# and bounded; its result never gates the unit.
prepare_standby_tag
wait "$agent_pair_pid" || log "WARN: agentpair preparation exited non-zero (tags may be missing)"
wait "$ov_pair_pid" || log "WARN: ov-pair preparation exited non-zero (tags may be missing)"
exit "$warm_status"
