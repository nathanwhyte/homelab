#!/usr/bin/env bash
# GPU fan controller for AMD RDNA 4 — uses fan1_target (RPM) instead of PWM.
# PWM writes are ignored by RDNA 4 firmware; fan1_target works.
# Reads junction temp (temp2_input). Designed for headless 24/7 inference.

set -euo pipefail

CARD="${AMDGPU_CARD:-card1}"
HWMON=$(ls -d /sys/class/drm/${CARD}/device/hwmon/hwmon* 2>/dev/null | head -1)
INTERVAL="${FAN_INTERVAL:-3}"

if [ -z "$HWMON" ]; then
    echo "ERROR: No hwmon found for ${CARD}"
    exit 1
fi

FAN_TARGET="${HWMON}/fan1_target"
FAN_INPUT="${HWMON}/fan1_input"
FAN_MAX=$(cat "${HWMON}/fan1_max" 2>/dev/null || echo 3600)
FAN_MIN=$(cat "${HWMON}/fan1_min" 2>/dev/null || echo 0)
TEMP_JUNCTION="${HWMON}/temp2_input"
TEMP_EDGE="${HWMON}/temp1_input"

if [ ! -f "$FAN_TARGET" ]; then
    echo "ERROR: fan1_target not found at ${FAN_TARGET}"
    exit 1
fi

# Enable manual fan control if supported
if [ -f "${HWMON}/pwm1_enable" ]; then
    echo 1 > "${HWMON}/pwm1_enable" 2>/dev/null || true
fi

# Fan curve: junction_temp → fan RPM percentage of max
# Aggressive for inference — prioritize cooling over noise
LAST_RPM=0
TEMP_DROP=5

get_fan_pct() {
    local t=$1
    [ "$t" -ge 85 ] && echo 100 && return
    [ "$t" -ge 80 ] && echo 80 && return
    [ "$t" -ge 70 ] && echo 60 && return
    [ "$t" -ge 50 ] && echo 40 && return
    echo 30
}

cleanup() {
    echo "Restoring automatic fan control..."
    if [ -f "${HWMON}/pwm1_enable" ]; then
        echo 2 > "${HWMON}/pwm1_enable" 2>/dev/null || true
    fi
    exit 0
}
trap cleanup SIGTERM SIGINT

echo "amdgpu-fan-control started (RPM target mode)"
echo "  Card: ${CARD} (${HWMON})"
echo "  Fan target: ${FAN_TARGET}"
echo "  Fan range: ${FAN_MIN}-${FAN_MAX} RPM"
echo "  Junction temp: ${TEMP_JUNCTION}"
echo "  Interval: ${INTERVAL}s"

while true; do
    junc_raw=$(cat "$TEMP_JUNCTION" 2>/dev/null || echo 0)
    junc=$((junc_raw / 1000))

    edge_raw=$(cat "$TEMP_EDGE" 2>/dev/null || echo 0)
    edge=$((edge_raw / 1000))

    pct=$(get_fan_pct "$junc")
    target_rpm=$(( pct * FAN_MAX / 100 ))

    # Hysteresis: only reduce RPM if temp dropped enough
    if [ "$target_rpm" -lt "$LAST_RPM" ]; then
        check_pct=$(get_fan_pct $((junc + TEMP_DROP)))
        check_rpm=$(( check_pct * FAN_MAX / 100 ))
        if [ "$check_rpm" -ge "$LAST_RPM" ]; then
            target_rpm=$LAST_RPM
            pct=$((LAST_RPM * 100 / FAN_MAX))
        fi
    fi

    echo "$target_rpm" > "$FAN_TARGET" 2>/dev/null || true
    LAST_RPM=$target_rpm
    actual_rpm=$(cat "$FAN_INPUT" 2>/dev/null || echo 0)

    # Log every 30 seconds
    if [ $(( SECONDS % 30 )) -lt "$INTERVAL" ]; then
        echo "j=${junc}C e=${edge}C target=${target_rpm}RPM actual=${actual_rpm}RPM (${pct}%)"
    fi

    sleep "$INTERVAL"
done
