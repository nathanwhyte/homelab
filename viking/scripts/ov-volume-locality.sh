#!/usr/bin/env bash
# Apply (or read back) the Longhorn Volume settings OpenViking's claims need but
# cannot get from their immutable `longhorn-ssd` class: dataLocality best-effort
# and, for the prod pair, numberOfReplicas 2. See
# viking/manifests/storageclass-longhorn-ov.yaml for why.
#
# usage: ov-volume-locality.sh [--apply] [--replicas N] <pvc-name>...
#
#   default        read-only: resolve each PVC to its Longhorn Volume and print
#                  state, robustness, attached node, locality, replica count and
#                  the node of every replica. Exit 1 if any volume is not
#                  attached+healthy or has a replica off its attached node.
#   --apply        patch spec.dataLocality=best-effort (and spec.numberOfReplicas
#                  when --replicas is given), then wait up to 15 min for every
#                  volume to be healthy with a replica on its attached node.
#   --replicas N   with --apply: also set the replica count (prod pair: 2).
#
# Applied on 2026-09-22 as:
#   ov-volume-locality.sh --apply openviking-data ov-vectordb-data \
#       openviking-test-data ov-vectordb-test-data            # 17:43:45Z
#   ov-volume-locality.sh --apply --replicas 2 openviking-data ov-vectordb-data   # 18:05Z
set -euo pipefail

NS=${NS:-viking}
LH_NS=${LH_NS:-longhorn-system}
APPLY=0
REPLICAS=""
PVCS=()
while [ $# -gt 0 ]; do
	case "$1" in
	--apply) APPLY=1 ;;
	--replicas)
		shift
		REPLICAS=$1
		;;
	-h | --help)
		sed -n '2,22p' "$0"
		exit 0
		;;
	*) PVCS+=("$1") ;;
	esac
	shift
done
[ ${#PVCS[@]} -gt 0 ] || {
	echo "usage: $0 [--apply] [--replicas N] <pvc-name>..." >&2
	exit 2
}

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }

pv_of() { kubectl -n "$NS" get pvc "$1" -o jsonpath='{.spec.volumeName}'; }

# Prints "<state> <robustness> <attachedNode> <locality> <numberOfReplicas>".
vol_state() {
	kubectl -n "$LH_NS" get volumes.longhorn.io "$1" \
		-o jsonpath='{.status.state} {.status.robustness} {.status.currentNodeID} {.spec.dataLocality} {.spec.numberOfReplicas}'
}

# Prints one "<node> <currentState>" line per replica of the volume.
replica_nodes() {
	kubectl -n "$LH_NS" get replicas.longhorn.io -o json |
		python3 -c '
import json, sys
vol = sys.argv[1]
for r in json.load(sys.stdin)["items"]:
    if r["spec"]["volumeName"] == vol:
        print(r["spec"].get("nodeID") or "(unscheduled)", r["status"].get("currentState", "?"))
' "$1"
}

# Exit 0 when the volume is attached+healthy, every replica is running, and one
# replica sits on the attached node (the locality goal); 1 otherwise.
converged() {
	local pv=$1 state robust node _locality _n
	read -r state robust node _locality _n < <(vol_state "$pv")
	[ "$state" = attached ] && [ "$robust" = healthy ] || return 1
	local reps
	reps=$(replica_nodes "$pv")
	grep -qv ' running$' <<<"$reps" && return 1
	grep -q "^$node running$" <<<"$reps"
}

report() {
	local pvc=$1 pv=$2
	log "$pvc ($pv): $(vol_state "$pv" | awk '{printf "state=%s robustness=%s attached=%s locality=%s replicas=%s", $1, $2, $3, $4, $5}')"
	replica_nodes "$pv" | sed 's/^/    replica on /'
}

# Parallel indexed arrays (macOS ships bash 3.2, which has no associative arrays).
PVS=()
for pvc in "${PVCS[@]}"; do
	pv=$(pv_of "$pvc")
	[ -n "$pv" ] || {
		echo "error: PVC $NS/$pvc has no bound volume" >&2
		exit 1
	}
	PVS+=("$pv")
done

if [ "$APPLY" = 1 ]; then
	for i in "${!PVCS[@]}"; do
		patch='{"spec":{"dataLocality":"best-effort"'
		[ -n "$REPLICAS" ] && patch+=",\"numberOfReplicas\":$REPLICAS"
		patch+='}}'
		kubectl -n "$LH_NS" patch volumes.longhorn.io "${PVS[$i]}" --type merge -p "$patch" >/dev/null
		log "${PVCS[$i]}: patched $patch"
	done
	log "waiting up to 15 min for every volume to be healthy with a replica on its attached node"
	for _ in $(seq 1 90); do
		all=1
		for pv in "${PVS[@]}"; do converged "$pv" || all=0; done
		[ "$all" = 1 ] && break
		sleep 10
	done
fi

rc=0
for i in "${!PVCS[@]}"; do
	report "${PVCS[$i]}" "${PVS[$i]}"
	converged "${PVS[$i]}" || rc=1
done
if [ "$rc" = 0 ]; then
	log "OK: every volume healthy with a local replica"
else
	log "NOT converged (see above)"
fi
exit "$rc"
