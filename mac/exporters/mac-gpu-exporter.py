#!/usr/bin/env python3
"""Prometheus exporter for Apple Silicon GPU / ANE / SoC telemetry.

Wraps `powermetrics`, which requires root. Run under launchd as a system
daemon (see ../launchd/com.nathanwhyte.mac-gpu-exporter.plist), or invoke
manually with sudo.

Metrics:
    mac_gpu_power_watts                 GPU package power
    mac_gpu_utilization_percent         GPU HW active residency
    mac_gpu_freq_mhz                    GPU HW active frequency
    mac_ane_power_watts                 Apple Neural Engine power
    mac_cpu_package_power_watts         Combined SoC power (CPU + GPU + ANE)
    mac_thermal_pressure                0=Nominal, 1=Fair, 2=Serious, 3=Critical
    mac_gpu_exporter_up                 1 if last powermetrics invocation succeeded

Usage:
    sudo python3 mac-gpu-exporter.py                # default :9112, sample every 5s
    sudo python3 mac-gpu-exporter.py --port 9112 --interval 5
"""

import argparse
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ── Metrics state ────────────────────────────────────────────────────────────

_lock = threading.Lock()
_metrics = {
    "mac_gpu_power_watts": 0.0,
    "mac_gpu_utilization_percent": 0.0,
    "mac_gpu_freq_mhz": 0.0,
    "mac_ane_power_watts": 0.0,
    "mac_cpu_package_power_watts": 0.0,
    "mac_thermal_pressure": 0,
    "mac_gpu_exporter_up": 0,
}

THERMAL_LEVELS = {
    "Nominal": 0,
    "Fair": 1,
    "Serious": 2,
    "Critical": 3,
}

# ── Parsing ──────────────────────────────────────────────────────────────────

# powermetrics emits lines like:
#   GPU Power: 1234 mW
#   GPU HW active residency: 42.31% (...)
#   GPU HW active frequency: 720 MHz
#   ANE Power: 0 mW
#   Combined Power (CPU + GPU + ANE): 5678 mW
#   Current pressure level: Nominal
_PATTERNS = {
    "mac_gpu_power_watts": re.compile(r"^GPU Power:\s*([0-9.]+)\s*mW", re.MULTILINE),
    "mac_gpu_utilization_percent": re.compile(
        r"^GPU HW active residency:\s*([0-9.]+)\s*%", re.MULTILINE
    ),
    "mac_gpu_freq_mhz": re.compile(
        r"^GPU HW active frequency:\s*([0-9.]+)\s*MHz", re.MULTILINE
    ),
    "mac_ane_power_watts": re.compile(r"^ANE Power:\s*([0-9.]+)\s*mW", re.MULTILINE),
    "mac_cpu_package_power_watts": re.compile(
        r"^Combined Power.*?:\s*([0-9.]+)\s*mW", re.MULTILINE
    ),
}

_THERMAL_RE = re.compile(r"^Current pressure level:\s*(\w+)", re.MULTILINE)


def parse_powermetrics(output: str) -> dict:
    """Parse powermetrics text output into a metric dict.

    Missing fields default to 0. Values in mW are converted to W.
    """
    result = {
        k: 0.0
        for k in _metrics
        if k not in ("mac_thermal_pressure", "mac_gpu_exporter_up")
    }
    result["mac_thermal_pressure"] = 0

    for name, pattern in _PATTERNS.items():
        m = pattern.search(output)
        if not m:
            continue
        val = float(m.group(1))
        if name.endswith("_watts"):
            val = val / 1000.0  # mW → W
        result[name] = val

    m = _THERMAL_RE.search(output)
    if m:
        result["mac_thermal_pressure"] = THERMAL_LEVELS.get(m.group(1), 0)

    return result


# ── Sampling loop ────────────────────────────────────────────────────────────


def sample_once() -> None:
    """Run powermetrics once and update the metric dict."""
    try:
        proc = subprocess.run(
            [
                "/usr/bin/powermetrics",
                "--samplers",
                "gpu_power,thermal,cpu_power",
                "-i",
                "1000",
                "-n",
                "1",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode != 0:
            with _lock:
                _metrics["mac_gpu_exporter_up"] = 0
            return
        parsed = parse_powermetrics(proc.stdout)
        parsed["mac_gpu_exporter_up"] = 1
        with _lock:
            _metrics.update(parsed)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        with _lock:
            _metrics["mac_gpu_exporter_up"] = 0


def sample_loop(interval: int) -> None:
    while True:
        sample_once()
        time.sleep(interval)


# ── Exposition ───────────────────────────────────────────────────────────────

_HELP = {
    "mac_gpu_power_watts": ("gauge", "Apple Silicon GPU package power in watts."),
    "mac_gpu_utilization_percent": ("gauge", "GPU HW active residency in percent."),
    "mac_gpu_freq_mhz": ("gauge", "GPU HW active frequency in MHz."),
    "mac_ane_power_watts": ("gauge", "Apple Neural Engine power in watts."),
    "mac_cpu_package_power_watts": (
        "gauge",
        "Combined SoC power (CPU + GPU + ANE) in watts.",
    ),
    "mac_thermal_pressure": (
        "gauge",
        "Thermal pressure: 0=Nominal,1=Fair,2=Serious,3=Critical.",
    ),
    "mac_gpu_exporter_up": ("gauge", "1 if last powermetrics invocation succeeded."),
}


def format_metrics() -> str:
    lines = []
    with _lock:
        snapshot = dict(_metrics)
    for name, value in snapshot.items():
        mtype, helptext = _HELP[name]
        lines.append(f"# HELP {name} {helptext}")
        lines.append(f"# TYPE {name} {mtype}")
        lines.append(f"{name} {value}")
    return "\n".join(lines) + "\n"


# ── HTTP handler ─────────────────────────────────────────────────────────────


class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metrics":
            body = format_metrics().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok\n")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # suppress access log


# ── Main ─────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Prometheus exporter for Apple Silicon GPU/SoC"
    )
    parser.add_argument(
        "--port", type=int, default=9112, help="Listen port (default 9112)"
    )
    parser.add_argument(
        "--interval", type=int, default=5, help="Sample interval seconds (default 5)"
    )
    args = parser.parse_args()

    print(
        f"mac-gpu-exporter starting on :{args.port}, sampling every {args.interval}s",
        flush=True,
    )

    sampler = threading.Thread(target=sample_loop, args=(args.interval,), daemon=True)
    sampler.start()

    server = ThreadingHTTPServer(("0.0.0.0", args.port), MetricsHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
