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
sudo systemctl enable --now nvidia-power-cap

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
