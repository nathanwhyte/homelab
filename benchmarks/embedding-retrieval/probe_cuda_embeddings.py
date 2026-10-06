#!/usr/bin/env python3
"""Read an already-authorized isolated probe Pod; stop it on thermal failure.

Sends exactly the supplied sample once. Owns a loopback port-forward. Saves
partial evidence on failure. Does not apply manifests or alter any Service.
"""

import argparse
import hashlib
import json
import math
import statistics
import subprocess
import threading
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

POD = "task1218-qwen4b-probe"
KUBE = ["kubectl", "-n", "viking"]
# i7-8750H Tjunction is 100 C; retain 5 C headroom for this bounded probe.
CPU_LIMIT_C = 95
GPU_LIMIT_C = 85
TELEMETRY = r"""
set -eu
nvidia-smi --query-gpu=memory.used,memory.total,temperature.gpu,utilization.gpu,power.draw --format=csv,noheader,nounits
awk '/^VmRSS:/ {rss=$2} /^VmHWM:/ {hwm=$2} END {print rss, hwm}' /proc/1/status
m=0
for h in /sys/class/hwmon/hwmon*; do
  case "$(cat "$h/name" 2>/dev/null)" in k10temp|coretemp)
    for t in "$h"/temp*_input; do
      v=$(cat "$t" 2>/dev/null || echo 0)
      [ "$v" -le "$m" ] || m=$v
    done;;
  esac
done
echo "$m"
"""


def get(base, route, payload=None, as_json=True):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        base + route, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=120) as response:
        text = response.read().decode()
    return json.loads(text) if as_json else text


def cosine(a, b):
    if len(a) != len(b) or not all(math.isfinite(x) for x in [*a, *b]):
        raise ValueError("Invalid vectors")
    return sum(x * y for x, y in zip(a, b)) / math.sqrt(
        sum(x * x for x in a) * sum(x * x for x in b)
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sample", type=Path, required=True)
    ap.add_argument("--baseline", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    sample = json.loads(args.sample.read_text())
    baseline = json.loads(args.baseline.read_text())
    if len(sample) != 44 or len(baseline) != 44:
        raise ValueError("Expected the frozen 44-text sample and historical baseline")
    args.output.mkdir(parents=True, exist_ok=False)
    result = {
        "started_at": datetime.now(UTC).isoformat(),
        "pod": POD,
        "sample_sha256": hashlib.sha256(args.sample.read_bytes()).hexdigest(),
        "historical_baseline_sha256": hashlib.sha256(
            args.baseline.read_bytes()
        ).hexdigest(),
        "baseline_caveat": "Historical 1080 vectors from 2026-09-22; sample order inherited from source experiment, not a fresh paired control",
        "requests": [],
        "telemetry": [],
        "completed": False,
        "thermal_guard": {
            "cpu_limit_c": CPU_LIMIT_C,
            "gpu_limit_c": GPU_LIMIT_C,
            "consecutive_samples": 2,
            "interval_seconds": 5,
        },
    }
    pod = json.loads(
        subprocess.check_output(KUBE + ["get", "pod", POD, "-o", "json"], text=True)
    )
    result["runtime"] = {
        "node": pod["spec"]["nodeName"],
        "uid": pod["metadata"]["uid"],
        "image_id": pod["status"]["containerStatuses"][0]["imageID"],
        "args": pod["spec"]["containers"][0]["args"],
    }
    stop = threading.Event()
    sampled = threading.Event()
    abort = []

    def monitor():
        hot = failures = 0
        while not stop.is_set():
            try:
                raw = subprocess.check_output(
                    KUBE + ["exec", POD, "-c", "llamacpp", "--", "sh", "-c", TELEMETRY],
                    text=True,
                    stderr=subprocess.PIPE,
                    timeout=15,
                )
                lines = raw.strip().splitlines()
                gpu = [float(x.strip()) for x in lines[0].split(",")]
                rss, hwm = [int(x) for x in lines[1].split()]
                cpu = int(lines[2]) / 1000
                if len(gpu) != 5 or cpu <= 0:
                    raise ValueError("Missing GPU or CPU temperature")
                result["telemetry"].append(
                    {
                        "ts": time.time(),
                        "memory_mib": gpu[0],
                        "total_mib": gpu[1],
                        "gpu_c": gpu[2],
                        "util_pct": gpu[3],
                        "watts": gpu[4],
                        "rss_kib": rss,
                        "hwm_kib": hwm,
                        "cpu_c": cpu,
                    }
                )
                hot = hot + 1 if gpu[2] >= GPU_LIMIT_C or cpu >= CPU_LIMIT_C else 0
                failures = 0
                sampled.set()
            except (OSError, subprocess.SubprocessError, ValueError, IndexError) as exc:
                failures += 1
                result["telemetry"].append({"ts": time.time(), "error": str(exc)})
            if hot >= 2 or failures >= 3:
                abort.append("thermal limit" if hot >= 2 else "telemetry unavailable")
                # This exact probe's deletion is included in the user's authorization.
                subprocess.run(
                    KUBE + ["delete", "pod", POD, "--wait=false", "--ignore-not-found"],
                    capture_output=True,
                    timeout=30,
                    check=False,
                )
                sampled.set()
                return
            stop.wait(5)

    log = (args.output / "port-forward.log").open("w")
    forward = subprocess.Popen(
        KUBE + ["port-forward", "pod/" + POD, "18084:8000", "--address", "127.0.0.1"],
        stdout=log,
        stderr=log,
    )
    monitor_thread = None
    base = "http://127.0.0.1:18084"
    try:
        for _ in range(60):
            if forward.poll() is not None:
                raise RuntimeError("port-forward exited")
            try:
                if get(base, "/health").get("status") == "ok":
                    break
            except OSError:
                pass
            time.sleep(1)
        else:
            raise TimeoutError("probe health timeout")
        monitor_thread = threading.Thread(target=monitor, daemon=True)
        monitor_thread.start()
        if not sampled.wait(60) or abort:
            raise RuntimeError(f"No healthy telemetry: {abort}")
        first = next(row for row in result["telemetry"] if "memory_mib" in row)
        if first["memory_mib"] < 4000:
            raise RuntimeError(
                "4B Q8_0 is not resident on the GPU; refuse a CPU fallback measurement"
            )
        result["metrics_before"] = get(base, "/metrics", as_json=False)
        started = time.monotonic()
        for index, row in enumerate(sample):
            if abort:
                raise RuntimeError(abort[0])
            begin = time.monotonic()
            response = get(
                base,
                "/v1/embeddings",
                {"model": "probe", "input": [row["text"]], "cache_prompt": False},
            )
            elapsed = 1000 * (time.monotonic() - begin)
            v = response["data"][0]["embedding"]
            if len(v) != 2560:
                raise ValueError("Wrong model dimension")
            result["requests"].append(
                {
                    "index": index,
                    "target": row["target"],
                    "sample_tokens": row["tokens"],
                    "usage": response.get("usage"),
                    "input_sha256": hashlib.sha256(row["text"].encode()).hexdigest(),
                    "latency_ms": elapsed,
                    "cosine_vs_historical_1080": cosine(v, baseline[index]),
                    "vector": v,
                }
            )
            print(
                f"{index + 1}/44: target={row['target']} latency={elapsed:.0f} ms cosine={result['requests'][-1]['cosine_vs_historical_1080']:.7f}",
                flush=True,
            )
        result["elapsed_seconds"] = time.monotonic() - started
        result["metrics_after"] = get(base, "/metrics", as_json=False)
        if abort:
            raise RuntimeError(abort[0])
        result["completed"] = True
        result["cosine_min"] = min(
            r["cosine_vs_historical_1080"] for r in result["requests"]
        )
        result["cosine_median"] = statistics.median(
            r["cosine_vs_historical_1080"] for r in result["requests"]
        )
    finally:
        stop.set()
        if monitor_thread:
            monitor_thread.join(timeout=20)
        result["abort"] = abort
        result["completed"] = result["completed"] and not abort
        try:
            server_log = subprocess.check_output(
                KUBE + ["logs", POD, "-c", "llamacpp"],
                text=True,
                stderr=subprocess.PIPE,
                timeout=20,
            )
            (args.output / "server.log").write_text(server_log)
        except (OSError, subprocess.SubprocessError) as exc:
            result["server_log_error"] = str(exc)
        (args.output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        forward.terminate()
        try:
            forward.wait(timeout=10)
        except subprocess.TimeoutExpired:
            forward.kill()
            forward.wait()
        log.close()


if __name__ == "__main__":
    main()
