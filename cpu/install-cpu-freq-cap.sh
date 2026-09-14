#!/usr/bin/env bash
set -euo pipefail
# Install the temporary CPU frequency cap on manu (BUG-1101 cooling fault).
#
# Copy the three files over first, then run it with a real terminal:
#
#   scp cpu/cpu-freq-cap cpu/cpu-freq-cap.service cpu/install-cpu-freq-cap.sh manu-lan:/tmp/
#   ssh -t manu-lan 'bash /tmp/install-cpu-freq-cap.sh'
#
# Do NOT pipe this over stdin (`ssh -t host 'bash -s' < script`). Two reasons:
# the script would have no sibling files to install, and ssh refuses to allocate
# a pty when stdin is a redirect — so sudo has no terminal to prompt on and the
# run dies at the first privileged step.
#
# Idempotent: re-running re-installs the files and restarts the unit.
#
# To remove once the cooler is fixed:
#   ssh -t manu-lan 'sudo systemctl disable --now cpu-freq-cap'
# That restores full clocks immediately via ExecStop — no reboot needed.

# BASH_SOURCE is unset under `bash -s`, and `set -u` makes that fatal, so guard
# it and fail with the real problem rather than an unbound-variable trace.
SRC_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]:-$0}")" && pwd)"

for f in cpu-freq-cap cpu-freq-cap.service; do
	[[ -f "$SRC_DIR/$f" ]] || {
		echo "Missing $SRC_DIR/$f — copy cpu-freq-cap, cpu-freq-cap.service and" >&2
		echo "this script into the same directory on the target host first. See" >&2
		echo "the usage comment at the top of this file." >&2
		exit 1
	}
done

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
