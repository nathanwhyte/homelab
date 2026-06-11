#!/usr/bin/env bash
# Operator helper for the IDEA-019 yt-dlp revival.
#
# Subcommands:
#   run-job <N> [extra-args]
#                            Manually run CronJob N (1-6) NOW by creating
#                            a one-off Job. Optional extra-args string is
#                            spliced into the yt-dlp command line (e.g.
#                            "--no-mtime", "--datebefore 20250101").
#   list-jobs                List the Job and CronJob history in the ns.
#   logs [job|pod]           Stream logs from a Job/Pod (most recent
#                            pod in the namespace if no arg given).
#   archive-list             Show per-playlist archive file sizes and
#                            the most recent video ID appended (proof
#                            the archive is being updated).
#   archive-reset <N>        Wipe /yt-dlp-archive/archive-playlist-N.txt
#                            so the next run re-downloads everything.
#                            Use with care — this forces a full resync.
#   list-archives            Alias for archive-list (kept for grep).
#   delete-archive <N>       Alias for archive-reset (kept for grep).
#   list-cronjobs            Show schedule, suspend state, and last-schedule
#                            time for all 6 CronJobs.
#   enable <N>               Flip CronJob N from suspend:true to suspend:false.
#   disable <N>              Flip CronJob N back to suspend:true.
#   download <URL> [args...] Run a one-off download (no archive, no
#                            playlist config) — useful for ad-hoc
#                            single videos. Uses /downloads/ad-hoc/ as
#                            the output directory.
#   reap-jobs                Delete completed/failed Jobs older than 1h
#                            to keep the namespace tidy.
#
# Environment:
#   YT_DLP_NS                default: yt-dlp
#   YT_DLP_IMAGE              default: nathanwhyte/yt-dlp:2026.06.10-yt-dlp

set -euo pipefail

NS="${YT_DLP_NS:-yt-dlp}"
IMAGE="${YT_DLP_IMAGE:-nathanwhyte/yt-dlp:2026.06.10-yt-dlp}"

usage() {
  sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "missing required command: $1" >&2
    exit 127
  }
}

require_cmd kubectl

# --- subcommands -----------------------------------------------------------

cmd_run_job() {
  local n="${1:-}"
  local extra="${2:-}"
  [ -n "$n" ] || { echo "usage: operator.sh run-job <N> [extra-args]" >&2; exit 64; }
  local cj="yt-dlp-playlist-${n}"
  local job="yt-dlp-playlist-${n}-manual-$(date +%Y%m%d-%H%M%S)"
  echo "creating Job ${job} from CronJob/${cj} (extra args: '${extra}')"
  if [ -n "$extra" ]; then
    kubectl create job --from="cronjob/${cj}" -n "$NS" "$job" \
      --dry-run=client -o json \
      | jq --arg extra "$extra" '
          .spec.template.spec.containers[0].env += [
            {name: "EXTRA", value: $extra}
          ]' \
      | kubectl apply -f -
  else
    kubectl create job --from="cronjob/${cj}" -n "$NS" "$job"
  fi
  echo
  echo "Tail with: operator.sh logs ${job}"
}

cmd_list_jobs() {
  kubectl get jobs,cronjobs -n "$NS"
}

cmd_logs() {
  local target="${1:-}"
  if [ -z "$target" ]; then
    # most recent pod in the namespace
    kubectl logs -n "$NS" -l app.kubernetes.io/name=yt-dlp \
      --tail=200 -f --max-log-requests=10
    return
  fi
  # Try as Job name first, then as Pod name.
  if kubectl get "job/${target}" -n "$NS" >/dev/null 2>&1; then
    kubectl logs -n "$NS" -l "job-name=${target}" --tail=200 -f
  elif kubectl get "pod/${target}" -n "$NS" >/dev/null 2>&1; then
    kubectl logs -n "$NS" "${target}" --tail=200 -f
  else
    echo "no Job or Pod named ${target} in namespace ${NS}" >&2
    exit 64
  fi
}

cmd_archive_list() {
  # We can't easily read the PVC from outside the cluster, so spawn a
  # one-off debug pod that mounts the same media PVC and `ls -la` the
  # archive directory.
  kubectl run yt-dlp-archive-list -n "$NS" \
    --rm -i --restart=Never --image=alpine:3.20 \
    --overrides='{
      "spec": {
        "nodeName": "wemby",
        "containers": [{
          "name": "list",
          "image": "alpine:3.20",
          "command": ["sh", "-c", "ls -la /yt-dlp-archive/ && echo --- && for f in /yt-dlp-archive/archive-playlist-*.txt; do echo \"$f: $(wc -l < $f) entries, last: $(tail -1 $f)\"; done"],
          "volumeMounts": [
            {"name": "media", "mountPath": "/yt-dlp-archive"}
          ]
        }],
        "volumes": [{
          "name": "media",
          "persistentVolumeClaim": {"claimName": "media"}
        }]
      }
    }'
}

cmd_archive_reset() {
  local n="${1:-}"
  [ -n "$n" ] || { echo "usage: operator.sh archive-reset <N>" >&2; exit 64; }
  local file="archive-playlist-${n}.txt"
  echo "This will delete ${file} from the media PVC."
  read -r -p "Proceed? [y/N] " ans
  case "$ans" in
    y|Y|yes|YES) ;;
    *) echo "aborted."; exit 0 ;;
  esac
  kubectl run yt-dlp-archive-reset -n "$NS" \
    --rm -i --restart=Never --image=alpine:3.20 \
    --overrides='{
      "spec": {
        "nodeName": "wemby",
        "containers": [{
          "name": "rm",
          "image": "alpine:3.20",
          "command": ["sh", "-c", "rm -f /yt-dlp-archive/'"${file}"' && echo deleted"],
          "volumeMounts": [
            {"name": "media", "mountPath": "/yt-dlp-archive"}
          ]
        }],
        "volumes": [{
          "name": "media",
          "persistentVolumeClaim": {"claimName": "media"}
        }]
      }
    }'
}

cmd_list_cronjobs() {
  kubectl get cronjobs -n "$NS" \
    -o custom-columns='NAME:.metadata.name,SCHEDULE:.spec.schedule,SUSPEND:.spec.suspend,LAST:.status.lastScheduleTime,AGE:.metadata.creationTimestamp'
}

cmd_set_suspend() {
  local n="${1:-}" val="${2:-}"
  [ -n "$n" ] && [ -n "$val" ] || {
    echo "usage: operator.sh <enable|disable> <N>" >&2
    exit 64
  }
  kubectl patch cronjob "yt-dlp-playlist-${n}" -n "$NS" \
    --type=merge -p "{\"spec\":{\"suspend\":${val}}}"
  echo "yt-dlp-playlist-${n} suspend=${val}"
}

cmd_download() {
  local url="${1:-}"
  shift || true
  [ -n "$url" ] || { echo "usage: operator.sh download <URL> [args...]" >&2; exit 64; }
  local job="yt-dlp-adhoc-$(date +%Y%m%d-%H%M%S)"
  local extra_args="$*"
  echo "creating ad-hoc Job ${job} (url=${url}, args=${extra_args})"
  kubectl run "${job}" -n "$NS" \
    --rm -i --restart=Never --image="$IMAGE" \
    --overrides="$(cat <<JSON
{
  "spec": {
    "nodeName": "wemby",
    "securityContext": {"fsGroup": 1000},
    "containers": [{
      "name": "yt-dlp",
      "image": "${IMAGE}",
      "imagePullPolicy": "IfNotPresent",
      "command": ["/bin/sh", "-c",
        "set -eu; mkdir -p /downloads/ad-hoc; cd /downloads/ad-hoc; exec yt-dlp ${extra_args} '${url}'"
      ],
      "resources": {
        "requests": {"cpu": "500m", "memory": "1Gi"},
        "limits":   {"cpu": "4",    "memory": "8Gi"}
      },
      "volumeMounts": [
        {"name": "media", "mountPath": "/downloads"}
      ]
    }],
    "volumes": [{
      "name": "media",
      "persistentVolumeClaim": {"claimName": "media"}
    }]
  }
}
JSON
)"
}

cmd_reap_jobs() {
  # Delete completed/failed Jobs older than 1h.
  kubectl get jobs -n "$NS" -o json \
    | jq -r '
        .items[]
        | select(.status.conditions[]?.type == "Complete" or .status.conditions[]?.type == "Failed")
        | select((.status.completionTime // "9999") | fromdateiso8601 < (now - 3600))
        | .metadata.namespace + " " + .metadata.name
      ' \
    | while read -r ns name; do
        [ -n "$name" ] || continue
        echo "deleting Job ${ns}/${name}"
        kubectl delete job "$name" -n "$ns"
      done
}

# --- dispatch --------------------------------------------------------------

sub="${1:-}"
shift || true

case "$sub" in
  run-job)            cmd_run_job "$@" ;;
  list-jobs)          cmd_list_jobs ;;
  logs)               cmd_logs "$@" ;;
  archive-list|list-archives)   cmd_archive_list ;;
  archive-reset|delete-archive) cmd_archive_reset "$@" ;;
  list-cronjobs)      cmd_list_cronjobs ;;
  enable)             cmd_set_suspend "${1:-}" "false" ;;
  disable)            cmd_set_suspend "${1:-}" "true" ;;
  download)           cmd_download "$@" ;;
  reap-jobs)          cmd_reap_jobs ;;
  -h|--help|help|"")  usage ;;
  *)
    echo "unknown subcommand: ${sub}" >&2
    usage >&2
    exit 64
    ;;
esac
