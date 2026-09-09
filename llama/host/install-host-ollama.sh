#!/usr/bin/env bash
# Install / reconcile the HOST Ollama daemon on timmy (IMPR-1075).
#
# Run ON timmy, from a checkout of this repo (needs sudo):
#
#   sudo llama/host/install-host-ollama.sh                # units + drop-in + warm + exporter
#   sudo llama/host/install-host-ollama.sh --sync-models  # ...and copy the retired PVC's
#                                                         # model store onto the host first
#   sudo llama/host/install-host-ollama.sh --check        # report only, change nothing
#
# What "install" does, idempotently:
#   1. Verify /usr/local/bin/ollama is exactly OLLAMA_VERSION (the binary pin
#      that replaces the retired Deployment's image pin, IMPR-1023). If not,
#      install that release with the upstream installer. Never floats.
#   2. Copy llama/host/ollama.service.d/homelab.conf to the drop-in dir.
#   3. Copy ollama-warm.sh + the agentpair Modelfiles to /opt/ollama-host and
#      install ollama-warm.service (PartOf=ollama.service).
#   4. Reconcile ollama-exporter.service from llama/ollama/ollama-exporter.py.
#   5. daemon-reload, enable everything, restart ollama.service, verify the
#      daemon is listening on 0.0.0.0:11434 and answers /api/version.
#
# ORDER OF OPERATIONS for the cutover (see llama/host/README.md): apply
# llama/ollama-service.yaml to the cluster FIRST so klipper's svclb releases
# host port 11434; otherwise step 5's bind fails with EADDRINUSE (seen live on
# 2026-09-08 20:33). This script checks for that and refuses to restart.
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
	"$OLLAMA_BIN" --version 2>/dev/null | awk '{print $NF}'
}

svclb_holds_port() {
	# klipper's svclb pod binds host port 11434 for the old LoadBalancer Service.
	# Any listener on *:11434 that is not our daemon means the cluster still owns it.
	ss -Hltnp 2>/dev/null | awk '$4 ~ /:11434$/ && $4 !~ /^127\.0\.0\.1/' | grep -v 'ollama' || true
}

log "ollama binary: $(installed_version) (pinned $OLLAMA_VERSION)"
log "ollama.service: $(systemctl is-enabled ollama 2>/dev/null || true) / $(systemctl is-active ollama 2>/dev/null || true)"
log "listeners on 11434:"
ss -Hltnp 2>/dev/null | awk '$4 ~ /:11434$/' | sed 's/^/    /' || true
if [[ -n $(svclb_holds_port) ]]; then
	log "WARNING: something other than ollama holds *:11434 (klipper svclb?) — apply llama/ollama-service.yaml first"
fi
log "model store: $MODELS_DIR ($(du -sh "$MODELS_DIR" 2>/dev/null | cut -f1 || echo absent))"
[[ $mode == check ]] && exit 0

# --- 1. Binary pin -----------------------------------------------------------
if [[ $(installed_version) != "$OLLAMA_VERSION" ]]; then
	log "installing ollama $OLLAMA_VERSION"
	curl -fsSL https://ollama.com/install.sh | OLLAMA_VERSION=$OLLAMA_VERSION sh
	[[ $(installed_version) == "$OLLAMA_VERSION" ]] || die "install did not produce $OLLAMA_VERSION"
fi
id "$OLLAMA_USER" >/dev/null 2>&1 || die "user $OLLAMA_USER missing (the upstream installer creates it)"
for grp in video render; do
	id -nG "$OLLAMA_USER" | tr ' ' '\n' | grep -qx "$grp" || usermod -aG "$grp" "$OLLAMA_USER"
done

# --- 2. Model store sync (optional) ------------------------------------------
if [[ $mode == sync ]]; then
	mount_path=$(findmnt -rn -o TARGET --source "/dev/longhorn/$PVC_NAME" 2>/dev/null | grep '/var/lib/kubelet/pods/' | head -1 || true)
	[[ -n $mount_path ]] || die "Longhorn volume $PVC_NAME is not mounted on this node (is the ollama pod still scheduled here?)"
	[[ -d $mount_path/models ]] || die "$mount_path/models missing"
	log "syncing $mount_path/models -> $MODELS_DIR (rsync, blobs are content-addressed so reruns are cheap)"
	mkdir -p "$MODELS_DIR"
	rsync -a --info=progress2 "$mount_path/models/" "$MODELS_DIR/"
	chown -R "$OLLAMA_USER:$OLLAMA_USER" "$(dirname "$MODELS_DIR")"
fi

# --- 3. Units, drop-in, warm script, Modelfiles -------------------------------
install -d -m 0755 "$DROPIN_DIR" "$HOST_DIR" "$HOST_DIR/modelfiles"
install -m 0644 "$SRC/ollama.service.d/homelab.conf" "$DROPIN_DIR/homelab.conf"
install -m 0755 "$SRC/ollama-warm.sh" "$HOST_DIR/ollama-warm.sh"
install -m 0644 "$REPO_DIR"/llama/ollama/agentpair-*.Modelfile "$HOST_DIR/modelfiles/"
install -m 0644 "$SRC/ollama-warm.service" /etc/systemd/system/ollama-warm.service

# --- 4. Exporter (same script the pod sidecar ran) ----------------------------
install -d -m 0755 /opt/ollama-exporter
install -m 0644 "$REPO_DIR/llama/ollama/ollama-exporter.py" /opt/ollama-exporter/ollama-exporter.py
if [[ ! -f /etc/systemd/system/ollama-exporter.service ]]; then
	cat >/etc/systemd/system/ollama-exporter.service <<'EOF'
[Unit]
Description=Prometheus exporter for Ollama inference metrics
After=ollama.service
Wants=ollama.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/ollama-exporter/ollama-exporter.py --ollama http://localhost:11434 --port 9111
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
fi

# --- 5. Activate and verify ---------------------------------------------------
if [[ -n $(svclb_holds_port) ]]; then
	die "refusing to restart ollama: *:11434 is still held by another listener. Apply llama/ollama-service.yaml (selector-less Service) and wait for the svclb-ollama pod to disappear, then rerun."
fi
systemctl daemon-reload
systemctl enable ollama ollama-warm ollama-exporter >/dev/null
log "restarting ollama.service (+ ollama-warm via PartOf)"
systemctl restart ollama
sleep 3
systemctl is-active --quiet ollama || die "ollama.service failed to start: $(journalctl -u ollama -n 5 --no-pager | tail -3)"
if ! ss -Hltn | awk '$4 ~ /^(0\.0\.0\.0|\*):11434$/' | grep -q .; then
	die "ollama is not listening on 0.0.0.0:11434 — journal: $(journalctl -u ollama -n 3 --no-pager | tail -1)"
fi
systemctl restart ollama-exporter
log "version: $(curl -fsS -m 5 http://127.0.0.1:11434/api/version)"
log "warm unit: $(systemctl is-active ollama-warm) (follow with: journalctl -fu ollama-warm)"
log "done. Verify from the cluster: kubectl -n llama run -it --rm probe --image=curlimages/curl:8.11.1 --restart=Never -- -fsS http://ollama.llama.svc:11434/api/tags"
