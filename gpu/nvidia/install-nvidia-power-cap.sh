#!/usr/bin/env bash
set -euo pipefail
# Install the GTX 1080 power cap on manu (BUG-1101 cooling fault).
#
# Copy the unit over first, then run this with a real terminal:
#
#   scp gpu/nvidia/nvidia-power-cap.service gpu/nvidia/install-nvidia-power-cap.sh manu-lan:/tmp/
#   ssh -t manu-lan 'bash /tmp/install-nvidia-power-cap.sh'
#
# Do NOT pipe over stdin (`ssh -t host 'bash -s' < script`): the script would
# arrive without its sibling unit file, and ssh refuses a pty when stdin is a
# redirect, so sudo has nowhere to prompt. Same trap as the cpu-freq-cap
# installer.
#
# Idempotent. To remove once the cooler is repasted:
#   ssh -t manu-lan 'sudo systemctl disable --now nvidia-power-cap'

SRC_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]:-$0}")" && pwd)"

[[ -f "$SRC_DIR/nvidia-power-cap.service" ]] || {
	echo "Missing $SRC_DIR/nvidia-power-cap.service — copy it alongside this" >&2
	echo "script on the target host first. See the usage comment above." >&2
	exit 1
}

command -v nvidia-smi >/dev/null || {
	echo "nvidia-smi not found — this host has no NVIDIA driver." >&2
	exit 1
}

echo "=== Before ==="
nvidia-smi --query-gpu=name,power.limit,power.draw,persistence_mode --format=csv

echo "=== Installing unit ==="
sudo install -m 0644 "$SRC_DIR/nvidia-power-cap.service" \
	/etc/systemd/system/nvidia-power-cap.service
sudo systemctl daemon-reload

echo "=== Enabling ==="
# enable --now does NOT restart a oneshot that is already active, so a rerun
# after changing CAP_WATTS — or after the limit drifted — would skip both the
# apply and the check below and still print "Installed." Restart explicitly.
sudo systemctl enable nvidia-power-cap
sudo systemctl restart nvidia-power-cap

echo "=== Verifying the cap actually applied ==="
want=$(systemctl show nvidia-power-cap -p Environment --value | tr ' ' '\n' |
	sed -n 's/^CAP_WATTS=//p')
got=$(nvidia-smi --query-gpu=power.limit --format=csv,noheader,nounits | cut -d. -f1)
if [[ "$got" != "$want" ]]; then
	echo "FAILED: unit asks for ${want}W, card reports ${got}W" >&2
	exit 1
fi
echo "  power limit ${got}W matches the unit's CAP_WATTS=${want}"

echo "=== After ==="
nvidia-smi --query-gpu=name,power.limit,power.draw,persistence_mode --format=csv
systemctl is-enabled nvidia-power-cap
systemctl is-active nvidia-power-cap

cat <<'NOTE'

Installed. The cap now survives reboots and driver unloads.

Verify after the next reboot:
  ssh manu-lan 'nvidia-smi --query-gpu=power.limit --format=csv,noheader'

Remove when the cooler is repasted, together with cpu-freq-cap:
  ssh -t manu-lan 'sudo systemctl disable --now nvidia-power-cap cpu-freq-cap'
NOTE
