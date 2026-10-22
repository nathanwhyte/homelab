#!/usr/bin/env bash
# AMD GPU fan controller — RDNA 4 (RX 9070 XT)
# Uses junction temp (temp2_input) for throttle-relevant control.
# RDNA 4 exposes fan1_enable instead of pwm1_enable; pwm1 is written directly.
#
# Curve matches amdgpu-fan.yml:
#   0°C→30%  50°C→40%  70°C→60%  80°C→80%  85°C→100%
#
# Deploy: cp amdgpu-fan-control.sh /opt/amdgpu-fan-control.sh
# Service env vars: AMDGPU_CARD (default: card1), FAN_INTERVAL (default: 3)

set -euo pipefail

CARD="${AMDGPU_CARD:-card1}"
INTERVAL="${FAN_INTERVAL:-3}"
TEMP_DROP=5   # hysteresis: don't reduce fan until temp falls this many °C

HWMON_BASE="/sys/class/drm/${CARD}/device/hwmon"
HWMON_PATH="${HWMON_BASE}/$(ls "${HWMON_BASE}/" | head -1)"
TEMP_FILE="${HWMON_PATH}/temp2_input"   # junction temp (millidegrees)
PWM_FILE="${HWMON_PATH}/pwm1"

# Curve: parallel arrays of (temp °C, fan %)
CURVE_TEMPS=(0  50  70  80  85)
CURVE_SPEEDS=(30 40  60  80 100)

interpolate() {
    local temp=$1
    local n=${#CURVE_TEMPS[@]}
    local last=$(( n - 1 ))

    (( temp <= CURVE_TEMPS[0] ))    && echo "${CURVE_SPEEDS[0]}"  && return
    (( temp >= CURVE_TEMPS[last] )) && echo "${CURVE_SPEEDS[last]}" && return

    for (( i=1; i<n; i++ )); do
        if (( temp <= CURVE_TEMPS[i] )); then
            local t0=${CURVE_TEMPS[i-1]} t1=${CURVE_TEMPS[i]}
            local s0=${CURVE_SPEEDS[i-1]} s1=${CURVE_SPEEDS[i]}
            echo $(( s0 + (s1 - s0) * (temp - t0) / (t1 - t0) ))
            return
        fi
    done
}

pct_to_pwm() { echo $(( $1 * 255 / 100 )); }

restore_auto() {
    echo "restoring automatic fan control"
    # pwm1_enable=2 restores auto mode on standard cards; RDNA 4 may not have this file
    [[ -f "${HWMON_PATH}/pwm1_enable" ]] && echo 2 > "${HWMON_PATH}/pwm1_enable" 2>/dev/null || true
}
trap restore_auto EXIT INT TERM

echo "amdgpu-fan-control starting"
echo "  card:     ${CARD}"
echo "  hwmon:    ${HWMON_PATH}"
echo "  interval: ${INTERVAL}s"
echo "  temp_drop: ${TEMP_DROP}°C"
echo "  fan control: direct pwm1 (RDNA 4 — no pwm1_enable)"

prev_temp=0
current_speed=0

while true; do
    raw=$(cat "${TEMP_FILE}")
    temp=$(( raw / 1000 ))

    # Apply hysteresis when temperature is falling:
    # use (temp - TEMP_DROP) for curve lookup so we don't ramp down prematurely
    if (( temp < prev_temp )); then
        effective=$(( temp - TEMP_DROP ))
        (( effective < 0 )) && effective=0
    else
        effective=${temp}
    fi

    target=$(interpolate "${effective}")

    if (( target != current_speed )); then
        pwm=$(pct_to_pwm "${target}")
        echo "${pwm}" > "${PWM_FILE}"
        printf "%s  junction=%d°C  effective=%d°C  fan=%d%%  pwm=%d\n" \
            "$(date '+%H:%M:%S')" "${temp}" "${effective}" "${target}" "${pwm}"
        current_speed=${target}
    fi

    prev_temp=${temp}
    sleep "${INTERVAL}"
done
