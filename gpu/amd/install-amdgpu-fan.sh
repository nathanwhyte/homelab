#!/usr/bin/env bash
set -euo pipefail
# Install amdgpu-fan on timmy for aggressive GPU cooling
# Run: ssh -t timmy 'bash -s' < gpu/amd/install-amdgpu-fan.sh

echo "=== Installing amdgpu-fan ==="
$HOME/.local/bin/uv tool install amdgpu-fan

echo "=== Deploying config ==="
sudo tee /etc/amdgpu-fan.yml > /dev/null << 'YAML'
speed_matrix:
- [0, 30]
- [50, 40]
- [70, 60]
- [80, 80]
- [85, 100]
cards:
- card1
temp_drop: 5
YAML

echo "=== Enabling systemd service ==="
sudo systemctl enable --now amdgpu-fan || {
  echo "Service file not found, creating..."
  sudo tee /etc/systemd/system/amdgpu-fan.service > /dev/null << SVC
[Unit]
Description=amdgpu-fan controller
After=multi-user.target

[Service]
ExecStart=$HOME/.local/bin/amdgpu-fan
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
SVC
  sudo systemctl daemon-reload
  sudo systemctl enable --now amdgpu-fan
}

echo "=== Verifying ==="
sleep 3
systemctl status amdgpu-fan --no-pager || true
echo ""
echo "Current fan speed:"
cat /sys/class/drm/card1/device/hwmon/hwmon*/fan1_input 2>/dev/null || echo "could not read"
echo " RPM"
