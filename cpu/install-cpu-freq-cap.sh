#!/usr/bin/env bash
set -euo pipefail
# Install the temporary CPU frequency cap on manu (BUG-1101 cooling fault).
#
#   ssh -t manu-lan 'bash -s' < cpu/install-cpu-freq-cap.sh
#
# Needs an interactive sudo password, hence `ssh -t`. Idempotent: re-running
# re-installs the files and restarts the unit.
#
# To remove once the cooler is fixed:
#   ssh -t manu-lan 'sudo systemctl disable --now cpu-freq-cap'
# That restores full clocks immediately via ExecStop — no reboot needed.

SRC_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! -e /sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq ]]; then
	echo "No cpufreq sysfs on this host — nothing to cap." >&2
	exit 1
fi

echo "=== Before ==="
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq
echo "available: $(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_available_frequencies 2>/dev/null || echo '(driver exposes none)')"

echo "=== Installing /usr/local/sbin/cpu-freq-cap ==="
sudo install -m 0755 "$SRC_DIR/cpu-freq-cap" /usr/local/sbin/cpu-freq-cap

echo "=== Installing unit ==="
sudo install -m 0644 "$SRC_DIR/cpu-freq-cap.service" \
	/etc/systemd/system/cpu-freq-cap.service
sudo systemctl daemon-reload

echo "=== Enabling ==="
sudo systemctl enable --now cpu-freq-cap

echo "=== After ==="
sudo /usr/local/sbin/cpu-freq-cap status | head -5
systemctl is-enabled cpu-freq-cap
systemctl is-active cpu-freq-cap

cat <<'NOTE'

Installed. The cap now survives reboots.

Verify after the next reboot:
  ssh manu-lan 'sudo cpu-freq-cap status | head -3'

Remove when the cooler is repasted:
  ssh -t manu-lan 'sudo systemctl disable --now cpu-freq-cap'
NOTE
