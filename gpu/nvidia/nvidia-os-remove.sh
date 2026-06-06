#!/usr/bin/env bash
# nvidia-os-remove.sh — remove a host OS-level NVIDIA driver install on
# wemby so the NVIDIA GPU Operator (gpu-operator DaemonSet) can own the GPU.
#
# Run on the affected node as root. The script is step-by-step and stops
# between phases so you can verify the state before each one runs.
#
# CONTEXT
# The NVIDIA GPU Operator expects the host to NOT have a separately-installed
# nvidia driver. If both are present, kernel-ABI drift between the OS DKMS
# module and the operator's container-built module can cause version-mismatch
# failures. The fix is to purge the OS install, blacklist any leftover
# module-load config, and reboot.
#
# After this script completes, the GPU Operator's nvidia-driver-daemonset
# pod will load the container-built module on next boot and reclaim the GPU.
#
# USAGE
#   sudo viking/tools/nvidia-os-remove.sh inventory   # show what's installed
#   sudo viking/tools/nvidia-os-remove.sh purge       # stop services + apt purge + dkms remove
#   sudo viking/tools/nvidia-os-remove.sh cleanup     # remove modprobe/udev/initramfs artifacts
#   sudo viking/tools/nvidia-os-remove.sh reboot      # reboot (uses 'shutdown -r +1')
#   sudo viking/tools/nvidia-os-remove.sh all         # all of the above, with pauses
#
# Requires: bash 4+, apt, systemctl, modprobe, dkms, update-initramfs. Run as root.

set -euo pipefail

CMD="${1:-help}"

# Show help for --help/-h regardless of position
case "$CMD" in
  -h|--help) CMD="help" ;;
esac

if [ "$(id -u)" -ne 0 ] && [ "$CMD" != "help" ]; then
  echo "must be run as root" >&2
  exit 1
fi

banner() {
  printf '\n=== %s ===\n' "$1"
}

prompt() {
  local msg="$1"
  local resp
  read -r -p "$msg [type 'yes' to continue, anything else to abort]: " resp
  if [ "$resp" != "yes" ]; then
    echo "aborted by user"
    exit 0
  fi
}

# -------- inventory --------
do_inventory() {
  banner "OS / kernel"
  uname -a
  cat /etc/os-release | grep -E "^(NAME|VERSION|PRETTY_NAME)=" || true
  echo

  banner "apt: nvidia packages (dpkg --list)"
  dpkg -l | grep -i nvidia || echo "(none)"
  echo

  banner "apt: nvidia packages (manual-marked)"
  apt-mark showmanual 2>/dev/null | grep -i nvidia || echo "(none)"
  echo

  banner "DKMS status (nvidia modules)"
  dkms status 2>/dev/null | grep -i nvidia || echo "(no nvidia dkms modules)"
  echo

  banner "DKMS source tree (/usr/src/nvidia-*)"
  ls -la /usr/src/ 2>/dev/null | grep -i nvidia || echo "(none)"
  echo

  banner "/etc/modprobe.d/ nvidia* files"
  ls -la /etc/modprobe.d/ 2>/dev/null | grep -i nvidia || echo "(none)"
  echo

  banner "/etc/modules-load.d/ nvidia* entries"
  ls -la /etc/modules-load.d/ 2>/dev/null | grep -i nvidia || echo "(none)"
  echo

  banner "udev rules (nvidia*)"
  ls -la /lib/udev/rules.d/ 2>/dev/null | grep -i nvidia || echo "(none)"
  ls -la /etc/udev/rules.d/ 2>/dev/null | grep -i nvidia || echo "(none)"
  echo

  banner "systemd nvidia* services (running + enabled)"
  systemctl list-unit-files 2>/dev/null | grep -i nvidia || echo "(none)"
  echo "-- running --"
  systemctl list-units --type=service 2>/dev/null | grep -i nvidia || echo "(none running)"
  echo

  banner "Loaded nvidia kernel modules"
  lsmod | grep -i nvidia || echo "(none currently loaded — already unloaded, or OS install not active)"
  echo

  banner "Process file handles on /dev/nvidia*"
  if command -v lsof >/dev/null 2>&1; then
    lsof /dev/nvidia* 2>/dev/null || echo "(no holders)"
  else
    fuser -v /dev/nvidia* 2>&1 || echo "(no holders)"
  fi
  echo

  banner "X11 config (only relevant if wemby has a desktop, which it does not)"
  ls -la /etc/X11/xorg.conf* 2>/dev/null || echo "(no xorg.conf — expected for headless server)"
  echo

  banner "/usr/local/cuda* (only present if CUDA toolkit was installed manually)"
  ls -la /usr/local/ 2>/dev/null | grep -i cuda || echo "(no /usr/local/cuda* — expected)"
  echo

  echo "Inventory complete. Review output, then run: $0 purge"
}

# -------- purge --------
do_purge() {
  banner "STOP OS-side nvidia services"
  for svc in nvidia-persistenced nvidia-fabricmanager nvidia-fabricmanager.service; do
    if systemctl list-unit-files 2>/dev/null | grep -q "^${svc}\b"; then
      echo "stopping: $svc"
      systemctl stop "$svc" || true
      systemctl disable "$svc" || true
    fi
  done
  echo

  banner "REMOVE dkms module (so apt doesn't trigger a rebuild on next install)"
  dkms_status="$(dkms status 2>/dev/null | grep -i nvidia || true)"
  if [ -n "$dkms_status" ]; then
    echo "$dkms_status" | while read -r line; do
      # dkms status lines: nvidia/580.126.20, 6.8.0-111-generic, x86_64: installed
      mod=$(echo "$line" | awk -F'[,/]' '{print $1"/"$2}')
      kver=$(echo "$line" | awk -F'[, ]+' '{print $2}')
      [ -z "$mod" ] || [ -z "$kver" ] && continue
      echo "dkms remove -m $mod -v $(echo "$mod" | cut -d/ -f2) -k $kver"
      dkms remove -m "$(echo "$mod" | cut -d/ -f1)" -v "$(echo "$mod" | cut -d/ -f2)" -k "$kver" || true
    done
  else
    echo "(no dkms nvidia modules — skipping)"
  fi
  echo

  banner "REMOVE dkms source tree"
  rm -rfv /usr/src/nvidia-* 2>/dev/null || true
  echo

  banner "APT PURGE nvidia packages"
  prompt "About to run: apt-get remove --purge '^nvidia-.*' (skip ubuntu-desktop since wemby is headless)"
  apt-get remove --purge '^nvidia-.*' 2>&1 | tail -20
  echo
  echo "== autoremove =="
  apt-get autoremove --purge -y 2>&1 | tail -10
  echo

  banner "VERIFY nothing nvidia remains"
  dpkg -l | grep -i nvidia || echo "(no nvidia packages remain — good)"
  dkms status 2>/dev/null | grep -i nvidia || echo "(no nvidia dkms — good)"
}

# -------- cleanup --------
do_cleanup() {
  banner "REMOVE modprobe configs that load the OS-built module"
  rm -fv /etc/modprobe.d/nvidia*.conf 2>/dev/null || true
  rm -fv /etc/modprobe.d/blacklist-nouveau.conf 2>/dev/null || true
  echo

  banner "REMOVE modules-load.d entries"
  rm -fv /etc/modules-load.d/nvidia*.conf 2>/dev/null || true
  echo

  banner "REMOVE udev rules"
  rm -fv /lib/udev/rules.d/*nvidia* 2>/dev/null || true
  rm -fv /etc/udev/rules.d/*nvidia* 2>/dev/null || true
  echo

  banner "REBUILD initramfs (so kernel doesn't try to load a now-removed module)"
  prompt "About to run: update-initramfs -k all -u (rebuilds initramfs for all kernels)"
  update-initramfs -k all -u 2>&1 | tail -20
  echo

  banner "VERIFY nothing references nvidia anymore"
  echo "-- modprobe.d --"
  grep -r nvidia /etc/modprobe.d/ 2>/dev/null || echo "(none — good)"
  echo "-- modules-load.d --"
  grep -r nvidia /etc/modules-load.d/ 2>/dev/null || echo "(none — good)"
  echo "-- udev rules --"
  grep -ri nvidia /lib/udev/rules.d/ /etc/udev/rules.d/ 2>/dev/null || echo "(none — good)"
  echo
  echo "Cleanup complete. Next: $0 reboot"
}

# -------- reboot --------
do_reboot() {
  banner "REBOOT"
  prompt "About to schedule reboot via 'shutdown -r +1' (gives 1 min to back out with 'shutdown -c')"
  shutdown -r +1 "nvidia-os-remove: scheduled reboot"
  echo "reboot scheduled. run 'shutdown -c' within 1 min to cancel."
  echo "after reboot, the GPU Operator's nvidia-driver-daemonset will load the container-built module."
}

print_help() {
  cat <<'EOF'
nvidia-os-remove.sh — remove a host OS-level NVIDIA driver install so the
NVIDIA GPU Operator (running via gpu-operator DaemonSet) can own the GPU.

USAGE
  sudo gpu/nvidia/nvidia-os-remove.sh <subcommand>
  sudo gpu/nvidia/nvidia-os-remove.sh --help

SUBCOMMANDS
  inventory   Show what is installed (packages, DKMS, modprobe, udev, services)
              and what is currently loaded. Read-only. Run this first.
  purge       Stop OS-side nvidia services, remove DKMS module + source tree,
              and apt-purge the nvidia-* packages. Confirms before each step.
  cleanup     Remove modprobe.d / modules-load.d / udev entries, and rebuild
              initramfs so the kernel doesn't try to load the now-removed
              module on next boot.
  reboot      Schedule a reboot via 'shutdown -r +1' (cancel with 'shutdown -c'
              within 1 minute). Required to fully unload the OS-installed
              kernel module.
  all         Run inventory → purge → cleanup → reboot in order, with a
              'yes'-to-continue prompt between each phase.
  help        Show this help.

FLOW
  inventory → purge → cleanup → reboot

After reboot, the GPU Operator's nvidia-driver-daemonset pod will load
the container-built module, and the operator will report the node as
Ready. ClusterPolicy spec.driver.upgradePolicy.drain.enable=true is
recommended before the next driver upgrade so GPU pods are drained
first.

REQUIRES
  bash 4+, apt, systemctl, modprobe, dkms, update-initramfs. Run as root
  (root not required for `help` or `inventory` if you only want a read-only
  peek; subsequent steps need root).

SEE ALSO
  gpu/nvidia/deploy-nvidia-gpu.sh — install/configure the GPU Operator
  gpu/nvidia/values.yaml          — ClusterPolicy Helm values
EOF
}

case "$CMD" in
  inventory)  do_inventory ;;
  purge)       do_purge ;;
  cleanup)     do_cleanup ;;
  reboot)      do_reboot ;;
  all)
    do_inventory
    prompt "Continue to purge?"
    do_purge
    prompt "Continue to cleanup?"
    do_cleanup
    prompt "Continue to reboot?"
    do_reboot
    ;;
  help|*)
    print_help
    [ "$CMD" = "help" ] && exit 0
    exit 2
    ;;
esac
