#!/usr/bin/env bash
set -euo pipefail

# GPU profile toggle for timmy's RX 9070 XT
# Switches LACT between performance (COMPUTE, 340W, aggressive fan)
# and default (firmware auto, 304W, firmware fan control)
#
# Usage: sudo gpu-toggle [performance|default|status]
# No argument: toggles between profiles

LACT_CONFIG="/etc/lact/config.yaml"
PROFILE_DIR="/opt/gpu-profiles"
PERF_PROFILE="$PROFILE_DIR/performance.yaml"
DEFAULT_PROFILE="$PROFILE_DIR/default.yaml"
STATE_FILE="$PROFILE_DIR/.current"

CARD="/sys/class/drm/card1/device"
HWMON="$CARD/hwmon/hwmon2"

show_status() {
    local profile
    profile=$(cat "$STATE_FILE" 2>/dev/null || echo "unknown")

    local power_cap junction edge fan_rpm pp_mode
    power_cap=$(( $(cat "$HWMON/power1_cap" 2>/dev/null || echo 0) / 1000000 ))
    junction=$(( $(cat "$HWMON/temp2_input" 2>/dev/null || echo 0) / 1000 ))
    edge=$(( $(cat "$HWMON/temp1_input" 2>/dev/null || echo 0) / 1000 ))
    fan_rpm=$(cat "$HWMON/fan1_input" 2>/dev/null || echo 0)
    pp_mode=$(grep '\*' "$CARD/pp_power_profile_mode" 2>/dev/null | awk '{print $2}' | tr -d ':')

    echo "=== GPU Profile: $profile ==="
    echo "  Power profile:  $pp_mode"
    echo "  Power cap:      ${power_cap}W"
    echo "  Junction temp:  ${junction}C"
    echo "  Edge temp:      ${edge}C"
    echo "  Fan RPM:        $fan_rpm"
}

apply_profile() {
    local profile="$1"
    local src

    case "$profile" in
        performance) src="$PERF_PROFILE" ;;
        default)     src="$DEFAULT_PROFILE" ;;
        *)           echo "Unknown profile: $profile"; exit 1 ;;
    esac

    if [ ! -f "$src" ]; then
        echo "Profile not found: $src"
        exit 1
    fi

    cp "$src" "$LACT_CONFIG"
    echo "$profile" > "$STATE_FILE"

    # Restart lactd to apply
    systemctl restart lactd

    # For default profile, also reset sysfs directly since LACT
    # won't touch performance_level or power_profile when unconfigured
    if [ "$profile" = "default" ]; then
        echo 0 > "$CARD/pp_power_profile_mode" 2>/dev/null || true
        echo 304000000 > "$HWMON/power1_cap" 2>/dev/null || true
    fi

    echo "Switched to: $profile"
    sleep 1
    show_status
}

current_profile() {
    cat "$STATE_FILE" 2>/dev/null || echo "unknown"
}

# Main
case "${1:-toggle}" in
    performance|default)
        apply_profile "$1"
        ;;
    status)
        show_status
        ;;
    toggle)
        cur=$(current_profile)
        if [ "$cur" = "performance" ]; then
            apply_profile "default"
        else
            apply_profile "performance"
        fi
        ;;
    *)
        echo "Usage: gpu-toggle [performance|default|status|toggle]"
        exit 1
        ;;
esac
