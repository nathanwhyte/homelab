#!/usr/bin/env python3
"""TASK-1218: one capped EmbeddingGemma 2 window on manu's GTX 1080 (llama.cpp b11580).

Modes (default: --dry-run):
  --dry-run   render the probe Pod and the request plan; no kubectl write verb
  --prepull   cache the b11580 image on manu with a GPU-less `llama-server --version`
              Pod, check the build, delete the Pod; production is not touched
  --execute   snapshot embedder-qwen-cuda, scale it to 0, run the isolated probe Pod
              (no Service selects it), measure, and restore production on every exit
  --evaluate  re-score a finished window's records.json, optionally with gate 4's
              cosines from the pop reference run (--cosines)

b11580 has the attention padding fix (ggml-org/llama.cpp#30416) but not the
host-memory fix (#30255), so every embedded input is capped at 3,072 EG2 tokens and
an RSS guard deletes the Pod past 5 GiB. The token-exact pairs are sliced from the
longest sample text, which is only tokenized, never embedded whole.

Plan: compendium docs/plans/2026-10-13-TASK-1218-eg2-gtx1080-probe.md.

  python3 probe_eg2_1080.py --sample sample.json --dry-run
  python3 probe_eg2_1080.py --prepull
  python3 probe_eg2_1080.py --sample sample.json --execute \\
    --output results-local/task1218-eg2-probe-<UTC>
"""

import argparse
import hashlib
import json
import math
import random
import re
import signal
import statistics
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import latency_local
from benchmark_1080_slots import (
    DEPLOY,
    Forward,
    Monitor,
    cleanup_restore,
    http,
    kube,
    now,
    obj,
    save,
)

POD = "task1218-eg2-probe"
PREPULL = "task1218-eg2-prepull"
# server-cuda-b11580 index digest (same as server-cuda12-b11580), registry 2026-10-13.
IMAGE = "ghcr.io/ggml-org/llama.cpp@sha256:c4348afb91499b10fad12f92626c7a8ca8b69d6475d10c793b810cb61237ce00"
BUILD = 11580
PROD_IMAGE = "ghcr.io/ggml-org/llama.cpp@sha256:ef08b5a98b1170f2b62177be0a4027c88a84043c55afbf190dd18b9e7cdcfebf"
EG2_DEPLOY = "embedder-eg2-cuda"
DIMENSION = 768
CAP = 3072  # EG2 tokens per embedded input
PREFIX_MARGIN = 24  # latency_local.fresh() adds 16 random characters and a space
TARGETS = (300, 1000, 3000)
PAIRS = ((1024, 1025), (2048, 2049), (2816, 2817))
REPEATS = 3
SEED = 1218
RSS_LIMIT_KIB = 5 * 1024 * 1024
DEADLINE_MINUTES = 20  # scale-down to restored
RESTORE_RESERVE_S = 240  # measurements stop this long before the deadline
# Qwen3-Embedding-4B Q8_0, one slot, this card: task1218-1080-slots-20260923.md:25-32.
QWEN_MEDIANS_MS = {300: 326, 1000: 1108, 3000: 3644}
PAIR_RATIO_MAX = 1.5
COSINE_MIN = 0.999
# b11580 src/llama-context.cpp:629-645, the -fa auto probe's verdict lines.
FA_ENABLED = "Flash Attention enabled"
FA_FALLBACK = ("Flash Attention is assigned to device", "Flash Attention not supported")

MODEL_ENV = [
    {"name": "MODEL_FILE", "value": "embeddinggemma-2-Q8_0.gguf"},
    {
        "name": "MODEL_URL",
        "value": "https://huggingface.co/ggml-org/embeddinggemma-2-GGUF/resolve/main/embeddinggemma-2-Q8_0.gguf",
    },
    {
        "name": "MODEL_SHA256",
        "value": "2188ac1deca4b77dffefd603c2776a9d76d9d74ec01841392982ebb840b09135",
    },
]
MODEL_FETCH = """set -eu
model_path="/models/${MODEL_FILE}"
if [ ! -f "${model_path}" ]; then
  echo "Downloading ${MODEL_FILE}..."
  curl -fL --retry 5 --retry-delay 10 "${MODEL_URL}" -o "${model_path}.tmp"
  mv "${model_path}.tmp" "${model_path}"
fi
echo "${MODEL_SHA256}  ${model_path}" | sha256sum -c -
ln -sf "${model_path}" /models/current.gguf
echo "Model ready."
"""
# embedder-eg2-cuda's args (viking/manifests/embedder-eg2-cuda-deployment.yaml).
EG2_ARGS = [
    "--model",
    "/models/current.gguf",
    "--host",
    "0.0.0.0",
    "--port",
    "8000",
    "--embedding",
    "--pooling",
    "mean",
    "--ctx-size",
    "8192",
    "--batch-size",
    "8192",
    "--ubatch-size",
    "8192",
    "--parallel",
    "1",
    "--cache-ram",
    "0",
    "--n-gpu-layers",
    "999",
    "--threads",
    "4",
    "--split-mode",
    "none",
    "--main-gpu",
    "0",
    "--metrics",
]
# auto is what production would run. Verbosity 3 keeps the INFO and WARN lines
# gate 1 reads; -lv 1 would drop both (common/log.h: WARN 2, INFO 3).
PROBE_ARGS = EG2_ARGS + ["--flash-attn", "auto", "--verbosity", "3"]


def manifest():
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        # No Service selects this label, so OV never routes to the probe.
        "metadata": {"name": POD, "namespace": "viking", "labels": {"app": POD}},
        "spec": {
            "restartPolicy": "Never",
            # Backstop if the runner dies: Kubernetes kills the Pod, freeing the GPU.
            "activeDeadlineSeconds": DEADLINE_MINUTES * 60 + 300,
            "terminationGracePeriodSeconds": 15,
            "runtimeClassName": "nvidia",
            "nodeSelector": {"kubernetes.io/hostname": "manu", "gpu.vendor": "nvidia"},
            "securityContext": {"fsGroup": 0},
            "initContainers": [
                {
                    "name": "model-fetch",
                    "image": "curlimages/curl:8.12.1",
                    "command": ["/bin/sh", "-c"],
                    "args": [MODEL_FETCH],
                    "env": MODEL_ENV,
                    "securityContext": {"runAsUser": 0, "runAsGroup": 0},
                    "volumeMounts": [{"name": "model-cache", "mountPath": "/models"}],
                }
            ],
            "containers": [
                {
                    "name": "llamacpp",
                    "image": IMAGE,
                    "command": ["/app/llama-server"],
                    "args": PROBE_ARGS,
                    "ports": [{"containerPort": 8000, "name": "http"}],
                    "resources": {
                        "requests": {
                            "cpu": "500m",
                            "memory": "1Gi",
                            "nvidia.com/gpu": "1",
                        },
                        "limits": {"cpu": "4", "memory": "6Gi", "nvidia.com/gpu": "1"},
                    },
                    "readinessProbe": {
                        "httpGet": {"path": "/health", "port": 8000},
                        "periodSeconds": 5,
                    },
                    "volumeMounts": [
                        {"name": "model-cache", "mountPath": "/models"},
                        {"name": "shm", "mountPath": "/dev/shm"},
                    ],
                }
            ],
            "volumes": [
                {
                    "name": "model-cache",
                    "persistentVolumeClaim": {"claimName": "embedder-eg2-model-cache"},
                },
                {"name": "shm", "emptyDir": {"medium": "Memory", "sizeLimit": "1Gi"}},
            ],
        },
    }


def prepull_manifest():
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": PREPULL,
            "namespace": "viking",
            "labels": {"app": PREPULL},
        },
        "spec": {
            "restartPolicy": "Never",
            "activeDeadlineSeconds": 900,
            "nodeSelector": {"kubernetes.io/hostname": "manu"},
            # No GPU request and no nvidia runtime: production keeps the card.
            "containers": [
                {
                    "name": "version",
                    "image": IMAGE,
                    "imagePullPolicy": "IfNotPresent",
                    "command": ["/app/llama-server", "--version"],
                    "env": [{"name": "NVIDIA_VISIBLE_DEVICES", "value": "void"}],
                    "resources": {
                        "requests": {"cpu": "50m", "memory": "64Mi"},
                        "limits": {"cpu": "1", "memory": "512Mi"},
                    },
                }
            ],
        },
    }


def select_inputs(sample):
    """The 300 / 1,000 / 3,000 targets whose Qwen count leaves room under the cap."""
    return [
        {
            "index": i,
            "target": s["target"],
            "qwen_tokens": s["tokens"],
            "text": s["text"],
        }
        for i, s in enumerate(sample)
        if s["target"] in TARGETS and s["tokens"] + PREFIX_MARGIN <= CAP
    ]


def cap_inputs(texts, count):
    """Split texts by EG2's own count (`count(text)`); a kept text fits with its prefix."""
    kept, dropped = [], []
    for item in texts:
        tokens = count(item["text"])
        row = {**item, "eg2_tokens": tokens}
        (kept if tokens + PREFIX_MARGIN <= CAP else dropped).append(row)
    if any(r["eg2_tokens"] + PREFIX_MARGIN > CAP for r in kept):
        raise AssertionError("cap_inputs kept a text over the cap")
    return kept, dropped


def check_vector(vector):
    if len(vector) != DIMENSION or not all(math.isfinite(x) for x in vector):
        raise ValueError(f"bad embedding: dimension {len(vector)}")
    norm = math.sqrt(sum(x * x for x in vector))
    if norm == 0:
        raise ValueError("zero embedding")
    return norm


def median(values):
    return statistics.median(values) if values else None


def evaluate(records, cosines=None):
    """Gate verdicts with their numbers. A gate with no measurement fails."""
    lines = (records.get("startup_log") or "").splitlines()
    fallback = [line for line in lines if any(s in line for s in FA_FALLBACK)]
    enabled = any(FA_ENABLED in line for line in lines)
    gate1 = {
        "pass": enabled and not fallback,
        "enabled_line": enabled,
        "fallback_lines": fallback[:5],
    }

    ratios = {}
    for a, b in PAIRS:
        ma = median(
            [
                r["ms"]
                for r in records.get("pairs", [])
                if r["length"] == a and "ms" in r
            ]
        )
        mb = median(
            [
                r["ms"]
                for r in records.get("pairs", [])
                if r["length"] == b and "ms" in r
            ]
        )
        name = f"{a}/{b}"
        if ma is None or mb is None:
            ratios[name] = {"pass": False, "measured": False}
            continue
        # Either direction counts: the slower of the two over the faster.
        ratio = max(ma, mb) / min(ma, mb)
        ratios[name] = {
            "pass": ratio <= PAIR_RATIO_MAX,
            "median_ms": [round(ma, 1), round(mb, 1)],
            "ratio": round(ratio, 3),
        }
    gate2 = {"pass": all(r["pass"] for r in ratios.values()), "pairs": ratios}

    buckets = {}
    for target, baseline in QWEN_MEDIANS_MS.items():
        rows = [r for r in records.get("buckets", []) if r["target"] == target]
        value = median([r["ms"] for r in rows if "ms" in r])
        buckets[str(target)] = {
            "pass": value is not None and value < baseline,
            "requests": len(rows),
            "median_ms": None if value is None else round(value, 1),
            "qwen_median_ms": baseline,
        }
    gate3 = {"pass": all(b["pass"] for b in buckets.values()), "buckets": buckets}

    if cosines is None:
        gate4 = {"pass": None, "pending": "phase 3 reference vectors on pop"}
    else:
        gate4 = {
            "pass": cosines["min"] >= COSINE_MIN,
            "min": cosines["min"],
            "median": cosines.get("median"),
            "threshold": COSINE_MIN,
        }

    gates = [gate1, gate2, gate3, gate4]
    if any(g["pass"] is False for g in gates):
        verdict = "FAIL"
    elif gate4["pass"] is None:
        verdict = "PENDING"
    else:
        verdict = "PASS"
    return {
        "verdict": verdict,
        "guard": records.get("guard"),
        "gate1_attention_on_gpu": gate1,
        "gate2_no_length_cliff": gate2,
        "gate3_faster_than_qwen": gate3,
        "gate4_reference_vectors": gate4,
    }


def plan(sample, kept):
    return {
        "image": IMAGE,
        "pod": POD,
        "args": PROBE_ARGS,
        "cap_eg2_tokens": CAP,
        "inputs": [
            {
                "index": r["index"],
                "target": r["target"],
                "qwen_tokens": r["qwen_tokens"],
            }
            for r in kept
        ],
        "inputs_by_target": dict(Counter(r["target"] for r in kept)),
        "pair_lengths": [n for pair in PAIRS for n in pair],
        "pair_token_source_index": sample.index(max(sample, key=lambda s: s["tokens"])),
        "repeats": REPEATS,
        "requests": 1 + 1 + REPEATS * (2 * len(PAIRS) + len(kept)) + REPEATS * 4,
        "gates": {
            "pair_ratio_max": PAIR_RATIO_MAX,
            "qwen_medians_ms": QWEN_MEDIANS_MS,
            "cosine_min": COSINE_MIN,
        },
        "guards": {
            "rss_kib": RSS_LIMIT_KIB,
            "cpu_c": 80,
            "gpu_c": 85,
            "deadline_minutes": DEADLINE_MINUTES,
            "restore_reserve_seconds": RESTORE_RESERVE_S,
        },
    }


def load_sample(path):
    sample = json.loads(path.read_text())
    counts = Counter(s["target"] for s in sample)
    if len(sample) != 44 or counts != {300: 12, 1000: 10, 3000: 10, 6000: 6, 7600: 6}:
        raise ValueError(f"wrong sample fixture: {dict(counts)}")
    return sample


def preflight():
    snapshot = obj("get", "deployment", DEPLOY)
    image = snapshot["spec"]["template"]["spec"]["containers"][0]["image"]
    if snapshot["spec"]["replicas"] != 1 or image != PROD_IMAGE:
        raise RuntimeError(f"{DEPLOY} is not 1 replica on the pinned digest: {image}")
    production = obj("get", "pods", "-l", "app=" + DEPLOY)["items"]
    if len(production) != 1 or not production[0]["status"]["containerStatuses"][0][
        "imageID"
    ].endswith(PROD_IMAGE.split("@")[1]):
        raise RuntimeError("production runtime image does not match the pinned digest")
    if obj("get", "deployment", EG2_DEPLOY)["spec"]["replicas"] != 0:
        raise RuntimeError(f"{EG2_DEPLOY} is not parked at 0")
    if kube("get", "pod", POD, "--ignore-not-found").stdout.strip():
        raise RuntimeError("probe Pod already exists")
    pods = json.loads(
        kube(
            "get",
            "pods",
            "-A",
            "--field-selector",
            "spec.nodeName=manu",
            "-o",
            "json",
            timeout=30,
        ).stdout
    )["items"]
    others = [
        p["metadata"]["namespace"] + "/" + p["metadata"]["name"]
        for p in pods
        if p["status"].get("phase") not in ("Succeeded", "Failed")
        and p["metadata"]["name"] != production[0]["metadata"]["name"]
        and any(
            c.get("resources", {}).get("limits", {}).get("nvidia.com/gpu")
            for c in p["spec"]["containers"]
        )
    ]
    if others:
        raise RuntimeError(f"other GPU workloads on manu: {others}")
    return snapshot


def run_window(out, snapshot, budget_s, body):
    """Scale production to 0, run body(), and restore it on every exit."""

    def interrupted(signum, frame):
        if signum == signal.SIGALRM:
            raise RuntimeError("deadline reached")
        raise RuntimeError(f"interrupted: signal {signum}")

    handled = (signal.SIGTERM, signal.SIGINT, signal.SIGALRM)
    previous = {s: signal.signal(s, interrupted) for s in handled}
    window = {
        "deadline_minutes": DEADLINE_MINUTES,
        "measure_budget_seconds": budget_s,
    }
    try:
        signal.alarm(budget_s)
        window["scale_down_at"] = now()
        save(out / "window.json", window)
        kube("scale", "deployment/" + DEPLOY, "--replicas=0")
        kube(
            "wait",
            "--for=delete",
            "pod",
            "-l",
            "app=" + DEPLOY,
            "--timeout=180s",
            timeout=190,
        )
        body()
    except BaseException as exc:
        window["error"] = {"at": now(), "type": type(exc).__name__, "error": str(exc)}
        raise
    finally:
        signal.alarm(0)
        # A second Ctrl-C must not cut the restore short.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        try:
            cleanup_restore(snapshot, out, pod=POD)
            window["restored_at"] = now()
        finally:
            window["finished_at"] = now()
            save(out / "window.json", window)
            for s, handler in previous.items():
                signal.signal(s, handler)


def tokenize(base, text, add_special=True):
    return http(
        base,
        "/tokenize",
        {"content": text, "add_special": add_special, "parse_special": True},
    )["tokens"]


def measure(base, monitor, texts, pair_source, scratch, records, flush):
    rng = random.Random(SEED)
    vectors = (scratch / "vectors.jsonl").open("w")

    def embed(key, value):
        monitor.check()
        start = time.monotonic()
        response = http(
            base,
            "/v1/embeddings",
            {"model": "probe", "input": [value], "cache_prompt": False},
            timeout=300,
        )
        ms = (time.monotonic() - start) * 1000
        vector = response["data"][0]["embedding"]
        norm = check_vector(vector)
        tokens = (response.get("usage") or {}).get("prompt_tokens")
        if tokens is None or tokens > CAP:
            raise RuntimeError(f"{key}: {tokens} prompt tokens, cap {CAP}")
        vectors.write(
            json.dumps({"key": key, "input": value, "tokens": tokens, "vector": vector})
            + "\n"
        )
        vectors.flush()
        return {"key": key, "ms": round(ms, 1), "tokens": tokens, "norm": norm}

    try:
        records["first"] = embed("first", "probe warm-up request")
        flush()

        # Token arrays are sent as-is (server-common.cpp tokenize_mixed adds no BOS);
        # the calibration request proves it on this build.
        ids = tokenize(base, pair_source, add_special=False)
        longest = max(n for pair in PAIRS for n in pair)
        if len(ids) < longest + REPEATS:
            raise RuntimeError(f"pair source has {len(ids)} tokens, need {longest}")
        calibration = embed("calibrate", ids[:16])
        offset = calibration["tokens"] - 16
        records["calibration"] = {**calibration, "offset": offset}
        if not 0 <= offset <= 2:
            raise RuntimeError(f"token-array offset {offset}")
        for repeat in range(REPEATS):
            for a, b in PAIRS:
                for length in (a, b) if repeat % 2 == 0 else (b, a):
                    row = embed(
                        f"pair-{length}-r{repeat}",
                        ids[repeat : repeat + length - offset],
                    )
                    if row["tokens"] != length:
                        raise RuntimeError(f"pair {length} sent {row['tokens']} tokens")
                    records["pairs"].append({"length": length, "repeat": repeat, **row})
                    flush()
        print(f"pairs done at {now()}", flush=True)

        for repeat in range(REPEATS):
            for item in texts:
                text = latency_local.fresh(item["text"], rng)
                counted = len(tokenize(base, text))
                if counted > CAP:
                    raise RuntimeError(f"text {item['index']}: {counted} tokens")
                row = embed(f"bucket-{item['target']}-{item['index']}-r{repeat}", text)
                records["buckets"].append(
                    {
                        "target": item["target"],
                        "index": item["index"],
                        "repeat": repeat,
                        "counted_tokens": counted,
                        **row,
                    }
                )
                flush()
            print(f"buckets repeat {repeat + 1}/{REPEATS} at {now()}", flush=True)

        short = min(
            (t for t in texts if t["target"] == 300), key=lambda t: t["eg2_tokens"]
        )
        long = max(texts, key=lambda t: t["eg2_tokens"])
        args = SimpleNamespace(
            backend="openai", url=base + "/v1/embeddings", model="probe"
        )
        records["ordering_inputs"] = {
            "short_index": short["index"],
            "long_index": long["index"],
            "long_eg2_tokens": long["eg2_tokens"],
        }
        for repeat in range(REPEATS):
            monitor.check()
            l1, s1, gap1 = latency_local.pair(
                args,
                latency_local.fresh(long["text"], rng),
                latency_local.fresh(short["text"], rng),
            )
            s2, l2, gap2 = latency_local.pair(
                args,
                latency_local.fresh(short["text"], rng),
                latency_local.fresh(long["text"], rng),
            )
            errors = [r["error"] for r in (l1, s1, s2, l2) if "error" in r]
            if gap1 <= 0 or gap2 <= 0:
                errors.append("ordering reversed")
            if s1["start"] >= l1["end"]:
                errors.append("short sent after long finished; no queueing measured")
            records["ordering"].append(
                {
                    "repeat": repeat,
                    "short_behind_long_ms": round(s1["ms"], 1),
                    "long_ms_when_first": round(l1["ms"], 1),
                    "short_first_ms": round(s2["ms"], 1),
                    "long_ms_when_second": round(l2["ms"], 1),
                    "gap_ms": [round(gap1, 1), round(gap2, 1)],
                    "errors": errors,
                }
            )
            flush()
    finally:
        vectors.close()


def execute(args, sample, kept_by_qwen):
    out = args.output
    scratch = Path(__file__).parent / "scratch" / out.name
    out.mkdir(parents=True, exist_ok=False)
    scratch.mkdir(parents=True, exist_ok=False)
    pod_path = out / "pod.json"
    save(pod_path, manifest())
    save(
        out / "provenance.json",
        {
            "created_at": now(),
            "sample": str(args.sample),
            "sample_sha256": hashlib.sha256(args.sample.read_bytes()).hexdigest(),
            "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "plan": plan(sample, kept_by_qwen),
            "vectors": str(scratch),
        },
    )
    snapshot = preflight()
    save(out / "production-before.json", snapshot)
    records = {"pairs": [], "buckets": [], "ordering": [], "guard": None}

    def flush():
        save(out / "records.json", records)

    def body():
        forward = monitor = None
        try:
            kube("create", "-f", str(pod_path))
            kube(
                "wait",
                "--for=condition=Ready",
                "pod/" + POD,
                "--timeout=420s",
                timeout=430,
            )
            log = kube("logs", POD, "-c", "llamacpp", check=False)
            records["startup_log"] = log.stdout + log.stderr
            (out / "startup.log").write_text(records["startup_log"])
            pod = obj("get", "pod", POD)
            image_id = pod["status"]["containerStatuses"][0]["imageID"]
            save(
                out / "runtime.json",
                {
                    "node": pod["spec"]["nodeName"],
                    "uid": pod["metadata"]["uid"],
                    "image_id": image_id,
                    "args": pod["spec"]["containers"][0]["args"],
                },
            )
            if not image_id.endswith(IMAGE.split("@")[1]):
                raise RuntimeError(f"probe runs {image_id}, not b11580")
            flush()
            gate1 = evaluate(records)["gate1_attention_on_gpu"]
            print(f"gate 1 (startup log): {json.dumps(gate1)}", flush=True)
            forward = Forward("pod/" + POD, out / "port-forward.log")
            forward.ready()
            save(out / "props.json", http(forward.base, "/props"))
            monitor = Monitor(
                out / "telemetry.jsonl",
                forward.base,
                pod=POD,
                rss_limit_kib=RSS_LIMIT_KIB,
            )
            monitor.start()
            count = lambda text: len(tokenize(forward.base, text))
            texts, dropped = cap_inputs(kept_by_qwen, count)
            records["inputs"] = [
                {k: r[k] for k in ("index", "target", "qwen_tokens", "eg2_tokens")}
                for r in texts
            ]
            records["dropped_over_cap"] = [
                {"index": r["index"], "eg2_tokens": r["eg2_tokens"]} for r in dropped
            ]
            flush()
            pair_source = max(sample, key=lambda s: s["tokens"])["text"]
            measure(forward.base, monitor, texts, pair_source, scratch, records, flush)
            monitor.check()
        except BaseException:
            if monitor and monitor.reason:
                records["guard"] = monitor.reason
            raise
        finally:
            if monitor:
                monitor.close()
                records["guard"] = records["guard"] or monitor.reason
            flush()
            try:
                log = kube("logs", POD, "-c", "llamacpp", check=False)
                (out / "server.log").write_text(log.stdout + log.stderr)
            finally:
                if forward:
                    forward.close()

    budget = DEADLINE_MINUTES * 60 - RESTORE_RESERVE_S
    try:
        run_window(out, snapshot, budget, body)
    finally:
        verdict = evaluate(records)
        save(out / "verdict.json", verdict)
        print("VERDICT " + json.dumps(verdict, indent=2), flush=True)


def prepull():
    if kube("get", "pod", PREPULL, "--ignore-not-found").stdout.strip():
        raise RuntimeError("pre-pull Pod already exists")
    kube("create", "-f", "-", data=json.dumps(prepull_manifest()))
    try:
        deadline = time.monotonic() + 900
        phase = None
        while time.monotonic() < deadline:
            phase = obj("get", "pod", PREPULL)["status"].get("phase")
            if phase in ("Succeeded", "Failed"):
                break
            time.sleep(5)
        log = kube("logs", PREPULL, check=False)
        text = log.stdout + log.stderr
        print(f"pre-pull phase {phase}\n{text.strip()}", flush=True)
        build = re.search(r"version: (\d+)", text)
        if not build or int(build.group(1)) != BUILD:
            raise RuntimeError(f"expected build {BUILD}")
        print(f"build {BUILD} confirmed; image cached on manu", flush=True)
    finally:
        kube(
            "delete",
            "pod",
            PREPULL,
            "--ignore-not-found",
            "--wait=true",
            "--timeout=60s",
            timeout=70,
        )


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--prepull", action="store_true")
    mode.add_argument("--execute", action="store_true")
    mode.add_argument("--evaluate", type=Path, metavar="WINDOW_DIR")
    ap.add_argument("--sample", type=Path)
    ap.add_argument("--output", type=Path)
    ap.add_argument(
        "--cosines", type=Path, help='JSON {"min": x, "median": y} for gate 4'
    )
    args = ap.parse_args()

    if args.prepull:
        prepull()
        return
    if args.evaluate:
        records = json.loads((args.evaluate / "records.json").read_text())
        cosines = json.loads(args.cosines.read_text()) if args.cosines else None
        print(json.dumps(evaluate(records, cosines), indent=2))
        return
    if not args.sample:
        ap.error("--sample is required")
    sample = load_sample(args.sample)
    kept = select_inputs(sample)
    if args.execute:
        if not args.output:
            ap.error("--execute needs --output")
        execute(args, sample, kept)
        return
    print(json.dumps({"pod": manifest(), "plan": plan(sample, kept)}, indent=2))
    print("dry run: no cluster call made", flush=True)


if __name__ == "__main__":
    main()
