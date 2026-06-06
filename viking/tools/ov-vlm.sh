#!/usr/bin/env bash
# Scale the OpenViking VLM (llamacpp-rocm) up around an indexing session and
# back to 0 when idle.
#
# The VLM (Qwen3-8B on timmy's RX 9070 XT) holds ~9 GB resident and is only
# needed during indexing, where OpenViking calls it to generate L0 abstracts
# and L1 overviews. Reads, searches, and `find` never touch it. So the idle
# state is replicas=0; we bring it up only for the duration of an index run.
#
# Indexing is async on the OV server: `add-resource` enqueues and returns long
# before the VLM work runs. So `run` does NOT scale down right after the wrapped
# command exits -- it first `ov wait`s for the single-worker queue to drain,
# otherwise we would kill the VLM out from under in-flight L0/L1 generation.
#
# Usage:
#   ov-vlm.sh up                         # scale to 1 and wait until ready
#   ov-vlm.sh down                       # scale to 0
#   ov-vlm.sh status                     # show replicas / readiness
#   ov-vlm.sh run -- <command...>        # up -> run -> drain queue -> down
#
# Examples:
#   ov-vlm.sh run -- python3 viking/tools/compendium-sync.py sync --limit 50
#   COMPENDIUM_ROOT=~/code/personal-compendium OV_TARGET_BASE=viking://resources/personal \
#     ov-vlm.sh run -- python3 viking/tools/compendium-sync.py sync
#
# `run` and `down`-after-drain require the same OPENVIKING_URL / OPENVIKING_KEY
# env that the sync tool uses, because the queue drain goes through `ov wait`.
#
# Env overrides:
#   VIKING_NS          namespace                       (default: viking)
#   VLM_DEPLOY         deployment name                 (default: llamacpp-rocm)
#   VLM_UP_TIMEOUT     seconds to wait for readiness    (default: 600)
#   VLM_DRAIN_TIMEOUT  seconds to wait for queue drain  (default: 3600)
set -euo pipefail

NS="${VIKING_NS:-viking}"
DEPLOY="${VLM_DEPLOY:-llamacpp-rocm}"
UP_TIMEOUT="${VLM_UP_TIMEOUT:-600}"
DRAIN_TIMEOUT="${VLM_DRAIN_TIMEOUT:-3600}"

log() { printf '%s %s\n' "$(date +%H:%M:%S)" "$*" >&2; }

usage() {
  sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

vlm_up() {
  log "scaling $DEPLOY -> 1 in $NS"
  kubectl scale deployment "$DEPLOY" -n "$NS" --replicas=1 >/dev/null
  log "waiting up to ${UP_TIMEOUT}s for rollout (cold model load from cached PVC)"
  kubectl rollout status deployment "$DEPLOY" -n "$NS" --timeout="${UP_TIMEOUT}s"
  log "$DEPLOY is ready"
}

vlm_down() {
  log "scaling $DEPLOY -> 0 in $NS"
  kubectl scale deployment "$DEPLOY" -n "$NS" --replicas=0 >/dev/null
}

vlm_status() {
  kubectl get deployment "$DEPLOY" -n "$NS" \
    -o 'custom-columns=NAME:.metadata.name,DESIRED:.spec.replicas,READY:.status.readyReplicas,AVAILABLE:.status.availableReplicas'
}

drain() {
  log "waiting up to ${DRAIN_TIMEOUT}s for OV index queue to drain (ov wait)"
  ov wait --timeout "$DRAIN_TIMEOUT"
}

main() {
  [ "$#" -ge 1 ] || usage 1
  case "$1" in
    up) vlm_up ;;
    down) vlm_down ;;
    status) vlm_status ;;
    run)
      shift
      [ "${1:-}" = "--" ] && shift
      [ "$#" -ge 1 ] || { log "run: no command given"; usage 1; }
      # Always return to idle (replicas=0) on any exit path, including Ctrl-C.
      trap vlm_down EXIT
      vlm_up
      rc=0
      "$@" || rc=$?
      drain || log "WARN: ov wait did not confirm a clean drain; scaling down anyway"
      exit "$rc"
      ;;
    -h | --help | help) usage 0 ;;
    *) log "unknown command: $1"; usage 1 ;;
  esac
}

main "$@"
