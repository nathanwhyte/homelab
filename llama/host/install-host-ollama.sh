#!/usr/bin/env bash
# Install / reconcile the HOST Ollama daemon on timmy (IMPR-1075).
#
# Run ON timmy, from a checkout of this repo (needs sudo):
#
#   sudo llama/host/install-host-ollama.sh                # units + drop-in + warm + exporter
#   sudo llama/host/install-host-ollama.sh --sync-models  # copy models ONLY; no service changes
#   sudo llama/host/install-host-ollama.sh --sync-identity # copy cloud identity ONLY; host must be stopped
#   sudo llama/host/install-host-ollama.sh --check        # report only, change nothing
#
# What "install" does, idempotently:
#   1. Check the cutover preconditions, then verify /usr/local/bin/ollama is
#      exactly OLLAMA_VERSION (replacing the retired Deployment's latest tag). If not,
#      install that release with the upstream installer. Never floats.
#   2. Copy llama/host/ollama.service.d/homelab.conf to the drop-in dir.
#   3. Copy ollama-warm.sh + the agentpair Modelfiles to /opt/ollama-host and
#      install ollama-warm.service (PartOf=ollama.service).
#   4. Reconcile ollama-exporter.service from llama/ollama/ollama-exporter.py.
#   5. daemon-reload, enable everything, restart ollama.service, verify the
#      daemon is listening on 0.0.0.0:11434 and answers /api/version.
#
# ORDER OF OPERATIONS (see llama/host/README.md): copy models, apply the host
# Service, scale the pod to zero, and wait for old pods/NAT rules to disappear.
# Only then install/restart the host. ss cannot detect Kubernetes hostPort DNAT.
set -euo pipefail

OLLAMA_VERSION=${OLLAMA_VERSION:-0.33.3}
OLLAMA_BIN=/usr/local/bin/ollama
OLLAMA_USER=ollama
MODELS_DIR=/usr/share/ollama/.ollama/models
HOST_DIR=/opt/ollama-host
DROPIN_DIR=/etc/systemd/system/ollama.service.d
PVC_NAME=${PVC_NAME:-pvc-5558663d-4631-446f-950e-bedd15b920c3}
REPO_DIR=${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}
SRC=$REPO_DIR/llama/host

mode=install
for arg in "$@"; do
	case $arg in
	--sync-models) mode=sync ;;
	--sync-identity) mode=identity ;;
	--check) mode=check ;;
	-h | --help)
		sed -n '2,25p' "$0"
		exit 0
		;;
	*)
		echo "unknown argument: $arg" >&2
		exit 2
		;;
	esac
done

log() { printf '==> %s\n' "$*"; }
die() {
	printf 'ERROR: %s\n' "$*" >&2
	exit 1
}

[[ $EUID -eq 0 ]] || die "run with sudo"
[[ -f $SRC/ollama.service.d/homelab.conf ]] || die "drop-in not found at $SRC — run from a repo checkout"
[[ $(hostname -s) == timmy ]] || die "this script is for timmy (hostname is $(hostname -s))"

# --- 0. Report ---------------------------------------------------------------
installed_version() {
	[[ -x $OLLAMA_BIN ]] || {
		echo none
		return
	}
	# An online --version reports the SERVER version, not necessarily this binary.
	OLLAMA_HOST=http://127.0.0.1:1 "$OLLAMA_BIN" --version 2>&1 | awk '/client version is/ {print $NF}'
}

cutover_preflight() {
	# hostPort is implemented by DNAT: ss alone cannot see it. API failures
	# must abort before the upstream installer or any unit/file mutation.
	local service pods svclb slices rules listeners
	service=$(k3s kubectl --request-timeout=15s -n llama get service ollama -o json) || die "cannot inspect ollama Service"
	python3 -c 'import json,sys
s=json.load(sys.stdin)["spec"]
if s["type"] != "ClusterIP" or s.get("selector") or s.get("externalIPs"):
    sys.exit("apply ollama-service.yaml first")
' <<<"$service" || die "Service still routes to the pod or owns an external IP"
	pods=$(k3s kubectl --request-timeout=15s -n llama get pods -l app=ollama -o json) || die "cannot inspect old Ollama pods"
	python3 -c 'import json,sys
if any(c["name"] == "ollama" for p in json.load(sys.stdin)["items"] for c in p["spec"]["containers"]):
    sys.exit("scale deployment/ollama to zero and wait for pod deletion")
' <<<"$pods" || die "old Ollama pod must release the GPU before host startup"
	svclb=$(k3s kubectl --request-timeout=15s -n kube-system get pods -l svccontroller.k3s.cattle.io/svcname=ollama -o name) || die "cannot inspect ServiceLB pods"
	[[ -z $svclb ]] || die "wait for svclb-ollama pods to disappear"
	slices=$(k3s kubectl --request-timeout=15s -n llama get endpointslice -l kubernetes.io/service-name=ollama -o json) || die "cannot inspect Ollama routes"
	python3 -c 'import json,sys
items=json.load(sys.stdin)["items"]
if len(items) != 1 or items[0]["metadata"]["name"] != "ollama-timmy-host":
    sys.exit("remove legacy Endpoints and old pod EndpointSlices")
if items[0]["endpoints"][0]["addresses"] != ["192.168.1.19"]:
    sys.exit("unexpected host route")
' <<<"$slices" || die "Ollama routes still include a stale or unexpected endpoint"
	rules=$(iptables-save -t nat) || die "cannot inspect host NAT rules"
	if grep -Eq -- '--dport (11434|9111) .* -j DNAT|--dport (11434|9111) -j DNAT|llama/ollama:.*(external IP|loadbalancer IP)' <<<"$rules"; then
		die "old Ollama DNAT rules still exist; wait for cluster networking reconciliation"
	fi
	listeners=$(ss -Hltnp) || die "cannot inspect listeners"
	if awk '$4 ~ /:11434$/ && $0 !~ /users:\(\("ollama"/' <<<"$listeners" | grep -q .; then
		die "another process owns port 11434"
	fi
}

log "ollama binary: $(installed_version) (pinned $OLLAMA_VERSION)"
log "ollama.service: $(systemctl is-enabled ollama 2>/dev/null || true) / $(systemctl is-active ollama 2>/dev/null || true)"
log "listeners on 11434:"
ss -Hltnp 2>/dev/null | awk '$4 ~ /:11434$/' | sed 's/^/    /' || true
log "model store: $MODELS_DIR ($(du -sh "$MODELS_DIR" 2>/dev/null | cut -f1 || echo absent))"
[[ $mode == check ]] && exit 0

# --- 1. Model store sync ONLY (safe while the old pod serves) -----------------
if [[ $mode == sync || $mode == identity ]]; then
	id "$OLLAMA_USER" >/dev/null 2>&1 || die "user $OLLAMA_USER missing"
	mount_path=$(findmnt -rn -o TARGET --source "/dev/longhorn/$PVC_NAME" 2>/dev/null | grep '/var/lib/kubelet/pods/' | head -1 || true)
	[[ -n $mount_path ]] || die "Longhorn volume $PVC_NAME is not mounted on this node (is the ollama pod still scheduled here?)"
	[[ -d $mount_path/models ]] || die "$mount_path/models missing"
	if [[ $mode == identity ]]; then
		[[ $(systemctl is-active ollama || true) == inactive ]] || die "stop ollama before copying its identity"
		[[ -s $mount_path/id_ed25519 && -s $mount_path/id_ed25519.pub ]] || die "source cloud identity missing"
		identity_backup=$(mktemp -d /var/backups/ollama-identity.XXXXXX)
		for key in id_ed25519 id_ed25519.pub; do
			if [[ -f $(dirname "$MODELS_DIR")/$key ]]; then
				cp -p "$(dirname "$MODELS_DIR")/$key" "$identity_backup/"
			fi
			install -o "$OLLAMA_USER" -g "$OLLAMA_USER" -m 0600 "$mount_path/$key" "$(dirname "$MODELS_DIR")/$key"
		done
		log "cloud identity copied; previous identity preserved in $identity_backup; start the host separately"
		exit 0
	fi
	log "syncing $mount_path/models -> $MODELS_DIR (rsync, blobs are content-addressed so reruns are cheap)"
	mkdir -p "$MODELS_DIR"
	rsync -a --exclude='*-partial*' --info=stats2 "$mount_path/models/" "$MODELS_DIR/"
	chown -R "$OLLAMA_USER:$OLLAMA_USER" "$(dirname "$MODELS_DIR")"
	log "model copy complete; no units, binaries, or services changed"
	exit 0
fi

# --- 2. Fail closed BEFORE any install/restart -------------------------------
cutover_preflight
if [[ $(installed_version) != "$OLLAMA_VERSION" ]]; then
	log "installing ollama $OLLAMA_VERSION"
	curl -fsSL https://ollama.com/install.sh | OLLAMA_VERSION=$OLLAMA_VERSION sh
	[[ $(installed_version) == "$OLLAMA_VERSION" ]] || die "install did not produce $OLLAMA_VERSION"
fi
id "$OLLAMA_USER" >/dev/null 2>&1 || die "user $OLLAMA_USER missing (the upstream installer creates it)"
for grp in video render; do
	id -nG "$OLLAMA_USER" | tr ' ' '\n' | grep -qx "$grp" || usermod -aG "$grp" "$OLLAMA_USER"
done

# --- 3. Units, drop-in, warm script, Modelfiles -------------------------------
install -d -m 0755 "$DROPIN_DIR" "$HOST_DIR" "$HOST_DIR/modelfiles"
install -m 0644 "$SRC/ollama.service.d/homelab.conf" "$DROPIN_DIR/homelab.conf"
install -m 0755 "$SRC/ollama-warm.sh" "$HOST_DIR/ollama-warm.sh"
install -m 0644 "$REPO_DIR"/llama/ollama/agentpair-*.Modelfile "$HOST_DIR/modelfiles/"
install -m 0644 "$SRC/ollama-warm.service" /etc/systemd/system/ollama-warm.service

# --- 4. Exporter (same script the pod sidecar ran) ----------------------------
install -d -m 0755 /opt/ollama-exporter
install -m 0644 "$REPO_DIR/llama/ollama/ollama-exporter.py" /opt/ollama-exporter/ollama-exporter.py
cat >/etc/systemd/system/ollama-exporter.service <<'EOF'
[Unit]
Description=Prometheus exporter for Ollama inference metrics
After=ollama.service
Wants=ollama.service

[Service]
Type=simple
User=ollama
Group=ollama
ExecStart=/usr/bin/python3 /opt/ollama-exporter/ollama-exporter.py --ollama http://localhost:11434 --port 9111
Restart=always
RestartSec=5
MemoryMax=128M
CPUQuota=20%

[Install]
WantedBy=multi-user.target
EOF

# --- 5. Activate and verify ---------------------------------------------------
systemctl daemon-reload
systemctl enable ollama ollama-warm ollama-exporter >/dev/null
log "restarting ollama.service (+ ollama-warm via PartOf)"
# The warm oneshot can take minutes. Queue it and verify API readiness separately.
systemctl restart --no-block ollama
for _tick in $(seq 1 30); do
	if systemctl is-active --quiet ollama && curl -fsS -m 2 http://127.0.0.1:11434/api/version >/dev/null; then
		break
	fi
	sleep 1
done
systemctl is-active --quiet ollama || die "ollama.service failed to start: $(journalctl -u ollama -n 5 --no-pager | tail -3)"
if ! ss -Hltn | awk '$4 ~ /^(0\.0\.0\.0|\*):11434$/' | grep -q .; then
	die "ollama is not listening on 0.0.0.0:11434 — journal: $(journalctl -u ollama -n 3 --no-pager | tail -1)"
fi
systemctl restart ollama-exporter
log "version: $(curl -fsS -m 5 http://127.0.0.1:11434/api/version)"
log "warm unit: $(systemctl is-active ollama-warm || true) (follow with: journalctl -fu ollama-warm)"
log "done. Verify from the cluster: kubectl -n llama run -it --rm probe --image=curlimages/curl:8.11.1 --restart=Never -- -fsS http://ollama.llama.svc:11434/api/tags"
