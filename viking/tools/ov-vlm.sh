#!/usr/bin/env bash
# Manual control for the OpenViking VLM (llamacpp-cuda-ov).
#
# The VLM (Qwen3-8B on manu's GTX 1080) holds ~9 GB resident and is used by
# OpenViking for L0 abstract / L1 overview generation during indexing, and by
# the `viking.nathanwhyte.dev` console / MCP for any write path that needs
# the model. As of 2026-06-11 the VLM is steady-state replicas=1, always on:
# the 1080 is VLM-exclusive (embedder on wemby/1060, hermes-agent is CPU-only),
# cold model load from the cached Longhorn PVC is ~40s, and a premature
# scale-down can cut off in-flight L0 jobs (this exact failure was hit on
# 2026-06-10, requiring a manual scale-back-up to recover).
#
# This script exists for manual override -- e.g. releasing the GPU if
# something else ever needs the 1080, or as a safety net if the VLM pod
# crashes and you need to recreate it. `up` / `down` / `status` are the
# primary commands; `run` is kept as a compatibility alias that wraps a
# command with up/drain/down for the old on-demand idiom, but is no longer
# the recommended pattern (just run the command -- the VLM is already up).
#
# `down` waits for the OV index queue to drain before scaling to 0, so you
# don't kill the VLM out from under in-flight L0/L1 generation. `up` waits
# for the rollout to complete (cold model load). The drain step uses the
# same `ov wait` (with polling fallback) pattern as before.
#
# Usage:
#   ov-vlm.sh up                         # scale to 1 and wait until ready
#   ov-vlm.sh down                       # drain queue, then scale to 0
#   ov-vlm.sh status                     # show replicas / readiness
#   ov-vlm.sh run -- <command...>        # legacy: up -> run -> drain -> down
#
# Examples:
#   ov-vlm.sh status                     # check the VLM
#   ov-vlm.sh down                       # release the GPU
#   ov-vlm.sh run -- python3 viking/tools/compendium-sync.py sync --limit 50
#
# `down` and `run` require the same OPENVIKING_URL / OPENVIKING_KEY env that
# the sync tool uses, because the queue drain goes through `ov wait`.
#
# Env overrides:
#   VIKING_NS          namespace                       (default: viking)
#   VLM_DEPLOY         deployment name                 (default: llamacpp-cuda-ov)
#   VLM_UP_TIMEOUT     seconds to wait for readiness    (default: 600)
#   VLM_DRAIN_TIMEOUT  seconds to wait for queue drain  (default: 3600)
set -euo pipefail

NS="${VIKING_NS:-viking}"
DEPLOY="${VLM_DEPLOY:-llamacpp-cuda-ov}"
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
  # Wait for the OV index queue to drain. Tries `ov wait` first (single
  # long-poll blocking call); if that errors out (the v0.3.14 CLI/server
  # build mismatch on /api/v1/system/wait means it returns "Network error"
  # even when the request actually reaches the server), fall back to polling
  # `ov status` until pending+in-progress = 0, or the timeout elapses.
  local deadline=$(( $(date +%s) + DRAIN_TIMEOUT ))
  log "waiting up to ${DRAIN_TIMEOUT}s for OV index queue to drain"
  if ov wait --timeout "$DRAIN_TIMEOUT" 2>/dev/null; then
    return 0
  fi
  log "ov wait unavailable (known v0.3.14 client/server mismatch on /api/v1/system/wait); falling back to polling ov status"
  while [ "$(date +%s)" -lt "$deadline" ]; do
    local out
    out=$(ov status 2>/dev/null) || { sleep 5; continue; }
    local pending inprog
    pending=$(echo "$out" | awk '/^TOTAL/ {print $3}' | head -1)
    inprog=$(echo "$out"  | awk '/^TOTAL/ {print $5}' | head -1)
    pending="${pending:-?}"
    inprog="${inprog:-?}"
    log "queue: pending=$pending in_progress=$inprog"
    if [ "$pending" = "0" ] && [ "$inprog" = "0" ]; then
      log "queue drained"
      return 0
    fi
    sleep 10
  done
  log "WARN: drain timeout (${DRAIN_TIMEOUT}s) reached; queue may still have pending work"
  return 1
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
