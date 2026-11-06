#!/usr/bin/env bash
# timmy-offline.sh — take timmy out of the cluster cleanly for a planned outage
# (Windows dual-boot session, live-USB disk work) and bring it back.
#
#   timmy-offline.sh down    silence alerts, suspend dependent CronJobs, park cross-node volumes,
#                            spin down memory-heavy services, cordon + drain
#   timmy-offline.sh windows down, then boot timmy into Windows once, no password needed
#                            (the reboot after the Windows session returns to Ubuntu)
#   timmy-offline.sh up      finish the node-maintenance cycle, restore the parked + spun-down workloads,
#                            resume CronJobs, run a catch-up k3s backup, expire the silence,
#                            list pods not Running and ready, not Completed, or crashlooping (a warning, never a failure)
#   timmy-offline.sh status  what is parked/suspended/silenced, what is cordoned, Longhorn health,
#                            unhealthy pods
#
# Why this exists on top of node-maintenance.sh: three Longhorn volumes are
# attached on manu/wemby but their ONLY replica lives on timmy's disk
# (garage/data-garage-0, garage/data-garage-2,
# viking/embedder-cuda-model-cache as of 2026-09-18; viking/reranker-model-cache
# went with the reranker's retirement). Powering timmy off while
# those pods run yanks the iSCSI backing device out from under them — the exact
# LMDB-corruption path from BUG-1033. `down` scales those workloads to zero so
# every such volume detaches cleanly before the drain; `up` restores the saved
# replica counts after timmy is Ready and Longhorn is healthy.
#
# `down` also passes --spin-down to node-maintenance.sh drain, which scales the
# memory-heavy services (viking/openviking, viking/ov-vectordb) to 0 *before*
# that script's memory-headroom preflight runs. Without it the preflight weighs
# timmy's evicted pods against manu/wemby's free memory with nothing freed and
# blocks the drain outright, so the documented `down` path cannot drain timmy on
# a loaded cluster — BUG-1105's spin-down ordering fix only helps a caller that
# opts in. `up` restores the recorded replica counts along with the parked
# workloads, via node-maintenance.sh recover.
#
# timmy is the only control plane, so between `down` and `up` there is no API
# server — manu and wemby keep running what they already have, nothing else.
#
# `down` also quiets the outage it is about to cause (BUG-1197, 2026-10-02).
# Without this a planned Windows session paged Slack ~50 times: `park` itself
# trips GarageQuorumLost, every CronJob that needs timmy, Garage or OpenViking
# fails while it is out, and each failed Job keeps firing KubeJobFailed hourly
# until its ttl reaps it — k3s-datastore-backup has no ttl by design (IMPR-1126),
# so its failure paged until deleted by hand. Two controls:
#   - an Alertmanager silence for the whole window, recorded by ID so `up`
#     expires exactly that one. It has a hard end (SILENCE_DURATION, default 24h)
#     so a forgotten `up` cannot mute the cluster indefinitely.
#   - QUIESCED_CRONJOBS are suspended; `up` resumes only the ones `down`
#     suspended (a CronJob that was already suspended stays suspended), then
#     runs one catch-up k3s datastore backup so the window leaves no gap.
# Prometheus and Alertmanager are pinned to timmy, so nothing alerts during the
# window anyway; the silence matters for the burst on return, when stale state
# from before the shutdown is evaluated all at once.
#
# Session watches learn of the window from a third record, $STATE_DIR/window
# ("<end epoch seconds> <what>", read by maintenance-active.py, IDEA-1150).
# `down` writes it after the silence and suspends, before it parks anything, and
# `up` removes it once it completes. Its end is the silence's (a re-run that keeps
# the silence takes its recorded end, $STATE_DIR/silence-end), so a forgotten or
# failed `up` stops covering failures when the silence stops muting them; the
# other records mean "left to undo" and can outlive the window.
#
# `down` runs node-maintenance.sh's own refusals (another cycle active, an earlier
# node still owing a reboot) before it silences, suspends or parks anything. On
# 2026-10-21 they ran last, inside the drain call: everything was parked and
# detached, then wemby's pending reboot refused the drain, timmy dropped before
# anyone noticed, and OpenViking took a power-cut stop. `up` in turn tolerates a
# `down` that never reached the drain: with no reboot to verify it skips
# node-maintenance.sh finish instead of dying on the missing cycle state.
#
# A power-off stays manual (needs a TTY for sudo):
#   ssh -t timmy-lan sudo shutdown -h now
# A Windows session does not: `windows` runs `down`, then `grub-reboot` the
# os-prober entry (Windows shares the Ubuntu ESP on nvme0n1p1) and reboots, both
# through NOPASSWD sudoers grants pinned to those exact commands (GUIDE-1070
# § sudoers). It boots Windows exactly once; the following reboot returns to
# Ubuntu. By hand, the same is:
#   ssh -t timmy-lan 'sudo grub-reboot "Windows Boot Manager (on /dev/nvme0n1p1)" && sudo reboot'

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# The script's own path for the commands it prints: $0 is the caller when sourced.
SELF=$SCRIPT_DIR/$(basename "${BASH_SOURCE[0]}")
# Overridable so the tests can run down/up against a stub instead of the real
# script (and the real cluster).
NODE_MAINTENANCE=${NODE_MAINTENANCE:-$SCRIPT_DIR/node-maintenance.sh}
NODE=timmy
# SSH alias for the printed power-off command, the same "<node><suffix>" rule as
# node-maintenance.sh: timmy-lan bypasses Tailscale SSH check-mode (GUIDE-1070).
NODE_SSH=$NODE${NODE_SSH_SUFFIX--lan}
STATE_ROOT=${XDG_STATE_HOME:-"$HOME/.local/state"}
STATE_DIR=$STATE_ROOT/homelab-timmy-offline
SCALE_FILE=$STATE_DIR/parked-scales
DETACH_TIMEOUT_SECONDS=${DETACH_TIMEOUT_SECONDS:-300}
READY_TIMEOUT_SECONDS=${READY_TIMEOUT_SECONDS:-600}
KUBECTL=${KUBECTL:-kubectl}
SUSPEND_FILE=$STATE_DIR/suspended-cronjobs
SILENCE_FILE=$STATE_DIR/silence-id
SILENCE_END_FILE=$STATE_DIR/silence-end # "<end epoch> <id>", beside silence-id
SILENCE_DURATION=${SILENCE_DURATION:-24h}
SILENCE_MAX_SECONDS=2592000 # 30d
WINDOW_FILE=$STATE_DIR/window
AM_NAMESPACE=grafana
AM_POD=alertmanager-prom-alertmanager-0
# `windows`: the os-prober GRUB entry (Windows shares the Ubuntu ESP on
# nvme0n1p1). Pinned, with /usr/sbin/grub-reboot, in timmy's sudoers grant.
WINDOWS_GRUB_ENTRY="Windows Boot Manager (on /dev/nvme0n1p1)"
# The ESP the entry names, and the Windows loader os-prober found on it.
WINDOWS_ESP=/dev/nvme0n1p1
WINDOWS_LOADER=/boot/efi/EFI/Microsoft/Boot/bootmgfw.efi
SSH=${SSH:-ssh} # overridable so the tests can stub it
# Prometheus, for pod_health's restart history (namespace/service:port), and the
# restarts in 24h of any one container that list its pod. A node reboot restarts
# each of its containers once, and timmy reboots up to ~3 times a day.
PROM_SERVICE=${PROM_SERVICE:-grafana/prom-prometheus:9090}
RESTART_WARN_24H=${RESTART_WARN_24H:-5}

# CronJobs that cannot succeed while timmy is out. Format: namespace/name
QUIESCED_CRONJOBS=(
	kube-system/k3s-datastore-backup # nodeSelector timmy; reads timmy's datastore
	compendium/compendium-sync       # needs viking/openviking, spun down by `down`
	viking/ovlock-janitor            # needs OpenViking and Garage, both down
)

# Workloads whose Longhorn volume lives only on timmy while the pod runs elsewhere,
# plus garage-1 (on timmy) because Garage should stop as a unit, not one node at a time.
# omnipendium-db, cloud-llm-counter and Grafana are NOT pinned to timmy: the drain
# reschedules them onto manu/wemby, where Longhorn happily attaches their timmy-only
# volume over iSCSI (seen 2026-09-02). Grafana joined on 2026-11-03: its RWO
# grafana-storage claim (2026-10-28) has its only replica on timmy. Left out, `down`
# refuses at wait_cross_node_detached (before the drain) when Grafana already runs
# on manu/wemby, or at the post-drain check when the drain moves it there.
# Format: kind/namespace/name
PARKED=(
	statefulset/garage/garage
	deployment/viking/embedder-qwen-cuda
	deployment/omnipendium/omnipendium-db
	deployment/llama/cloud-llm-counter
	deployment/grafana/kube-prometheus-stack-grafana
)

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARN:\033[0m %s\n' "$*"; }
die() {
	printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2
	exit 1
}

usage() {
	sed -n '2,13p' "$SELF" | sed 's/^# \{0,1\}//'
	exit 1
}

# --- Longhorn helpers -----------------------------------------------------------

# Volumes attached on another node whose replicas are all on $NODE. Any pod still
# using one of these when timmy powers off loses its disk mid-write.
cross_node_orphans() {
	"$KUBECTL" get volumes.longhorn.io,replicas.longhorn.io -n longhorn-system -o json |
		jq -r --arg n "$NODE" '.items as $all
			| ($all | map(select(.kind == "Replica")) | group_by(.spec.volumeName)
				| map({key: .[0].spec.volumeName, value: (map(.spec.nodeID) | unique)}) | from_entries) as $nodes
			| $all[] | select(.kind == "Volume")
			| select(.status.state == "attached" and .status.currentNodeID != $n)
			| select(($nodes[.metadata.name] // []) == [$n])
			| "\(.status.kubernetesStatus.namespace)/\(.status.kubernetesStatus.pvcName)\tattached on \(.status.currentNodeID)"'
}

wait_cross_node_detached() {
	local deadline left='' row printed=''
	deadline=$((SECONDS + DETACH_TIMEOUT_SECONDS))
	log "waiting for timmy-only volumes attached elsewhere to detach..."
	while ((SECONDS < deadline)); do
		left=$(cross_node_orphans) || die "cannot read Longhorn state"
		[[ -z $left ]] && return 0
		# Each distinct row once, not on every 10s poll (a volume back on a node
		# already printed is not printed again).
		while IFS= read -r row; do
			grep -qxF -- "$row" <<<"$printed" && continue
			printf '    still attached: %s\n' "$row"
			printed+=$row$'\n'
		done <<<"$left"
		sleep 10
	done
	# Name them here: the loop printed each row only once, possibly minutes ago.
	die "volumes still attached after ${DETACH_TIMEOUT_SECONDS}s (${left//$'\n'/; }); not safe to power off"
}

# --- Parking --------------------------------------------------------------------

current_replicas() {
	local kind=$1 ns=$2 name=$3
	"$KUBECTL" -n "$ns" get "$kind" "$name" -o jsonpath='{.spec.replicas}'
}

park() {
	local entry kind ns name replicas
	mkdir -p "$STATE_DIR"
	if [[ -s $SCALE_FILE ]]; then
		# Re-run of `down` (timmy came back without `up`, e.g. an aborted live-USB
		# session): keep the original saved counts, just re-assert zero.
		warn "parked state already exists — re-asserting 0 replicas, keeping saved counts"
		for entry in "${PARKED[@]}"; do
			IFS=/ read -r kind ns name <<<"$entry"
			grep -q "^$entry	" "$SCALE_FILE" || die "[$entry] is in PARKED but not in $SCALE_FILE; add its replica count by hand"
			replicas=$(current_replicas "$kind" "$ns" "$name") || die "cannot read $entry"
			[[ $replicas == 0 ]] && continue
			log "[$entry] scale $replicas -> 0 (saved count unchanged)"
			"$KUBECTL" -n "$ns" scale "$kind" "$name" --replicas=0 >/dev/null
		done
		return 0
	fi
	: >"$SCALE_FILE"
	for entry in "${PARKED[@]}"; do
		IFS=/ read -r kind ns name <<<"$entry"
		replicas=$(current_replicas "$kind" "$ns" "$name") || die "cannot read $entry"
		printf '%s\t%s\n' "$entry" "$replicas" >>"$SCALE_FILE"
		if [[ $replicas == 0 ]]; then
			log "[$entry] already at 0"
			continue
		fi
		log "[$entry] scale $replicas -> 0"
		"$KUBECTL" -n "$ns" scale "$kind" "$name" --replicas=0 >/dev/null
	done
}

unpark() {
	local entry kind ns name replicas
	# A `down` that stopped before `park` (a CronJob it could not suspend) leaves
	# the silence and suspend records with nothing parked; `up` must still clear those.
	[[ -s $SCALE_FILE ]] || {
		log "nothing parked by down"
		return 0
	}
	while IFS=$'\t' read -r entry replicas; do
		IFS=/ read -r kind ns name <<<"$entry"
		log "[$entry] scale 0 -> $replicas"
		"$KUBECTL" -n "$ns" scale "$kind" "$name" --replicas="$replicas" >/dev/null
	done <"$SCALE_FILE"
	rm -f "$SCALE_FILE"
}

wait_parked_ready() {
	local entry kind ns name replicas deadline ready
	deadline=$((SECONDS + READY_TIMEOUT_SECONDS))
	for entry in "${PARKED[@]}"; do
		IFS=/ read -r kind ns name <<<"$entry"
		replicas=$(current_replicas "$kind" "$ns" "$name")
		[[ $replicas == 0 ]] && continue
		log "[$entry] waiting for $replicas ready..."
		while ((SECONDS < deadline)); do
			ready=$("$KUBECTL" -n "$ns" get "$kind" "$name" -o jsonpath='{.status.readyReplicas}' 2>/dev/null || true)
			[[ ${ready:-0} == "$replicas" ]] && break
			sleep 10
		done
		[[ ${ready:-0} == "$replicas" ]] || warn "[$entry] only ${ready:-0}/$replicas ready after ${READY_TIMEOUT_SECONDS}s"
	done
}

# --- Quieting the outage ---------------------------------------------------------

# SILENCE_DURATION in seconds; the window takes the silence's end, so only the
# single-unit forms up to SILENCE_MAX_SECONDS are accepted.
silence_seconds() {
	[[ $SILENCE_DURATION =~ ^([0-9]{1,7})([smhd])$ ]] || return 1
	local n=$((10#${BASH_REMATCH[1]})) s
	case ${BASH_REMATCH[2]} in
	s) s=$n ;;
	m) s=$((n * 60)) ;;
	h) s=$((n * 3600)) ;;
	d) s=$((n * 86400)) ;;
	esac
	((s > 0 && s <= SILENCE_MAX_SECONDS)) || return 1
	echo "$s"
}

# down's refusals that need no cluster: a duration the window cannot follow, or
# something other than a regular file where the window goes (mv would put the
# window inside a directory and succeed).
check_window() {
	silence_seconds >/dev/null ||
		die "SILENCE_DURATION=$SILENCE_DURATION: use <n>s, <n>m, <n>h or <n>d, at most 30d; this run changed nothing"
	if [[ -L $WINDOW_FILE || (-e $WINDOW_FILE && ! -f $WINDOW_FILE) ]]; then
		die "$WINDOW_FILE is not a regular file; remove it and re-run. This run changed nothing"
	fi
}

# Exactly one "<10-digit end epoch> <what>" line, the grammar
# maintenance-active.py accepts.
WINDOW_LINE=$'^([1-9][0-9]{9}) ([^[:cntrl:][:space:]][^[:cntrl:]]*)\n$'

line_end() { # <file>: the end such a file records; fails on anything else
	local text=''
	[[ -f $1 && ! -L $1 ]] || return 1
	IFS= read -r -d '' text <"$1" || true
	[[ $text =~ $WINDOW_LINE ]] || return 1
	printf '%s\n' "${BASH_REMATCH[1]}"
}

# The window ends with the recorded silence, whether this run made it or kept an
# earlier down's. Without a silence of known end (amtool failed), an open window
# is kept and anything else replaced by one of SILENCE_DURATION.
open_window() {
	local end seconds now
	now=$(date +%s)
	if [[ -s $SILENCE_FILE ]] && end=$(line_end "$SILENCE_END_FILE"); then
		if ((end <= now)); then
			close_window
			warn "the silence an earlier down made has ended; opening no maintenance window"
			return 0
		fi
	elif end=$(line_end "$WINDOW_FILE") && ((end > now)); then
		log "maintenance window already open"
		return 0
	else
		seconds=$(silence_seconds) || die "SILENCE_DURATION=$SILENCE_DURATION: use <n>s, <n>m, <n>h or <n>d, at most 30d"
		end=$((now + seconds))
	fi
	mkdir -p "$STATE_DIR"
	printf '%s %s\n' "$end" "planned timmy outage (timmy-offline.sh down)" >"$WINDOW_FILE.$$"
	mv -f "$WINDOW_FILE.$$" "$WINDOW_FILE"
}

close_window() {
	rm -f "$WINDOW_FILE"
}

amtool_exec() {
	"$KUBECTL" -n "$AM_NAMESPACE" exec "$AM_POD" -c alertmanager -- \
		amtool --alertmanager.url=http://localhost:9093 "$@"
}

# Best effort: a silence that cannot be created warns rather than blocking the
# outage, because the alternative is a noisy channel, not an unsafe power-off.
silence_alerts() {
	local id
	mkdir -p "$STATE_DIR"
	if [[ -s $SILENCE_FILE ]]; then
		log "alert silence already recorded ($(<"$SILENCE_FILE")) — not adding another"
		return 0
	fi
	if ! id=$(amtool_exec silence add --duration="$SILENCE_DURATION" \
		--author=timmy-offline.sh \
		--comment="planned timmy outage (timmy-offline.sh down); expired by 'up'" \
		'alertname=~".+"' 2>&1); then
		warn "could not create an Alertmanager silence: $id"
		warn "the outage will page Slack; continuing"
		return 0
	fi
	id=${id//[[:space:]]/}
	if [[ ! $id =~ ^[0-9a-f-]{36}$ ]]; then
		warn "unexpected amtool output, not recording a silence ID: $id"
		return 0
	fi
	printf '%s\n' "$id" >"$SILENCE_FILE"
	# Its end, for a later down's window (open_window); amtool took the same duration.
	printf '%s %s\n' "$(($(date +%s) + $(silence_seconds)))" "$id" >"$SILENCE_END_FILE"
	log "silenced all alerts for up to $SILENCE_DURATION (silence $id)"
}

expire_silence() {
	local id
	[[ -s $SILENCE_FILE ]] || {
		log "no recorded alert silence"
		return 0
	}
	id=$(<"$SILENCE_FILE")
	local out
	if out=$(amtool_exec silence expire "$id" 2>&1); then
		log "expired alert silence $id"
		rm -f "$SILENCE_FILE" "$SILENCE_END_FILE"
	elif [[ $out == *deleteSilenceNotFound* || $out == *'[404]'* ]]; then
		# Alertmanager's store is persistent, but it snapshots silences only every
		# 15 minutes and on a clean shutdown. A silence younger than that when
		# timmy loses power is gone on return (2026-10-21), and with it the cover
		# for the alert burst `down` created it for — so say so, then clear the ID
		# rather than retrying a delete that can never succeed.
		warn "silence $id no longer exists: Alertmanager came back without it, so the return burst was not covered (an unclean stop within its 15-minute snapshot interval loses a fresh silence)"
		rm -f "$SILENCE_FILE" "$SILENCE_END_FILE"
	else
		warn "could not expire silence $id — it ends on its own after $SILENCE_DURATION; retry: $SELF up"
	fi
}

# Record only the CronJobs this run suspended, so `up` never resumes one that
# was suspended on purpose before the outage.
suspend_cronjobs() {
	local entry ns name current
	mkdir -p "$STATE_DIR"
	touch "$SUSPEND_FILE"
	for entry in "${QUIESCED_CRONJOBS[@]}"; do
		ns=${entry%%/*}
		name=${entry#*/}
		if grep -qxF "$entry" "$SUSPEND_FILE"; then
			current=true # suspended by an earlier `down`; re-assert below
		else
			current=$("$KUBECTL" -n "$ns" get cronjob "$name" -o jsonpath='{.spec.suspend}') ||
				die "cannot read cronjob $entry"
			if [[ $current == true ]]; then
				log "[$entry] already suspended — leaving it for its owner"
				continue
			fi
			printf '%s\n' "$entry" >>"$SUSPEND_FILE"
		fi
		log "[$entry] suspend"
		"$KUBECTL" -n "$ns" patch cronjob "$name" --type=merge -p '{"spec":{"suspend":true}}' >/dev/null ||
			die "cannot suspend cronjob $entry"
	done
}

resume_cronjobs() {
	local entry ns name
	[[ -s $SUSPEND_FILE ]] || {
		log "no CronJobs suspended by down"
		rm -f "$SUSPEND_FILE"
		return 0
	}
	while IFS= read -r entry; do
		[[ -n $entry ]] || continue
		ns=${entry%%/*}
		name=${entry#*/}
		log "[$entry] resume"
		"$KUBECTL" -n "$ns" patch cronjob "$name" --type=merge -p '{"spec":{"suspend":false}}' >/dev/null ||
			die "cannot resume cronjob $entry (state kept in $SUSPEND_FILE)"
	done <"$SUSPEND_FILE"
	rm -f "$SUSPEND_FILE"
}

# The nightly datastore backup is pinned to timmy, so a window spanning 03:30
# skips it. One extra same-day run is harmless: it overwrites state-<date>.db.
catchup_backup() {
	local job
	job=k3s-datastore-backup-catchup-$(date +%Y%m%d%H%M)
	if "$KUBECTL" -n kube-system create job "$job" --from=cronjob/k3s-datastore-backup >/dev/null; then
		log "started catch-up backup job kube-system/$job"
	else
		warn "could not start a catch-up backup; run: kubectl -n kube-system create job <name> --from=cronjob/k3s-datastore-backup"
	fi
}

garage_check() {
	log "garage status"
	local out
	# A report, not a gate: under pipefail a failed exec here used to end up
	# silently, before the silence was expired.
	if ! out=$("$KUBECTL" -n garage exec garage-0 -c garage -- /garage status 2>&1); then
		warn "garage status unavailable (${out//$'\n'/ }); check it once garage-0 is Running"
		return 0
	fi
	sed -n '/HEALTHY NODES/,$p' <<<"$out" | sed 's/^/    /'
}

# This report's own health rule, not kubectl's STATUS column (whose printer has
# more branches than a copy here could keep up with). A pod is healthy when it
# is not being deleted and has Succeeded, or is Running, Ready (or its Ready
# condition has not been True for less than NOTREADY_GRACE_SECONDS, so pods
# still starting after `up` are left out), with no container problem and no
# container restarting RESTART_WARN_24H+ times in 24h ($restarts,
# {"<pod uid>": n} from restart_counts). Any other pod is listed with the first
# REASON that applies, in this order:
#   Terminating                        deletionTimestamp set
#   Init:<reason>, Init:ExitCode:<n>   an init container (native sidecars included)
#                                      waiting for a reason other than starting, or
#                                      exited non-zero
#   <reason>, ExitCode:<n>             likewise for a regular container
#                                      (CrashLoopBackOff, ImagePullBackOff, Error)
#   <.status.reason> or <phase>        not Running (Evicted, Pending, Failed, Unknown)
#   Running (<n> restarts in 24h)      a slow crashloop, Running between crashes
#   NotReady                           Ready not True (False, Unknown or absent)
#                                      beyond the grace
# READY counts regular containers and native sidecars; RESTARTS sums every
# container, init containers included. <reason> and <.status.reason> are the
# API's own strings; Terminating, the Init: prefix, ExitCode:<n>, the restart
# label and NotReady are this report's.
NOTREADY_GRACE_SECONDS=300
# shellcheck disable=SC2016 # jq program, not shell
POD_STATUS_JQ='
def starting: (.state.waiting.reason // "") | IN("ContainerCreating", "PodInitializing");
def problem:
	if .state.waiting.reason and (starting | not) then .state.waiting.reason
	elif .state.terminated and .state.terminated.exitCode != 0 then
		(.state.terminated.reason // "ExitCode:\(.state.terminated.exitCode)")
	else null end;
def since($t): try (now - ($t | fromdateiso8601)) catch 0;
def reason($r):
	([.status.conditions[]? | select(.type == "Ready")] | first) as $ready
	| if .metadata.deletionTimestamp then "Terminating"
	elif .status.phase == "Succeeded" then null
	else
		([.status.initContainerStatuses[]? | problem | select(. != null)] | first) as $init
		| ([.status.containerStatuses[]? | problem | select(. != null)] | first) as $main
		| if $init then "Init:\($init)"
		elif $main then $main
		elif .status.phase != "Running" then (.status.reason // .status.phase // "Unknown")
		elif $r > 0 then "Running (\($r) restarts in 24h)"
		elif ($ready.status // "") != "True"
			and since($ready.lastTransitionTime // .status.startTime // .metadata.creationTimestamp) >= $grace
			then "NotReady"
		else null end
	end;
def age:
	try ((now - (.metadata.creationTimestamp | fromdateiso8601)) | floor
		| if . >= 86400 then "\(. / 86400 | floor)d"
		elif . >= 3600 then "\(. / 3600 | floor)h"
		elif . >= 60 then "\(. / 60 | floor)m"
		else "\(.)s" end)
	catch "?";
.items[]
| reason($restarts[.metadata.uid // ""] // 0) as $s
| select($s != null)
| ([.spec.initContainers[]? | select(.restartPolicy == "Always") | .name]) as $sidecars
| ([.status.containerStatuses[]?] + [.status.initContainerStatuses[]? | select(.name | IN($sidecars[]))]) as $serving
| [.metadata.namespace, .metadata.name,
	"\([$serving[] | select(.ready)] | length)/\((.spec.containers // [] | length) + ($sidecars | length))",
	$s, ([.status.containerStatuses[]?, .status.initContainerStatuses[]? | .restartCount // 0] | add // 0 | tostring), age]
| @tsv'

# {"<pod uid>": n} for each pod with a container (init containers included) that restarted at least
# RESTART_WARN_24H times in the last 24h (n is that container's count), from
# Prometheus' kube-state-metrics history. A slow crashloop reads Running and ready
# between crashes, and a node reboot restarts every container on the node, so
# neither the pod's phase nor its last restart time tells them apart; the count
# over a day does. Read through the API server's service proxy: no exec, no
# port-forward. On failure prints the reason on stderr and returns 1.
restart_counts() {
	local query path out err_file err rc=0
	if [[ ! $RESTART_WARN_24H =~ ^[1-9][0-9]*$ ]]; then
		echo "RESTART_WARN_24H must be a positive integer, got '$RESTART_WARN_24H'" >&2
		return 1
	fi
	# By UID, not name: a recreated StatefulSet pod must not inherit its
	# predecessor's crashes. Init containers count too: a native sidecar's
	# restarts are only in the init-container series.
	query="max by (uid) (floor(increase(kube_pod_container_status_restarts_total[24h])) or floor(increase(kube_pod_init_container_status_restarts_total[24h]))) >= $RESTART_WARN_24H"
	path="/api/v1/namespaces/${PROM_SERVICE%%/*}/services/${PROM_SERVICE#*/}/proxy/api/v1/query?query=$(jq -rn --arg q "$query" '$q | @uri')"
	err_file=$(mktemp)
	out=$("$KUBECTL" get --raw "$path" 2>"$err_file") || rc=$?
	err=$(<"$err_file")
	rm -f "$err_file"
	if ((rc != 0)); then
		echo "${err//$'\n'/ }" >&2
		return 1
	fi
	jq -ce 'select(.status == "success")
		| [.data.result[] | select(.metric.uid) | {key: .metric.uid, value: (.value[1] | tonumber)}]
		| from_entries' <<<"$out" 2>/dev/null || {
		echo "Prometheus returned no query result" >&2
		return 1
	}
}

# Pods cluster-wide that are not healthy by POD_STATUS_JQ's rule (namespace,
# name, ready, reason, restarts, age). It reads the pod JSON, never kubectl's
# table, so a kubectl warning on stderr is never read as a pod. A report, never
# a gate: `up` has already restored service, and an unrelated
# crashloop (Longhorn's csi-provisioner at 93 restarts, found by hand on
# 2026-11-05, Running and ready between crashes) should surface at the next
# maintenance cycle without failing it. Without Prometheus (it is pinned to
# timmy) the restart history is skipped with a warning.
pod_health() {
	local json bad err_file err rc=0 restarts history=1
	err_file=$(mktemp)
	json=$("$KUBECTL" get pods -A -o json 2>"$err_file") || rc=$?
	err=$(<"$err_file")
	if ((rc != 0)); then
		rm -f "$err_file"
		warn "pod health unavailable (${err//$'\n'/ }); check with: kubectl get pods -A"
		return 0
	fi
	if ! restarts=$(restart_counts 2>"$err_file"); then
		warn "restart history unavailable ($(<"$err_file")); listing pods by status only. Prometheus restarts with timmy: for crashloops, rerun $SELF status once it answers"
		restarts='{}' history=0
	fi
	if ! jq -e '.items | arrays' >/dev/null 2>&1 <<<"$json"; then
		rm -f "$err_file"
		warn "pod health unavailable (kubectl did not return pod JSON); check with: kubectl get pods -A"
		return 0
	fi
	rc=0
	bad=$(jq -r --argjson restarts "$restarts" --argjson grace "$NOTREADY_GRACE_SECONDS" \
		"$POD_STATUS_JQ" <<<"$json" 2>"$err_file") || rc=$?
	err=$(<"$err_file")
	rm -f "$err_file"
	if ((rc != 0)); then
		warn "pod health unavailable (report failed: ${err//$'\n'/ }); check with: kubectl get pods -A"
		return 0
	fi
	if [[ -z $bad ]]; then
		if ((history)); then
			log "all pods healthy (Running and ready, or Completed), none restarting $RESTART_WARN_24H+ times in 24h"
		else
			log "all pods healthy (Running and ready, or Completed)"
		fi
		return 0
	fi
	warn "pods not healthy (not Running and ready, not Completed, or restarting $RESTART_WARN_24H+ times in 24h):"
	{
		printf 'NAMESPACE\tNAME\tREADY\tREASON\tRESTARTS\tAGE\n'
		printf '%s\n' "$bad"
	} | column -t -s $'\t' | sed 's/^/    /'
}

# --- Commands -------------------------------------------------------------------

cmd_down() {
	check_window
	log "preflight: Longhorn must be healthy before parking anything"
	"$NODE_MAINTENANCE" status >/dev/null
	# node-maintenance.sh refuses the drain while another cycle is active or an
	# earlier node still owes a reboot. Ask it first: a refusal after the silence,
	# suspend and park below leaves timmy half out with no instruction (2026-10-21).
	# The cycle module also owns the already-cordoned repeat path. Decide once,
	# from timmy's cordon and its owner when down starts, and pass that decision
	# on: a cordon that appears after this read then meets the strict preflight
	# or a bare drain, which refuse it. Only this Mac's outage cordon repeats;
	# any other refuses here, before any change (IMPR-1428).
	local preflight_args=() drain_args=(--spin-down --outage)
	if own_outage_cordon; then
		preflight_args=(--allow-repeat --outage)
		drain_args+=(--repeat)
	fi
	log "preflight: node-maintenance.sh must be willing to drain $NODE"
	"$NODE_MAINTENANCE" preflight "$NODE" ${preflight_args[@]+"${preflight_args[@]}"} ||
		die "node-maintenance.sh will not drain $NODE (see above); this run changed nothing"
	# Before park: parking Garage is what trips GarageQuorumLost.
	silence_alerts
	suspend_cronjobs
	# Before the first workload goes down, and after the steps above, so their
	# failure leaves no window while every service is still up (a silence they
	# made counts on its own).
	open_window
	park
	wait_cross_node_detached
	log "drain via node-maintenance.sh (no reboot, pre-drain spin-down)"
	# --spin-down is required here, not optional: it frees headroom for the
	# pods evicted from timmy before the memory-headroom preflight runs.
	# --repeat only when own_outage_cordon above saw this Mac's outage cordon.
	"$NODE_MAINTENANCE" drain "$NODE" "${drain_args[@]}" ||
		die "the drain did not finish (see above); fix that, then re-run: $SELF down"
	# The drain itself can create new orphans: any unpinned pod evicted from timmy
	# lands on another node and re-attaches its timmy-only volume from there. Check
	# again after the drain and fail closed — the pod's owner must be added to PARKED.
	log "post-drain: re-checking for volumes attached elsewhere with their only replica on $NODE"
	local orphans
	orphans=$(cross_node_orphans) || die "cannot read Longhorn state"
	if [[ -n $orphans ]]; then
		printf '%s\n' "$orphans" | sed 's/^/    /'
		die "NOT safe to power off — scale the owning workloads to 0 and add them to PARKED, then re-check with '$SELF status'"
	fi
	echo
	down_done "${1:-}"
}

# down's closing lines; `windows` (down called by cmd_windows) drops the
# power-off hint, since cmd_windows goes on to reboot timmy itself.
down_done() {
	if [[ ${1:-} == windows ]]; then
		log "timmy is parked, cordoned and drained."
		return 0
	fi
	log "timmy is parked, cordoned and drained. Power off when ready:"
	log "    ssh -t $NODE_SSH sudo shutdown -h now"
	log "or boot Windows once:  $SELF windows"
	log "When it is back on Ubuntu:  $SELF up"
}

# `windows`: down, then boot timmy into Windows exactly once. grub-reboot sets
# GRUB's one-shot next_entry, so the boot after Windows returns to Ubuntu
# (GRUB_DEFAULT=0). Both sudo commands are NOPASSWD grants on timmy pinned to
# these exact arguments (GUIDE-1070 § sudoers), so no TTY or password is needed;
# sudo -n fails instead of prompting when a grant is missing.
#
# Every failure after down ends with windows_way_back, which works whatever
# next_entry holds (a grub-reboot whose ssh dropped may have written it), so no
# message has to guess timmy's next boot.
windows_way_back() {
	printf '%s' "Next, either boot Windows: ssh -t $NODE_SSH 'sudo grub-reboot \"$WINDOWS_GRUB_ENTRY\" && sudo reboot', or return to Ubuntu: ssh -t $NODE_SSH 'sudo grub-editenv - unset next_entry && sudo reboot'. $SELF up needs a reboot since down (it checks the boot ID): run it once $NODE is back on Ubuntu."
}

cmd_windows() {
	local entry next out rc=0 esp
	# grub.cfg is root-only, so whether the pinned title still names a menu entry
	# cannot be read. Check what os-prober built it from instead, before draining:
	# the ESP it names is still /boot/efi and still holds the Windows loader. A
	# renumbered disk fails here; a reworded title would not.
	esp=$("$SSH" -o BatchMode=yes "$NODE_SSH" "findmnt -no SOURCE /boot/efi; if test -f $(printf '%q' "$WINDOWS_LOADER"); then echo loader-ok; fi") ||
		die "cannot reach $NODE over ssh to check the Windows boot entry; nothing was changed."
	[[ -n ${esp%%$'\n'*} && ${esp%%$'\n'*} != loader-ok ]] ||
		die "/boot/efi is not mounted on $NODE, so the Windows boot entry cannot be checked; nothing was changed. Mount it (ssh -t $NODE_SSH sudo mount /boot/efi), then re-run: $SELF windows"
	[[ $esp == "$WINDOWS_ESP"$'\n'loader-ok ]] ||
		die "the Windows GRUB entry may be stale, so nothing was changed: '$WINDOWS_GRUB_ENTRY' names $WINDOWS_ESP and $WINDOWS_LOADER, but $NODE reports /boot/efi on '${esp%%$'\n'*}'$([[ $esp == *loader-ok ]] || echo ' without that loader'). Check with: ssh -t $NODE_SSH sudo grep menuentry /boot/grub/grub.cfg; then update WINDOWS_GRUB_ENTRY, WINDOWS_ESP and the sudoers grant (GUIDE-1070 § sudoers)."
	cmd_down windows
	entry=$(printf '%q' "$WINDOWS_GRUB_ENTRY")
	log "setting $NODE's next boot (once) to: $WINDOWS_GRUB_ENTRY"
	"$SSH" -o BatchMode=yes "$NODE_SSH" "sudo -n /usr/sbin/grub-reboot $entry" ||
		die "grub-reboot failed on $NODE (is the NOPASSWD grant in /etc/sudoers.d/node-maintenance? GUIDE-1070 § sudoers); not rebooting: it is drained and still on Ubuntu. $(windows_way_back)"
	# grubenv is world-readable. Reading next_entry back proves the write and its
	# quoting landed; whether the title matches a menu entry is the preflight's
	# job above.
	next=$("$SSH" -o BatchMode=yes "$NODE_SSH" "grep '^next_entry=' /boot/grub/grubenv") ||
		die "cannot read $NODE's /boot/grub/grubenv after grub-reboot; not rebooting: it is drained and still on Ubuntu. $(windows_way_back)"
	[[ $next == "next_entry=$WINDOWS_GRUB_ENTRY" ]] ||
		die "$NODE's next boot entry reads '$next', not 'next_entry=$WINDOWS_GRUB_ENTRY'; not rebooting: it is drained and still on Ubuntu. Inspect: ssh $NODE_SSH cat /boot/grub/grubenv. $(windows_way_back)"
	log "rebooting $NODE"
	# ssh exits 255 both when the reboot drops the connection and when it never
	# connected, so its status proves nothing. The remote echoes reboot's own
	# status after it returns (systemd queues the reboot and returns at once);
	# only that line says the reboot was accepted.
	out=$("$SSH" -o BatchMode=yes "$NODE_SSH" "sudo -n /usr/sbin/reboot; echo reboot-rc=\$?" 2>&1) || rc=$?
	case $out in
	*reboot-rc=0*) ;;
	*reboot-rc=*)
		die "reboot refused on $NODE (${out//$'\n'/ }); not rebooted: it is drained and still on Ubuntu, with next_entry set to Windows. $(windows_way_back)"
		;;
	*)
		die "could not confirm $NODE accepted the reboot (ssh exit $rc: ${out//$'\n'/ }). Check whether it is going down: ssh $NODE_SSH uptime. If it went down, it boots next_entry (Windows, unless the title matches no menu entry); run $SELF up once it is back on Ubuntu. If it is still up on Ubuntu: $(windows_way_back)"
		;;
	esac
	log "timmy is rebooting with next_entry set to Windows. After the Windows session, reboot it back to Ubuntu, then:  $SELF up"
	log "If it comes back on Ubuntu without Windows, the title matched no menu entry (grub.cfg changed): run $SELF up, then fix WINDOWS_GRUB_ENTRY."
}

# Succeeds for this Mac's outage cordon, fails for an uncordoned timmy, and dies
# on any other cordon (by hand, a reboot or drain cycle, the other Mac's outage)
# or a failed or unexpected read. Call it in the calling shell (an `if`), never
# in a $( ), so the die ends the run.
own_outage_cordon() {
	local cordon
	cordon=$("$NODE_MAINTENANCE" cordon-status "$NODE" --outage) || die "cannot read whether $NODE is cordoned; this run changed nothing"
	case $cordon in
	owned) return 0 ;;
	uncordoned) return 1 ;;
	cordoned)
		die "$NODE is cordoned by hand (no owner recorded), not by this Mac's down; this run changed nothing. Only if it is this Mac's outage from before cordon owners were recorded, claim it and re-run: kubectl annotate node $NODE homelab/cordon-owner=\"outage:\$(scutil --get ComputerName)\" --overwrite"
		;;
	cordoned\ ?*)
		die "$NODE is cordoned by ${cordon#cordoned }, not by this Mac's down; this run changed nothing. Finish that work first (an outage: run '$SELF up' on the Mac that started it)"
		;;
	*) die "unexpected cordon status for $NODE: $cordon; this run changed nothing" ;;
	esac
}

cmd_up() {
	local cycle
	cycle=$("$NODE_MAINTENANCE" cycle-status "$NODE") || die "cannot read maintenance-cycle status for $NODE"
	case $cycle in
	recorded | none) ;;
	*) die "unexpected maintenance-cycle status for $NODE: $cycle" ;;
	esac
	if ! [[ -s $SCALE_FILE || -s $SUSPEND_FILE || -s $SILENCE_FILE || $cycle == recorded ]]; then
		# A down that stopped after opening the window, before recording a change.
		[[ -e $WINDOW_FILE ]] && close_window && log "closed the maintenance window; nothing else was recorded"
		die "nothing recorded by down (no parked, suspended, silence or cycle state); nothing to do"
	fi
	# Recovering through node-maintenance.sh alone leaves workloads parked,
	# CronJobs suspended and the silence active, so its retry hints name `up`.
	NODE_MAINTENANCE_RERUN="$SELF up" "$NODE_MAINTENANCE" recover "$NODE" ||
		die "node cycle not recovered (see above); nothing unparked — fix that, then re-run: $SELF up"
	unpark
	wait_parked_ready
	resume_cronjobs
	catchup_backup
	garage_check
	# Last: anything still firing after recovery is real and should page now.
	expire_silence
	close_window
	log "timmy is back in service."
	pod_health
}

cmd_status() {
	if [[ -s $SCALE_FILE ]]; then
		warn "parked workloads (restore with '$SELF up'):"
		sed 's/^/    /' "$SCALE_FILE"
	else
		log "nothing parked"
	fi
	if [[ -s $SUSPEND_FILE ]]; then
		warn "CronJobs suspended by down (resumed by '$SELF up'):"
		sed 's/^/    /' "$SUSPEND_FILE"
	fi
	if [[ -s $SILENCE_FILE ]]; then
		warn "alert silence active (expired by '$SELF up'): $(<"$SILENCE_FILE")"
	fi
	local orphans
	orphans=$(cross_node_orphans) || die "cannot read Longhorn state"
	if [[ -n $orphans ]]; then
		warn "volumes attached elsewhere whose only replica is on $NODE:"
		printf '%s\n' "$orphans" | sed 's/^/    /'
	else
		log "no cross-node volume depends solely on $NODE"
	fi
	# node-maintenance.sh status fails when it cannot read the nodes or Longhorn
	# (degraded volumes it only reports); list pods anyway, then keep its status.
	local rc=0
	"$NODE_MAINTENANCE" status || rc=$?
	pod_health
	return "$rc"
}

main() {
	case ${1:-} in
	down) cmd_down ;;
	windows) cmd_windows ;;
	up) cmd_up ;;
	status) cmd_status ;;
	*) usage ;;
	esac
}

# Sourcing (tests/test-timmy-offline-quiet.sh) defines functions only.
if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
	main "$@"
fi
