#!/usr/bin/env bash
# Install / reconcile the HOST Ollama daemon on timmy (IMPR-1075).
#
# Run ON timmy, from a checkout of this repo (needs sudo):
#
#   sudo llama/host/install-host-ollama.sh                # units + drop-in + warm + exporter
#   sudo llama/host/install-host-ollama.sh --sync-models  # copy models ONLY; no service changes
#   sudo llama/host/install-host-ollama.sh --sync-identity # copy cloud identity ONLY; host must be stopped
#   sudo llama/host/install-host-ollama.sh --check        # report only, change nothing
#   sudo llama/host/install-host-ollama.sh --files-only   # unchanged posture; no restart
#   sudo llama/host/install-host-ollama.sh --bootstrap-recovery # explicit offline recovery
#
# --files-only compares complete effective recipes and posture through Ollama,
# and refuses unit/directive changes. A legacy installation needs full migration.
# A normal install requires the current API and prepares before host mutation.
# --bootstrap-recovery explicitly permits setup while the API is unavailable;
# it still requires the same complete recipe and runtime proof after startup.
#
# What "install" does, idempotently:
#   1. Check the cutover preconditions, then verify /usr/local/bin/ollama is
#      exactly OLLAMA_VERSION (replacing the retired Deployment's latest tag). If not,
#      install that release's base tarball (sha256-verified; no ROCm). Never floats.
#      A fresh host also gets the ollama user and the base ollama.service unit.
#   2. Copy llama/host/ollama.service.d/homelab.conf to the drop-in dir.
#   3. Copy the preparation module and all Modelfiles to /opt/ollama-host and
#      install ollama-warm.service (PartOf=ollama.service).
#   4. Reconcile ollama-exporter.service from llama/ollama/ollama-exporter.py.
#      Also install the udev rule that keeps the 9070 XT out of runtime suspend
#      and trigger it (BUG-1242); --files-only does this too, since it restarts
#      nothing.
#   5. daemon-reload, enable everything, restart ollama.service, verify the
#      selected models and actual daemon/runner settings against a fresh receipt.
#
# ORDER OF OPERATIONS (see llama/host/README.md): copy models, apply the host
# Service, scale the pod to zero, and wait for old pods/NAT rules to disappear.
# Only then install/restart the host. ss cannot detect Kubernetes hostPort DNAT.
set -euo pipefail

OLLAMA_USER=ollama
PVC_NAME=${PVC_NAME:-pvc-5558663d-4631-446f-950e-bedd15b920c3}
REPO_DIR=${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}
SRC=$REPO_DIR/llama/host

mode=install
[[ $# -le 1 ]] || { echo 'ERROR: at most one mode may be selected' >&2; exit 2; }
for arg in "$@"; do
	case $arg in
	--sync-models) mode=sync ;;
	--sync-identity) mode=identity ;;
	--check) mode=check ;;
	--files-only) mode=files ;;
	--bootstrap-recovery) mode=recovery ;;
	-h | --help)
		sed -n '2,34p' "$0"
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
validate_lock() {
	python3 - "$@" <<'PY'
import os, stat, sys
fd, path = int(sys.argv[1]), sys.argv[2]
actual, expected = os.fstat(fd), os.lstat(path)
if (not stat.S_ISREG(actual.st_mode) or not stat.S_ISREG(expected.st_mode)
        or actual.st_uid != os.geteuid() or actual.st_mode & 0o022
        or (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino)):
    sys.exit("installer lock must be owned by the installer and not group/world-writable")
if len(sys.argv) == 4:
    os.fchmod(fd, int(sys.argv[3], 8))
PY
}
initialize_state_directory() {
	python3 - "$STATE_DIR" <<'PY'
import os, pathlib, stat, sys
path = pathlib.Path(sys.argv[1])
def create(directory):
    try:
        directory.mkdir(mode=0o755)
    except FileNotFoundError:
        create(directory.parent)
        create(directory)
    except FileExistsError:
        pass
create(path)
def validate(info):
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o022):
        sys.exit("preparation state must be a nonsymlink directory owned by the installer and not group/world-writable")
expected = path.lstat()
validate(expected)
fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try:
    actual = os.fstat(fd)
    validate(actual)
    if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
        sys.exit("preparation state changed during validation")
    # Existing private state can become readable only after its trust check.
    os.fchmod(fd, 0o755)
finally:
    os.close(fd)
PY
}
# Empty directive output is valid; a read error is not an empty unit.
directives() {
	local status=0
	grep -vE '^[[:space:]]*(#|;|$)' "$@" || status=$?
	[[ $status -le 1 ]] || return "$status"
}
compare_directives() {
	local label=$1 old=$2 new=$3 staged status
	if staged=$(diff <(printf '%s\n' "$old") <(printf '%s\n' "$new") 2>&1); then
		return
	else
		status=$?
	fi
	if [[ $status -eq 1 ]]; then
		die "--files-only refuses effective unit changes in $label; run full adoption. Staged change:
$staged"
	fi
	die "--files-only comparison failed for $label (diff exit $status): $staged"
}

# The integration harness gets a redirected filesystem ONLY as a normal user.
# Root must never honor the harness's caller-supplied destination.
ROOT=
if [[ -n ${HOMELAB_INSTALL_TEST_ROOT:-} ]]; then
	[[ $EUID -ne 0 ]] || die "test root is forbidden when running as root"
	[[ $HOMELAB_INSTALL_TEST_ROOT == /* && $HOMELAB_INSTALL_TEST_ROOT != / && -d $HOMELAB_INSTALL_TEST_ROOT ]] || die "test root must be an existing absolute directory"
	ROOT=${HOMELAB_INSTALL_TEST_ROOT%/}
else
	[[ $EUID -eq 0 ]] || die "run with sudo"
fi
OLLAMA_BIN=$ROOT/usr/local/bin/ollama
MODELS_DIR=$ROOT/usr/share/ollama/.ollama/models
HOST_DIR=$ROOT/opt/ollama-host
UNIT_DIR=$ROOT/etc/systemd/system
DROPIN_DIR=$UNIT_DIR/ollama.service.d
STATE_DIR=$ROOT/var/lib/ollama-preparation
EXPORTER_DIR=$ROOT/opt/ollama-exporter
UDEV_RULE=$ROOT/etc/udev/rules.d/99-homelab-dgpu-runpm.rules
DGPU=$ROOT/sys/bus/pci/devices/0000:03:00.0
[[ -f $SRC/ollama.service.d/homelab.conf ]] || die "drop-in not found at $SRC — run from a repo checkout"
[[ $(hostname -s) == timmy ]] || die "this script is for timmy (hostname is $(hostname -s))"
prepare() {
	local options=(--config "$SRC/model-posture.json" --recipes "$REPO_DIR/llama/ollama" \
		--url http://127.0.0.1:11434 --state "$STATE_DIR")
	if [[ ${preparation_locked:-0} == 1 ]]; then options+=(--lock-fd 8); fi
	python3 "$SRC/ollama_prepare.py" "${options[@]}" "$@"
}
pinned_version=$(prepare version) || die "cannot read the declared Ollama version"
[[ -z ${OLLAMA_VERSION:-} || $OLLAMA_VERSION == "$pinned_version" ]] || die "OLLAMA_VERSION contradicts model-posture.json ($pinned_version)"
OLLAMA_VERSION=$pinned_version

# --- 0. Report ---------------------------------------------------------------
installed_version() {
	[[ -x $OLLAMA_BIN ]] || {
		echo none
		return
	}
	# An online --version reports the SERVER version, not necessarily this binary.
	OLLAMA_HOST=http://127.0.0.1:1 "$OLLAMA_BIN" --version 2>&1 | awk '/client version is/ {print $NF}'
}

# The pinned release's BASE tarball, not the upstream install.sh (IDEA-1105).
# install.sh adds the 1 GB ROCm tarball on any AMD GPU (timmy serves Vulkan,
# which ships in the base ollama-linux-amd64 tarball; the ROCm one holds only
# rocm_v7_2) and rewrites + restarts ollama.service, while this script owns the
# unit, the drop-in, and the single restart in step 5. Download and verify fully
# before replacing anything. The dotfiles `timmy` branch's auto-install.sh does
# the same with OLLAMA_LINUX_VERSION — keep the two pins equal.
install_release_tarball() {
	local version=$1 arch url tmp want got
	case $(uname -m) in
	x86_64) arch=amd64 ;;
	aarch64 | arm64) arch=arm64 ;;
	*) die "unsupported arch $(uname -m)" ;;
	esac
	command -v zstd >/dev/null 2>&1 || apt-get install -y zstd
	url=https://github.com/ollama/ollama/releases/download/v$version
	tmp=$(mktemp -d)
	curl -fsSL -o "$tmp/ollama.tar.zst" "$url/ollama-linux-$arch.tar.zst" || die "download failed: $url/ollama-linux-$arch.tar.zst"
	curl -fsSL -o "$tmp/sha256sum.txt" "$url/sha256sum.txt" || die "download failed: $url/sha256sum.txt"
	want=$(awk -v f="ollama-linux-$arch.tar.zst" '$2 == f || $2 == "./" f || $2 == "*" f {print $1}' "$tmp/sha256sum.txt")
	got=$(sha256sum "$tmp/ollama.tar.zst" | awk '{print $1}')
	[[ -n $want && $want == "$got" ]] || die "ollama-linux-$arch.tar.zst sha256 mismatch (want '${want:-missing}', got '$got'); install unchanged"
	rm -rf "$ROOT/usr/local/lib/ollama"
	zstd -dc "$tmp/ollama.tar.zst" | tar -xf - -C "$ROOT/usr/local"
	rm -rf "$tmp"
}

# Base unit for a FRESH host only (the upstream installer used to create it).
# An existing unit is left alone; homelab.conf carries every setting that matters.
ensure_base_unit() {
	[[ -f $UNIT_DIR/ollama.service ]] && return 0
	log "writing base /etc/systemd/system/ollama.service"
	install -d -m 0755 "$UNIT_DIR"
	cat >"$UNIT_DIR/ollama.service" <<EOF
[Unit]
Description=Ollama Service
After=network-online.target

[Service]
ExecStart=/usr/local/bin/ollama serve
User=$OLLAMA_USER
Group=$OLLAMA_USER
Restart=always
RestartSec=3
Environment="PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

[Install]
WantedBy=default.target
EOF
}

exporter_unit() {
	cat <<'EOF'
[Unit]
Description=Prometheus exporter for Ollama inference metrics
After=ollama.service
Wants=ollama.service

[Service]
Type=simple
User=ollama
Group=ollama
ExecStart=/usr/bin/python3 /opt/ollama-exporter/ollama-exporter.py --ollama http://localhost:11434 --port 9111 --preparation-state /var/lib/ollama-preparation
Restart=always
RestartSec=5
MemoryMax=128M
CPUQuota=20%

[Install]
WantedBy=multi-user.target
EOF
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
log "9070 XT runtime PM: control=$(cat "$DGPU/power/control" 2>/dev/null || echo ?) status=$(cat "$DGPU/power/runtime_status" 2>/dev/null || echo ?) (want on / active)"
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
		identity_backup=$(mktemp -d "$ROOT/var/backups/ollama-identity.XXXXXX")
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

# --- 2. Fail closed BEFORE any host install/restart --------------------------
if [[ $mode == install ]]; then
	curl -fsS -m 3 http://127.0.0.1:11434/api/version >/dev/null 2>&1 || die "Ollama is unreachable; no host files changed. Use --bootstrap-recovery for explicit recovery with full verification"
fi
# Serialize the complete host transaction, not just the core's model operations.
# A normal unavailable-daemon refusal above does not create even this lock file.
install_lock=$ROOT/run/lock/ollama-install.lock
[[ ! -L $install_lock ]] || die "installer lock must not be a symlink"
previous_umask=$(umask)
umask 077
exec 9>>"$install_lock"
umask "$previous_umask"
validate_lock 9 "$install_lock" || die "unsafe installer lock"
flock -n 9 || die "another host adoption is running; retry after it finishes"
for module in ollama_prepare.py ollama_recovery.py ollama_runtime.py ollama_attestation.py; do
	[[ -f $SRC/$module ]] || die "preparation module is incomplete: $module"
done
prepare render --root "$REPO_DIR" --check || die "generated adoption artifacts must match the declared posture"
if [[ $mode == recovery ]]; then
	cutover_preflight
	# Stop the old warm cgroup before waiting on its lock. Its orphaned create
	# child may still hold that lock after a cancelled parent.
	log "explicit bootstrap/recovery: stopping managed units before acquiring preparation lock"
	for unit in ollama-warm ollama; do
		load_state=$(systemctl show "$unit" --property=LoadState --value) || die "cannot inspect $unit for recovery"
		case $load_state in
		not-found) ;;
		loaded | masked) systemctl stop "$unit" || die "could not stop $unit for recovery" ;;
		*) die "unexpected $unit load state: $load_state" ;;
		esac
	done
fi
initialize_state_directory || die "preparation state was not adopted; inspect $STATE_DIR and explicitly correct its ownership, type or permissions before retrying"
[[ ! -L $STATE_DIR/preparation.lock ]] || die "preparation lock must not be a symlink"
previous_umask=$(umask)
umask 077
exec 8>>"$STATE_DIR/preparation.lock"
umask "$previous_umask"
validate_lock 8 "$STATE_DIR/preparation.lock" || die "unsafe preparation lock"
flock 8 || die "cannot acquire the shared preparation lock"
# Widen only a validated, acquired descriptor. An interrupted creation leaves
# a safe private file, which the next trusted owner can make readable again.
validate_lock 8 "$STATE_DIR/preparation.lock" 0644 || die "cannot set preparation lock readability"
preparation_locked=1
if [[ $mode == files ]]; then
	[[ -f $UNIT_DIR/ollama.service && -f $HOST_DIR/model-posture.json ]] || die "--files-only needs a full migration to the preparation module first"
	for unit in ollama.service.d/homelab.conf ollama-warm.service; do
		[[ -f $UNIT_DIR/$unit ]] || die "--files-only refuses a missing installed unit: $unit; run full adoption"
		old_directives=$(directives "$UNIT_DIR/$unit") || die "cannot read installed unit directives: $unit"
		new_directives=$(directives "$SRC/$unit") || die "cannot read source unit directives: $unit"
		compare_directives "$unit" "$old_directives" "$new_directives"
	done
	[[ -f $UNIT_DIR/ollama-exporter.service ]] || die "--files-only refuses a missing exporter unit; run full adoption"
	old_directives=$(directives "$UNIT_DIR/ollama-exporter.service") || die "cannot read installed exporter unit"
	new_directives=$(exporter_unit | directives) || die "cannot read source exporter unit"
	compare_directives "ollama-exporter.service" "$old_directives" "$new_directives"
  prepare files-check --installed "$HOST_DIR" || die "--files-only refused; follow the diagnostic above: changed posture requires full adoption; an unreachable or wrong-version daemon requires --bootstrap-recovery"
	log "--files-only: complete recipes, posture and unit directives unchanged; nothing restarts"
else
	if [[ $mode != recovery ]]; then
		cutover_preflight
		log "verifying and preparing the declared recipes before host changes"
		prepare prepare || die "recipe preparation failed; host installation unchanged"
	fi
	if [[ $(installed_version) != "$OLLAMA_VERSION" ]]; then
		log "installing ollama $OLLAMA_VERSION (base tarball, no ROCm) over $(installed_version)"
		install_release_tarball "$OLLAMA_VERSION"
		[[ $(installed_version) == "$OLLAMA_VERSION" ]] || die "install did not produce $OLLAMA_VERSION"
	fi
	if ! id "$OLLAMA_USER" >/dev/null 2>&1; then
		log "creating system user $OLLAMA_USER"
		useradd -r -s /bin/false -U -m -d "$(dirname "$(dirname "$MODELS_DIR")")" "$OLLAMA_USER"
	fi
	ensure_base_unit
	for grp in video render; do
		id -nG "$OLLAMA_USER" | tr ' ' '\n' | grep -qx "$grp" || usermod -aG "$grp" "$OLLAMA_USER"
	done
fi

# Everything steps 3 and 4 write, so --files-only can report what actually changed.
installed_files=(
	"$DROPIN_DIR/homelab.conf"
	"$HOST_DIR/ollama-warm.sh"
	"$HOST_DIR/model-posture.json"
	"$HOST_DIR/ollama_prepare.py"
	"$HOST_DIR/ollama_recovery.py"
	"$HOST_DIR/ollama_runtime.py"
	"$HOST_DIR/ollama_attestation.py"
	"$UNIT_DIR/ollama-warm.service"
	"$EXPORTER_DIR/ollama-exporter.py"
	"$EXPORTER_DIR/ollama_recovery.py"
	"$EXPORTER_DIR/ollama_attestation.py"
	"$UNIT_DIR/ollama-exporter.service"
	"$UDEV_RULE"
)
for recipe in "$REPO_DIR"/llama/ollama/*.Modelfile; do
	installed_files+=("$HOST_DIR/modelfiles/${recipe##*/}")
done
checksums() {
	local f
	for f in "${installed_files[@]}"; do
		if [[ -f $f ]]; then sha256sum "$f"; else echo "missing  $f"; fi
	done
}
before=$(checksums)

# --- 3. Units, drop-in, warm script, Modelfiles -------------------------------
install -d -m 0755 "$DROPIN_DIR" "$HOST_DIR" "$HOST_DIR/modelfiles"
install -m 0644 "$SRC/ollama.service.d/homelab.conf" "$DROPIN_DIR/homelab.conf"
install -m 0755 "$SRC/ollama-warm.sh" "$HOST_DIR/ollama-warm.sh"
install -m 0644 "$SRC/model-posture.json" "$SRC/ollama_prepare.py" \
	"$SRC/ollama_recovery.py" "$SRC/ollama_runtime.py" "$SRC/ollama_attestation.py" "$HOST_DIR/"
# The agentpair:* tags were retired 2026-10-06; drop their stale recipes.
rm -f "$HOST_DIR"/modelfiles/agentpair-*.Modelfile
install -m 0644 "$REPO_DIR"/llama/ollama/*.Modelfile "$HOST_DIR/modelfiles/"
install -m 0644 "$SRC/ollama-warm.service" "$UNIT_DIR/ollama-warm.service"

# --- 4. Exporter (same script the pod sidecar ran) ----------------------------
install -d -m 0755 "$EXPORTER_DIR"
install -m 0644 "$REPO_DIR/llama/ollama/ollama-exporter.py" "$EXPORTER_DIR/ollama-exporter.py"
install -m 0644 "$SRC/ollama_recovery.py" "$SRC/ollama_attestation.py" "$EXPORTER_DIR/"
exporter_unit >"$UNIT_DIR/ollama-exporter.service"

# Runtime suspend evicts the resident model's VRAM to system RAM (BUG-1242).
# Triggering applies the rule now; it only resumes the card, so no restart.
install -m 0644 "$SRC/99-homelab-dgpu-runpm.rules" "$UDEV_RULE"
udevadm control --reload
udevadm trigger --action=add --subsystem-match=pci --attr-match=vendor=0x1002 --attr-match=device=0x7550
udevadm settle
[[ $(cat "$DGPU/power/control" 2>/dev/null) == on ]] || die "udev rule installed but $DGPU/power/control is not 'on'"

if [[ $mode == files ]]; then
	systemctl daemon-reload
	changed=$(diff <(printf '%s\n' "$before") <(checksums) | awk '/^>/ {print $NF}' || true)
	if [[ -n $changed ]]; then
		log "--files-only: installed files that changed:"
		while IFS= read -r file; do printf '    %s\n' "$file"; done <<<"$changed"
	else
		log "--files-only: every installed file already matched the repo"
	fi
	log "--files-only: nothing restarted. ollama.service keeps its running Environment= values and ollama-exporter its loaded code until the next restart; ollama-warm.service picks the new script up on its next run. Run without --files-only to restart."
	preparation_locked=0
	exec 8>&-
	exit 0
fi

# --- 5. Activate and verify ---------------------------------------------------
systemctl daemon-reload
systemctl enable ollama ollama-warm ollama-exporter >/dev/null
log "restarting ollama.service and waiting for verified model preparation"
# Clear RemainAfterExit from the previous generation. Starting a unit whose
# startup job is already queued joins that job and waits for its completion.
systemctl stop ollama-warm
# A stopped old warm unit cannot resume a stale waiter against copied files.
# Release before restarting: the new warm unit needs this same lock itself.
preparation_locked=0
exec 8>&-
systemctl restart ollama || die "daemon startup failed; inspect journalctl -u ollama and use --bootstrap-recovery after correction"
systemctl start ollama-warm || die "model preparation failed; inspect journalctl -u ollama-warm and use --bootstrap-recovery after correction"
systemctl is-active --quiet ollama-warm || die "warmup did not finish successfully"
prepare verify --stop-on-failure || die "current model/runtime verification failed; adoption is incomplete; use --bootstrap-recovery after correction"
systemctl is-active --quiet ollama || die "ollama.service failed to start: $(journalctl -u ollama -n 5 --no-pager | tail -3)"
if ! ss -Hltn | awk '$4 ~ /^(0\.0\.0\.0|\*):11434$/' | grep -q .; then
	die "ollama is not listening on 0.0.0.0:11434 — journal: $(journalctl -u ollama -n 3 --no-pager | tail -1)"
fi
systemctl restart ollama-exporter
log "version: $(curl -fsS -m 5 http://127.0.0.1:11434/api/version)"
log "done: current recipe, residency and runtime verification passed"
