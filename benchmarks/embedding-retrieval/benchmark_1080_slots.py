#!/usr/bin/env python3
"""Run the authorized isolated 1080 experiment and restore production replicas.

Without --execute, render only the four Pod manifests and verify local inputs.
Every output directory is exclusive. No OV data or Service selector is changed.
"""

import argparse
import concurrent.futures
import hashlib
import json
import random
import signal
import statistics
import subprocess
import threading
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from benchmark_local_quality import normalize, score
from probe_cuda_embeddings import TELEMETRY

KUBE = ["kubectl", "-n", "viking"]
DEPLOY = "embedder-qwen-cuda"
POD = "task1218-1080-arm"
IMAGE = "ghcr.io/ggml-org/llama.cpp@sha256:95cdc1510dbc5bc15735529353d72ace9fc96b9aeb0dd6c4cdaf1a6b3f70776f"
MODELS = {
    "4b": ("Qwen3-Embedding-4B-Q8_0.gguf", 2560),
    "06b": ("task1218-Qwen3-Embedding-0.6B-f16.gguf", 1024),
}


def now():
    return datetime.now(UTC).isoformat()


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def kube(*args, timeout=60, check=True, data=None):
    return subprocess.run(
        KUBE + list(args),
        input=data,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=check,
    )


def obj(*args):
    return json.loads(kube(*args, "-o", "json").stdout)


def http(base, route, payload=None, timeout=180, raw=False):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        base + route, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        text = response.read().decode()
    return text if raw else json.loads(text)


def manifest(model, slots):
    args = [
        "--model",
        "/models/" + MODELS[model][0],
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
        "--embedding",
        "--pooling",
        "last",
        "--ctx-size",
        str(8192 * slots),
        "--parallel",
        str(slots),
        "--n-gpu-layers",
        "999",
        "--threads",
        "4",
        "--batch-size",
        "2048",
        "--ubatch-size",
        "2048",
        "--cache-ram",
        "0",
        "--split-mode",
        "none",
        "--main-gpu",
        "0",
        "--flash-attn",
        "on",
        "--cache-type-k",
        "f16",
        "--cache-type-v",
        "f16",
        "--metrics",
    ]
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": POD, "namespace": "viking", "labels": {"app": POD}},
        "spec": {
            "restartPolicy": "Never",
            "activeDeadlineSeconds": 7200,
            "terminationGracePeriodSeconds": 15,
            "runtimeClassName": "nvidia",
            "nodeSelector": {"kubernetes.io/hostname": "manu"},
            "containers": [
                {
                    "name": "llamacpp",
                    "image": IMAGE,
                    "command": ["/app/llama-server"],
                    "args": args,
                    "resources": {
                        "requests": {
                            "cpu": "500m",
                            "memory": "2Gi",
                            "nvidia.com/gpu": "1",
                        },
                        "limits": {"cpu": "4", "memory": "6Gi", "nvidia.com/gpu": "1"},
                    },
                    "readinessProbe": {
                        "httpGet": {"path": "/health", "port": 8000},
                        "periodSeconds": 5,
                    },
                    "volumeMounts": [
                        {"name": "models", "mountPath": "/models", "readOnly": True},
                        {"name": "shm", "mountPath": "/dev/shm"},
                    ],
                }
            ],
            "volumes": [
                {
                    "name": "models",
                    "persistentVolumeClaim": {"claimName": "embedder-cuda-model-cache"},
                },
                {"name": "shm", "emptyDir": {"medium": "Memory", "sizeLimit": "2Gi"}},
            ],
        },
    }


class Forward:
    def __init__(self, target, path):
        self.log = path.open("w")
        self.process = subprocess.Popen(
            KUBE + ["port-forward", target, "18085:8000", "--address", "127.0.0.1"],
            stdout=self.log,
            stderr=self.log,
        )
        self.base = "http://127.0.0.1:18085"

    def ready(self, seconds=180):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("port-forward exited")
            try:
                if http(self.base, "/health", timeout=3).get("status") == "ok":
                    return
            except (OSError, ValueError):
                pass
            time.sleep(2)
        raise TimeoutError("model health timeout")

    def close(self):
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.log.close()


class Monitor:
    def __init__(self, path, base):
        self.path, self.base = path, base
        self.stop = threading.Event()
        self.sampled = threading.Event()
        self.reason = None
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        hot = failures = 0
        with self.path.open("w") as stream:
            while not self.stop.is_set():
                row = {"at": now()}
                try:
                    raw = kube(
                        "exec",
                        POD,
                        "-c",
                        "llamacpp",
                        "--",
                        "sh",
                        "-c",
                        TELEMETRY + "\ncat /proc/loadavg\n",
                        timeout=20,
                    ).stdout
                    lines = raw.strip().splitlines()
                    gpu = [float(x.strip()) for x in lines[0].split(",")]
                    rss, hwm = map(int, lines[1].split())
                    cpu = int(lines[2]) / 1000
                    if len(gpu) != 5 or cpu <= 0:
                        raise ValueError("missing telemetry")
                    row.update(
                        vram_mib=gpu[0],
                        gpu_c=gpu[2],
                        util_pct=gpu[3],
                        watts=gpu[4],
                        rss_kib=rss,
                        hwm_kib=hwm,
                        cpu_c=cpu,
                    )
                    row["host_load_average"] = [float(x) for x in lines[3].split()[:3]]
                    hot = hot + 1 if cpu >= 80 or gpu[2] >= 85 else 0
                    failures = 0
                    self.sampled.set()
                except (
                    OSError,
                    subprocess.SubprocessError,
                    ValueError,
                    IndexError,
                ) as exc:
                    row["error"] = str(exc)
                    failures += 1
                try:
                    row["metrics"] = http(self.base, "/metrics", timeout=2, raw=True)
                except (OSError, ValueError):
                    row["metrics_unavailable"] = True
                stream.write(json.dumps(row) + "\n")
                stream.flush()
                if hot >= 2 or failures >= 3:
                    self.reason = "thermal guard" if hot >= 2 else "telemetry lost"
                    self.sampled.set()
                    kube(
                        "delete",
                        "pod",
                        POD,
                        "--wait=false",
                        "--ignore-not-found",
                        check=False,
                    )
                    return
                self.stop.wait(5)

    def start(self):
        self.thread.start()
        if not self.sampled.wait(65):
            raise RuntimeError("no healthy telemetry")
        self.check()

    def check(self):
        if self.reason:
            raise RuntimeError(self.reason)

    def close(self):
        self.stop.set()
        self.thread.join(timeout=25)


def embed(base, text, dimension, key, monitor, keep_vector=True):
    monitor.check()
    start = time.time()
    response = http(
        base,
        "/v1/embeddings",
        {"model": "benchmark", "input": [text], "cache_prompt": False},
    )
    end = time.time()
    vector = response["data"][0]["embedding"]
    normalize(vector, dimension)
    row = {
        "key": key,
        "start": start,
        "end": end,
        "latency_ms": (end - start) * 1000,
        "input_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "input_chars": len(text),
        "usage": response.get("usage"),
    }
    if keep_vector:
        row["vector"] = vector
    return row


def batch(base, jobs, dimension, workers, monitor, path, keep_vectors=False):
    begin = time.monotonic()
    rows = []
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    try:
        stream = path.open("w")
        futures = {
            pool.submit(embed, base, text, dimension, key, monitor, keep_vectors): key
            for key, text in jobs
        }
        for future in concurrent.futures.as_completed(futures):
            row = future.result()
            stream.write(json.dumps(row) + "\n")
            stream.flush()
            rows.append(row)
            if keep_vectors and len(rows) % 100 == 0:
                print(
                    f"{path.parent.name}/{path.stem}: {len(rows)}/{len(jobs)}",
                    flush=True,
                )
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        if "stream" in locals():
            stream.close()
    elapsed = time.monotonic() - begin
    return rows, {
        "requests": len(rows),
        "elapsed_seconds": elapsed,
        "requests_per_second": len(rows) / elapsed,
        "response_tokens_per_second": sum(
            (r["usage"] or {}).get("prompt_tokens", 0) for r in rows
        )
        / elapsed,
        "median_ms": statistics.median(r["latency_ms"] for r in rows),
    }


def quality(base, corpus, lengths, questions, dimension, slots, monitor, out):
    jobs = []
    for row in corpus:
        expected = lengths[row["entry_id"]]
        text = row["text"][: expected["input_chars"]]
        if hashlib.sha256(text.encode()).hexdigest() != expected["input_sha256"]:
            raise ValueError("paired input mismatch")
        jobs.append((row["entry_id"], text))
    rows, perf = batch(
        base, jobs, dimension, slots, monitor, out / "vectors.jsonl", True
    )
    vectors = {r["key"].lower(): normalize(r["vector"], dimension) for r in rows}
    qrows, _ = batch(
        base,
        [(q["qid"], q["question"]) for q in questions],
        dimension,
        slots,
        monitor,
        out / "queries.jsonl",
        True,
    )
    qvectors = {r["key"]: r["vector"] for r in qrows}
    scored = []
    for q in questions:
        qv = normalize(qvectors[q["qid"]], dimension)
        ranking = sorted(
            ((key, sum(a * b for a, b in zip(v, qv))) for key, v in vectors.items()),
            key=lambda x: (-x[1], x[0]),
        )[:5]
        scored.append(
            {"qid": q["qid"], "expected": q["match_fragment"], "top5": ranking}
        )
    result = {"scores": score(scored), "per_query": scored, "performance": perf}
    save(out / "quality.json", result)
    print(out.name + " quality: " + json.dumps(result["scores"]), flush=True)


def run_arm(model, slots, args, corpus, lengths, questions, sample):
    out = args.output / f"{model}-s{slots}"
    out.mkdir()
    mf = args.output / f"{model}-s{slots}.json"
    diff = kube("diff", "-f", str(mf), check=False)
    (out / "manifest.diff").write_text(diff.stdout + diff.stderr)
    if diff.returncode not in (0, 1):
        raise RuntimeError("benchmark manifest diff failed")
    kube("apply", "-f", str(mf))
    forward = monitor = None
    try:
        kube(
            "wait", "--for=condition=Ready", "pod/" + POD, "--timeout=300s", timeout=310
        )
        pod = obj("get", "pod", POD)
        save(
            out / "runtime.json",
            {
                "node": pod["spec"]["nodeName"],
                "uid": pod["metadata"]["uid"],
                "image_id": pod["status"]["containerStatuses"][0]["imageID"],
                "args": pod["spec"]["containers"][0]["args"],
            },
        )
        forward = Forward("pod/" + POD, out / "port-forward.log")
        forward.ready()
        gpu_info = kube(
            "exec",
            POD,
            "--",
            "nvidia-smi",
            "-q",
            "-d",
            "PERFORMANCE,TEMPERATURE,POWER",
            check=False,
        )
        (out / "gpu-before.txt").write_text(gpu_info.stdout + gpu_info.stderr)
        sensor_info = kube(
            "exec",
            POD,
            "--",
            "sh",
            "-c",
            'for h in /sys/class/hwmon/hwmon*; do case "$(cat "$h/name")" in k10temp|coretemp) for t in "$h"/temp*_input; do printf "%s " "$(cat "${t%_input}_label" 2>/dev/null)"; cat "$t"; done;; esac; done; for f in /sys/devices/system/cpu/cpu*/thermal_throttle/*throttle_count; do [ ! -f "$f" ] || { printf "%s " "$f"; cat "$f"; }; done',
            check=False,
        )
        (out / "cpu-before.txt").write_text(sensor_info.stdout + sensor_info.stderr)
        props = http(forward.base, "/props")
        save(out / "props.json", props)
        ctx = props.get("default_generation_settings", {}).get("n_ctx")
        if props.get("total_slots") != slots or ctx != 8192:
            raise RuntimeError(
                f"Unexpected slot/context properties: slots={props.get('total_slots')}, n_ctx={ctx}"
            )
        gpu = kube(
            "exec",
            POD,
            "--",
            "nvidia-smi",
            "--query-gpu=memory.used",
            "--format=csv,noheader,nounits",
        ).stdout
        if float(gpu.strip()) < (4000 if model == "4b" else 1000):
            raise RuntimeError("insufficient GPU residency")
        monitor = Monitor(out / "telemetry.jsonl", forward.base)
        monitor.start()
        dim = MODELS[model][1]
        base = forward.base
        summary = {
            "started_at": now(),
            "model": model,
            "slots": slots,
            "thermal_guard": {"cpu_c": 80, "gpu_c": 85},
            "phases": [],
        }
        save(out / "summary.json", summary)
        if f"{model}-s{slots}" in getattr(args, "quality_only_arms", []):
            summary["performance_skipped"] = True
            quality(base, corpus, lengths, questions, dim, slots, monitor, out)
            monitor.check()
            summary["completed_at"] = now()
            save(out / "summary.json", summary)
            return
        _, perf = batch(
            base,
            [(i, r["text"]) for i, r in enumerate(sample)],
            dim,
            1,
            monitor,
            out / "sequential.jsonl",
            True,
        )
        summary["phases"].append({"phase": "sequential", **perf})
        save(out / "summary.json", summary)
        print(out.name + " sequential complete", flush=True)
        for concurrency in args.client_concurrencies:
            for repeat in range(3):
                jobs = [(i, r["text"]) for i, r in enumerate(sample)]
                random.Random(1218 + repeat).shuffle(jobs)
                before = http(base, "/metrics", raw=True)
                _, perf = batch(
                    base,
                    jobs,
                    dim,
                    concurrency,
                    monitor,
                    out / f"c{concurrency}-r{repeat}.jsonl",
                )
                phase = {
                    "phase": "throughput",
                    "concurrency": concurrency,
                    "repeat": repeat,
                    **perf,
                    "metrics_before": before,
                    "metrics_after": http(base, "/metrics", raw=True),
                }
                summary["phases"].append(phase)
                save(out / "summary.json", summary)
                print(
                    f"{out.name} c{concurrency} r{repeat}: {perf['response_tokens_per_second']:.0f} tok/s",
                    flush=True,
                )
        short = min(sample, key=lambda r: abs(r["tokens"] - 300))["text"]
        long = max(sample, key=lambda r: r["tokens"])["text"]
        mixed = []
        for first, second, order in (
            (long, short, "long-short"),
            (short, long, "short-long"),
        ):
            for repeat in range(3):
                first_text = first
                second_text = second
                if args.fresh_mixed:
                    first_text = f"benchmark-{order}-{repeat}-first\n" + first
                    second_text = f"benchmark-{order}-{repeat}-second\n" + second
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    a = pool.submit(
                        embed, base, first_text, dim, "first", monitor, False
                    )
                    time.sleep(0.1)
                    b = pool.submit(
                        embed, base, second_text, dim, "second", monitor, False
                    )
                    mixed.append(
                        {
                            "order": order,
                            "repeat": repeat,
                            "fresh_prefix": args.fresh_mixed,
                            "first": a.result(),
                            "second": b.result(),
                        }
                    )
        save(out / "mixed.json", mixed)
        if args.skip_quality:
            summary["quality_skipped"] = True
        else:
            quality(base, corpus, lengths, questions, dim, slots, monitor, out)
        monitor.check()
        summary["completed_at"] = now()
        save(out / "summary.json", summary)
    finally:
        if monitor:
            monitor.close()
        try:
            log = kube("logs", POD, "-c", "llamacpp", check=False)
            (out / "server.log").write_text(log.stdout + log.stderr)
        finally:
            if forward:
                forward.close()
            kube(
                "delete",
                "pod",
                POD,
                "--ignore-not-found",
                "--wait=true",
                "--timeout=60s",
                timeout=70,
            )


def restore(snapshot, out):
    current = obj("get", "deployment", DEPLOY)
    if (
        current["metadata"]["uid"] != snapshot["metadata"]["uid"]
        or current["spec"]["template"] != snapshot["spec"]["template"]
    ):
        raise RuntimeError(
            "Production template/identity changed concurrently; refusing blind restore"
        )
    replicas = snapshot["spec"]["replicas"]
    kube("scale", "deployment/" + DEPLOY, f"--replicas={replicas}")
    kube("rollout", "status", "deployment/" + DEPLOY, "--timeout=420s", timeout=430)
    forward = Forward("deployment/" + DEPLOY, out / "restore-port-forward.log")
    try:
        forward.ready(240)
        response = http(
            forward.base,
            "/v1/embeddings",
            {
                "model": "restore-check",
                "input": ["Embedding service restoration check."],
                "cache_prompt": False,
            },
        )
        normalize(response["data"][0]["embedding"], 2560)
        result = {
            "at": now(),
            "replicas": replicas,
            "health": "ok",
            "embedding_dimension": 2560,
            "template_unchanged": True,
        }
        save(out / "restored.json", result)
        print("PRODUCTION RESTORED " + json.dumps(result), flush=True)
    finally:
        forward.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--corpus", type=Path, required=True)
    ap.add_argument("--paired-vectors", type=Path, required=True)
    ap.add_argument("--questions", type=Path, required=True)
    ap.add_argument("--sample", type=Path, required=True)
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--window-minutes", type=int, default=140)
    choices = ("4b-s1", "06b-s1", "06b-s2", "06b-s4")
    ap.add_argument("--arms", nargs="+", choices=choices, default=list(choices))
    ap.add_argument("--quality-only-arms", nargs="*", choices=choices, default=[])
    ap.add_argument("--skip-quality", action="store_true")
    ap.add_argument("--fresh-mixed", action="store_true")
    ap.add_argument(
        "--client-concurrencies",
        nargs="+",
        type=int,
        choices=(1, 2, 4),
        default=[1, 2, 4],
    )
    args = ap.parse_args()
    if len(set(args.arms)) != len(args.arms):
        ap.error("--arms must not contain duplicates")
    if not set(args.quality_only_arms).issubset(args.arms):
        ap.error("quality-only arms must be selected in --arms")
    if args.quality_only_arms and args.skip_quality:
        ap.error("quality-only arms cannot skip quality")
    if len(set(args.client_concurrencies)) != len(args.client_concurrencies):
        ap.error("client concurrency values must be unique")
    if args.window_minutes <= 10:
        ap.error("window must reserve more than ten minutes for restoration")
    corpus = [json.loads(x) for x in args.corpus.read_text().splitlines()]
    lengths = {
        r["entry_id"]: r
        for r in map(json.loads, args.paired_vectors.read_text().splitlines())
    }
    questions = json.loads(args.questions.read_text())["questions"]
    sample = json.loads(args.sample.read_text())
    if len(corpus) != 1280 or len(lengths) != 1280 or len(sample) != 44:
        raise ValueError("wrong input fixture")
    for r in corpus:
        ref = lengths[r["entry_id"]]
        if (
            hashlib.sha256(r["text"][: ref["input_chars"]].encode()).hexdigest()
            != ref["input_sha256"]
        ):
            raise ValueError("paired corpus differs")
    args.output.mkdir(parents=True, exist_ok=False)
    arms = [(name.split("-s")[0], int(name.split("-s")[1])) for name in args.arms]
    for model, slots in arms:
        save(args.output / f"{model}-s{slots}.json", manifest(model, slots))
    save(
        args.output / "provenance.json",
        {
            "created_at": now(),
            "image": IMAGE,
            "inputs": {
                str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in (args.corpus, args.paired_vectors, args.questions, args.sample)
            },
            "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "quality_scope": "same frozen whole-entry corpus; no OV writes or chunked retrieval",
            "arms": arms,
            "quality_only_arms": args.quality_only_arms,
            "skip_quality": args.skip_quality,
            "fresh_mixed": args.fresh_mixed,
            "client_concurrencies": args.client_concurrencies,
        },
    )
    if not args.execute:
        print(f"Rendered {len(arms)} manifests; inputs verified; no cluster mutation")
        return
    snapshot = obj("get", "deployment", DEPLOY)
    if snapshot["spec"]["replicas"] != 1:
        raise RuntimeError("expected one production replica")
    production = obj("get", "pods", "-l", "app=embedder-qwen-cuda")["items"]
    if (
        len(production) != 1
        or production[0]["status"]["containerStatuses"][0]["imageID"] != IMAGE
    ):
        raise RuntimeError(
            "production runtime image no longer matches the pinned experiment"
        )
    pods = json.loads(
        subprocess.check_output(
            [
                "kubectl",
                "get",
                "pods",
                "-A",
                "--field-selector",
                "spec.nodeName=manu",
                "-o",
                "json",
            ],
            text=True,
            timeout=30,
        )
    )["items"]
    occupants = []
    for pod in pods:
        if pod["status"].get("phase") in ("Succeeded", "Failed"):
            continue
        gpu = any(
            c.get("resources", {}).get("limits", {}).get("nvidia.com/gpu")
            for c in pod["spec"]["containers"]
        )
        if gpu and pod["metadata"]["name"] != production[0]["metadata"]["name"]:
            occupants.append(
                pod["metadata"]["namespace"] + "/" + pod["metadata"]["name"]
            )
    if occupants:
        raise RuntimeError(f"Other GPU workloads on manu: {occupants}")
    save(
        args.output / "node-workloads-before.json",
        [
            {
                "namespace": p["metadata"]["namespace"],
                "pod": p["metadata"]["name"],
                "phase": p["status"].get("phase"),
            }
            for p in pods
        ],
    )
    existing = kube("get", "pod", POD, "--ignore-not-found").stdout
    if existing.strip():
        raise RuntimeError("benchmark Pod already exists")
    save(args.output / "production-before.json", snapshot)

    def interrupted(signum, frame):
        raise RuntimeError(f"interrupted: signal {signum}")

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGALRM, interrupted)
    try:
        save(
            args.output / "outage-start.json",
            {"at": now(), "budget_minutes": args.window_minutes},
        )
        signal.alarm((args.window_minutes - 10) * 60)
        kube("scale", "deployment/" + DEPLOY, "--replicas=0")
        kube(
            "wait",
            "--for=delete",
            "pod",
            "-l",
            "app=embedder-qwen-cuda",
            "--timeout=180s",
            timeout=190,
        )
        for model, slots in arms:
            print(f"START {model} slots={slots} at {now()}", flush=True)
            run_arm(model, slots, args, corpus, lengths, questions, sample)
    except BaseException as exc:
        save(
            args.output / "error.json",
            {"at": now(), "type": type(exc).__name__, "error": str(exc)},
        )
        raise
    finally:
        signal.alarm(0)
        cleanup_restore(snapshot, args.output)


def cleanup_restore(snapshot, output):
    try:
        kube(
            "delete",
            "pod",
            POD,
            "--ignore-not-found",
            "--wait=true",
            "--timeout=60s",
            timeout=70,
        )
    finally:
        # Even a failed cleanup must attempt to bring the production replica back.
        restore(snapshot, output)


if __name__ == "__main__":
    main()
