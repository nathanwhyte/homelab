#!/usr/bin/env python3
"""TASK-1218: one EmbeddingGemma 2 window on manu's GTX 1080 (llama.cpp b11610).

Modes (default: --dry-run):
  --dry-run   render the probe Pod and the request plan; no kubectl write verb
  --prepull   cache the b11610 image on manu with a GPU-less `llama-server --version`
              Pod, check the build, delete the Pod; production is not touched
  --execute   snapshot embedder-qwen-cuda, scale it to 0, run the isolated probe Pod
              (no Service selects it), measure, and restore production on every exit
  --evaluate  re-score a finished window's records.json, optionally with gate 4's
              cosines from the pop reference run (--cosines)
  --rehearse  run the --execute request plan against a local llama-server (--base-url,
              started with PROBE_ARGS; --server-log is its log) and fail unless every
              setup defect a window cannot afford is absent; touches no cluster, writes
              rehearsal.json. --execute refuses without --rehearsal pointing at a
              passing one for the same plan (--skip-rehearsal overrides, recorded)

b11610 has the attention padding fix (ggml-org/llama.cpp#30416, b11576) and the
host-memory fix (#30255, b11588), so inputs run up to the 8,192-token context. The
first window (2026-10-15, b11580) capped them at 3,072 for want of #30255. The RSS
guard still deletes the Pod past 5 GiB. The token-exact pairs are sliced from the
longest sample text. Token arrays carry the model's BOS/EOS specials, as OV's do,
except the --queries `frag-N` inputs, which are special-free on purpose.

Plan: compendium docs/plans/2026-10-13-TASK-1218-eg2-gtx1080-probe.md.

  python3 probe_eg2_1080.py --sample sample.json --dry-run
  python3 probe_eg2_1080.py --prepull
  python3 probe_eg2_1080.py --sample sample.json --rehearse \\
    --base-url http://127.0.0.1:18093 --server-log server.log \\
    --output results-local/task1218-eg2-rehearsal-<UTC>
  python3 probe_eg2_1080.py --sample sample.json --execute \\
    --rehearsal results-local/task1218-eg2-rehearsal-<UTC>/rehearsal.json \\
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
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import latency_local
from benchmark_1080_slots import (
    DEPLOY,
    Forward,
    Monitor,
    ProductionChanged,
    cleanup_restore,
    http,
    kube,
    log,
    now,
    obj,
    save,
    scale_checked,
)

POD = "task1218-eg2-probe"
PREPULL = "task1218-eg2-prepull"
# server-cuda-b11610 index digest (same as server-cuda12-b11610), registry 2026-10-15.
# The first published server-cuda build after b11588; b11588-b11609 have no image.
IMAGE = "ghcr.io/ggml-org/llama.cpp@sha256:b0baccbe2e00b5d74e63f85f9f023c93d61d0f329475269e1e7002e87eb875ea"
BUILD = 11610
PROD_IMAGE = "ghcr.io/ggml-org/llama.cpp@sha256:ef08b5a98b1170f2b62177be0a4027c88a84043c55afbf190dd18b9e7cdcfebf"
EG2_DEPLOY = "embedder-eg2-cuda"
DIMENSION = 768
CAP = 8192  # EG2 tokens per embedded input: the --ctx-size
PREFIX_MARGIN = 24  # latency_local.fresh() adds 16 random characters and a space
TARGETS = (300, 1000, 3000, 6000, 7600)
PAIRS = (
    (1024, 1025),
    (2048, 2049),
    (2816, 2817),
    (4096, 4097),
    (6144, 6145),
    (7680, 7681),
)
REPEATS = 3
# --queries windows: token-fragment lengths around the 256 attention padding.
SHORT_LENGTHS = (8, 16, 32, 64, 128, 255, 256, 257)
SEED = 1218
RSS_LIMIT_KIB = 5 * 1024 * 1024
# scale-down to restored. 25 leaves 630 s to measure; the b11580 window used ~200 s
# for three buckets and three pairs, and the long inputs here roughly double that.
DEADLINE_MINUTES = 25
# Measurements stop this long before the deadline: one attempt of every restore
# step at its kubectl timeout (probe delete 70 s, get + fence 120 s, get + scale
# 120 s, rollout 310 s, verify get 60 s, port-forward readiness 120 s, vector
# check 60 s, fence annotation removal 60 s = 920 s, plus port-forward close; a
# fake-clock run of every first attempt took 909 s, Codex round 7). No retry
# starts after the deadline. Each step still runs once, so a restore whose retries
# used up the reserve ends in an error naming the step, never a silent abandon;
# window.json records `overran_deadline`.
RESTORE_RESERVE_S = 960
# Qwen3-Embedding-4B Q8_0, one slot, this card: task1218-1080-slots-20260923.md:25-32.
QWEN_MEDIANS_MS = {300: 326, 1000: 1108, 3000: 3644, 6000: 8789, 7600: 12238}
PAIR_RATIO_MAX = 1.5
COSINE_MIN = 0.999
# b11588 src/llama-context.cpp:629-645, the -fa auto probe's verdict lines.
FA_ENABLED = "Flash Attention enabled"
FA_FALLBACK = ("Flash Attention is assigned to device", "Flash Attention not supported")
FA_LOG_VERBOSITY = 4  # libllama logs the resolution at TRACE: common/log.cpp:532
REHEARSAL_URL = "http://127.0.0.1:18093"

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
# auto is what production would run. Gate 1 reads libllama's INFO "enabled" line,
# which common_log_get_verbosity maps to TRACE (4), not INFO (3): the 2026-10-15
# b11580 window at verbosity 3 logged no resolution line at all (common/log.cpp:532).
PROBE_ARGS = EG2_ARGS + ["--flash-attn", "auto", "--verbosity", "4"]


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


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def load_queries_digested(path):
    """The queries and the sha256 of the exact bytes parsed, from one read."""
    raw = path.read_bytes()
    queries = json.loads(raw)["queries"]
    ids = [q["id"] for q in queries]
    if not queries or len(set(ids)) != len(ids) or not all(q["text"] for q in queries):
        raise ValueError(f"{path}: need non-empty queries with unique ids and text")
    return queries, sha256(raw)


def load_queries(path):
    return load_queries_digested(path)[0]


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


def combine(results):
    """False if any measured part failed, None if any part is unmeasured, else True."""
    results = list(results)
    if any(r is False for r in results):
        return False
    if any(r is None for r in results):
        return None
    return True


def evaluate(records, cosines=None):
    """Gate verdicts with their numbers.

    A gate is judged only on what was measured (plan § Phase 2): an unmeasured
    gate is `pass: None`, never a failure, so a window a guard cut short reads
    INCOMPLETE rather than closing EG2 on missing data.
    """
    log = records.get("startup_log")
    lines = (log or "").splitlines()
    fallback = [line for line in lines if any(s in line for s in FA_FALLBACK)]
    enabled = any(FA_ENABLED in line for line in lines)
    gate1 = {
        # A captured log without the enabled line fails: --verbosity 3 prints it.
        "pass": None if not log else enabled and not fallback,
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
            ratios[name] = {"pass": None, "measured": False}
            continue
        # Either direction counts: the slower of the two over the faster.
        ratio = max(ma, mb) / min(ma, mb)
        ratios[name] = {
            "pass": ratio <= PAIR_RATIO_MAX,
            "median_ms": [round(ma, 1), round(mb, 1)],
            "ratio": round(ratio, 3),
        }
    gate2 = {"pass": combine(r["pass"] for r in ratios.values()), "pairs": ratios}

    buckets = {}
    for target, baseline in QWEN_MEDIANS_MS.items():
        rows = [r for r in records.get("buckets", []) if r["target"] == target]
        value = median([r["ms"] for r in rows if "ms" in r])
        buckets[str(target)] = {
            "pass": None if value is None else value < baseline,
            "requests": len(rows),
            "median_ms": None if value is None else round(value, 1),
            "qwen_median_ms": baseline,
        }
    gate3 = {"pass": combine(b["pass"] for b in buckets.values()), "buckets": buckets}

    if cosines is None:
        gate4 = {"pass": None, "pending": "phase 3 reference vectors on pop"}
    else:
        gate4 = {
            "pass": cosines["min"] >= COSINE_MIN,
            "min": cosines["min"],
            "median": cosines.get("median"),
            "threshold": COSINE_MIN,
        }

    measured = combine([gate1["pass"], gate2["pass"], gate3["pass"]])
    if measured is False or gate4["pass"] is False:
        verdict = "FAIL"
    elif measured is None:
        verdict = "INCOMPLETE"  # nothing failed, but gates 1-3 are not all measured
    elif gate4["pass"] is None:
        verdict = "PENDING"  # gates 1-3 pass; gate 4 waits for phase 3
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


def plan(sample, kept, queries=None):
    if queries is not None:
        # --queries windows: warm-up, both fragment kinds per length, each query.
        return {
            "image": IMAGE,
            "pod": POD,
            "args": PROBE_ARGS,
            "cap_eg2_tokens": CAP,
            "mode": "queries",
            "fragment_lengths": list(SHORT_LENGTHS),
            "fragment_token_source_index": sample.index(
                max(sample, key=lambda s: s["tokens"])
            ),
            "queries": len(queries),
            "requests": 1 + 2 * len(SHORT_LENGTHS) + len(queries),
            "gates": {"cosine_min": COSINE_MIN},
            "guards": {
                "rss_kib": RSS_LIMIT_KIB,
                "cpu_c": 80,
                "gpu_c": 85,
                "deadline_minutes": DEADLINE_MINUTES,
                "restore_reserve_seconds": RESTORE_RESERVE_S,
            },
        }
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
    """The fixture and the sha256 of the exact bytes parsed, from one read.

    A rehearsal certifies this digest, so a file rewritten after the parse (Codex
    round 3 on homelab#233) cannot be certified in place of what was measured.
    """
    raw = path.read_bytes()
    sample = json.loads(raw)
    counts = Counter(s["target"] for s in sample)
    if len(sample) != 44 or counts != {300: 12, 1000: 10, 3000: 10, 6000: 6, 7600: 6}:
        raise ValueError(f"wrong sample fixture: {dict(counts)}")
    return sample, sha256(raw)


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


STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT)


def run_window(out, snapshot, budget_s, start_pod, body, abort, note=None):
    """Scale production to 0, start the probe, run body() in a worker, restore.

    The signal handlers only record a reason and set `abort`; nothing raises into
    the main thread, so no signal can unwind past the restore. Every cluster write
    (scale-down, probe create, delete, restore) runs in the main thread, so the
    cleanup never races a create still in flight. The main thread stops waiting at
    the measurement budget; the restore then deletes the probe Pod, which fails
    any request the worker still has in flight. One absolute deadline,
    DEADLINE_MINUTES after scale-down, bounds the retries of every step.
    """
    reasons = []

    def stop(signum, frame):
        reasons.append(f"interrupted: signal {signum}")
        abort.set()

    # Every catchable terminating signal a shell or terminal sends: a closed
    # terminal (HUP) or Ctrl-\ (QUIT) after scale-down must not skip the restore.
    previous = {s: signal.signal(s, stop) for s in STOP_SIGNALS}
    failures = []

    def work():
        try:
            body()
        except BaseException as exc:  # noqa: BLE001 — re-raised after the restore
            failures.append(exc)

    worker = threading.Thread(target=work, daemon=True)
    window = {
        "deadline_minutes": DEADLINE_MINUTES,
        "measure_budget_seconds": budget_s,
        **(note or {}),
    }
    started = time.monotonic()
    measure_until = started + budget_s
    deadline = started + DEADLINE_MINUTES * 60
    issued, created = [], []

    def remaining():
        """Seconds left to measure; at zero, record the deadline and stop."""
        left = measure_until - time.monotonic()
        if left <= 0 and not abort.is_set():
            reasons.append("deadline reached")
            abort.set()
        return left

    try:
        window["scale_down_at"] = now()
        save(out / "window.json", window)
        scale_checked(snapshot, 0, measure_until, issued)
        if not issued:
            # Someone else scaled it to 0 after preflight: not this runner's outage.
            raise ProductionChanged(f"{DEPLOY} was already at 0 replicas")
        left = remaining()
        if left > 0:
            wait = int(min(180, left)) or 1
            kube(
                "wait",
                "--for=delete",
                "pod",
                "-l",
                "app=" + DEPLOY,
                f"--timeout={wait}s",
                timeout=wait + 10,
            )
        left = remaining()
        if left > 0 and not abort.is_set():
            created.append(now())
            # Bounded by the cutoff, so a slow create can't eat the restore reserve.
            start_pod(max(1, min(60, int(left))))
        if remaining() > 0 and not abort.is_set():
            worker.start()
        while worker.is_alive() and remaining() > 0 and not abort.is_set():
            worker.join(1)
    except BaseException as exc:  # noqa: BLE001 — re-raised after the restore
        failures.append(exc)
    finally:
        abort.set()
        restore_ended = None

        def delete_probe():
            # Runs even when the restore fails: a probe left on the GPU (a create
            # the API accepted late, a delete that never landed) blocks every later
            # restore attempt.
            try:
                kube(
                    "delete",
                    "pod",
                    POD,
                    "--ignore-not-found",
                    "--wait=false",
                    check=False,
                )
            except subprocess.SubprocessError as exc:
                window["final_probe_delete_error"] = str(exc)

        try:
            if issued:
                try:
                    cleanup_restore(snapshot, out, pod=POD, deadline=deadline)
                    window["restored_at"] = now()
                finally:
                    restore_ended = time.monotonic()
                    delete_probe()
            else:
                # Refused before any scale write: production was never touched.
                window["production_untouched"] = True
                if created:
                    delete_probe()
        finally:
            # Scale-down to restored (or to the failed restore).
            elapsed = (restore_ended or time.monotonic()) - started
            window["outage_seconds"] = round(elapsed)
            window["overran_deadline"] = elapsed > DEADLINE_MINUTES * 60
            if worker.is_alive():
                worker.join(30)
            window["finished_at"] = now()
            stopped = reasons + [f"{type(e).__name__}: {e}" for e in failures]
            if stopped:
                window["stopped_by"] = stopped
            save(out / "window.json", window)
            for s, handler in previous.items():
                signal.signal(s, handler)
    if failures:
        raise failures[0]
    if reasons:
        raise RuntimeError(reasons[0])


def capture_startup_log(records, out, attempts=3):
    """Gate 1's evidence. A failed capture leaves the gate unmeasured, never failed.

    `kubectl logs` prints the container's stdout and stderr on its own stdout;
    its own stderr carries API errors, which must never stand in for the log.
    """
    for _ in range(attempts):
        try:
            result = kube("logs", POD, "-c", "llamacpp", check=False)
        except subprocess.SubprocessError as exc:
            records["startup_log_error"] = str(exc)
        else:
            if result.returncode == 0 and result.stdout.strip():
                records["startup_log"] = result.stdout
                records.pop("startup_log_error", None)
                (out / "startup.log").write_text(result.stdout)
                return
            records["startup_log_error"] = (
                result.stderr.strip() or f"empty log, exit {result.returncode}"
            )[:500]
        time.sleep(2)
    records["startup_log"] = None


def tokenize(base, text, add_special=True):
    return http(
        base,
        "/tokenize",
        {"content": text, "add_special": add_special, "parse_special": True},
    )["tokens"]


def measure(
    base, monitor, abort, texts, pair_source, scratch, records, flush, queries=None
):
    rng = random.Random(SEED)
    vectors = (scratch / "vectors.jsonl").open("w")
    lock = threading.Lock()

    def embed(key, value, sent=None, special_free=False, prefix_only=False):
        if abort.is_set():
            raise RuntimeError("window stopped")
        monitor.check()
        start = time.monotonic()
        if sent is not None:
            sent.set()
        response = http(
            base,
            "/v1/embeddings",
            {"model": "probe", "input": [value], "cache_prompt": False},
            timeout=300,
        )
        end = time.monotonic()
        vector = response["data"][0]["embedding"]
        norm = check_vector(vector)
        tokens = (response.get("usage") or {}).get("prompt_tokens")
        with lock:
            # What --rehearse audits: each request's shape, kept even when over the cap.
            records.setdefault("requests", []).append(
                {
                    "key": key,
                    "tokens": tokens,
                    "array": isinstance(value, list),
                    "head": value[0] if isinstance(value, list) and value else None,
                    "tail": value[-1] if isinstance(value, list) and value else None,
                    "special_free": special_free,
                    "prefix_only": prefix_only,
                }
            )
        if tokens is None or tokens > CAP:
            raise RuntimeError(f"{key}: {tokens} prompt tokens, cap {CAP}")
        row = {"key": key, "input": value, "tokens": tokens, "vector": vector}
        with lock:
            vectors.write(json.dumps(row) + "\n")
            vectors.flush()
        return {
            "key": key,
            "ms": round((end - start) * 1000, 1),
            "start": start,
            "end": end,
            "tokens": tokens,
            "norm": norm,
        }

    def pair(first_key, first, second_key, second):
        """latency_local.pair(), keeping both vectors: `second` goes GAP_S after `first`."""
        out, sent = {}, threading.Event()

        def run_first():
            try:
                out["first"] = embed(first_key, first, sent)
            except Exception as exc:  # noqa: BLE001 — recorded as the run's error
                out["first"] = {"error": str(exc)}
                sent.set()

        thread = threading.Thread(target=run_first)
        thread.start()
        if not sent.wait(timeout=10):
            raise RuntimeError("first request of an ordering pair was never dispatched")
        time.sleep(latency_local.GAP_S)
        try:
            out["second"] = embed(second_key, second)
        except Exception as exc:  # noqa: BLE001 — recorded as the run's error
            out["second"] = {"error": str(exc)}
        thread.join()
        return out["first"], out["second"]

    try:
        records["first"] = embed("first", "probe warm-up request")
        flush()

        if queries is not None:
            # Gate 4 on short inputs only (third window): the 16-token token-array
            # calibration scored 0.988 against pop while a 7-token text scored 0.9999.
            # Fragments with and without a BOS separate "no BOS" from "padding to 256".
            ids = tokenize(base, pair_source, add_special=False)
            if len(ids) < max(SHORT_LENGTHS):
                raise RuntimeError(f"fragment source has {len(ids)} tokens")
            bos = tokenize(base, "", add_special=True)
            records["bos"] = bos
            records["specials"] = bos
            for length in SHORT_LENGTHS:
                for kind, value in (
                    ("frag", ids[:length]),
                    ("bosfrag", bos + ids[:length]),
                ):
                    # `frag` is the deliberate special-free half of the pair.
                    row = embed(
                        f"{kind}-{length}",
                        value,
                        special_free=kind == "frag",
                        prefix_only=kind == "bosfrag",
                    )
                    if row["tokens"] != len(value):
                        raise RuntimeError(
                            f"{kind}-{length}: sent {len(value)}, server counted "
                            f"{row['tokens']}"
                        )
                    records["short"].append({"kind": kind, "length": length, **row})
                    flush()
            for query in queries:
                # Counted before sending, so no request over the cap reaches the server.
                counted = len(tokenize(base, query["text"]))
                if counted > CAP:
                    raise RuntimeError(f"query {query['id']}: {counted} tokens")
                row = embed(f"query-{query['id']}", query["text"])
                records["short"].append({"kind": "query", "id": query["id"], **row})
                flush()
            print(f"short inputs done at {now()}", flush=True)
            return

        # Token arrays are sent as-is (server-common.cpp tokenize_mixed adds no BOS),
        # so they carry the model's specials ([BOS, EOS]) as OV's inputs do. The
        # 2026-10-15 third window found a special-free array scoring 0.988 vs pop.
        specials = tokenize(base, "", add_special=True)
        records["specials"] = specials

        def wrap(ids):
            return specials[:1] + ids + specials[1:]

        ids = tokenize(base, pair_source, add_special=False)
        longest = max(n for pair in PAIRS for n in pair)
        if len(ids) < longest + REPEATS:
            raise RuntimeError(f"pair source has {len(ids)} tokens, need {longest}")
        calibration = embed("calibrate", wrap(ids[:16]))
        offset = calibration["tokens"] - 16
        records["calibration"] = {**calibration, "offset": offset}
        if not 0 <= offset <= 2:
            raise RuntimeError(f"token-array offset {offset}")
        for repeat in range(REPEATS):
            for a, b in PAIRS:
                for length in (a, b) if repeat % 2 == 0 else (b, a):
                    row = embed(
                        f"pair-{length}-r{repeat}",
                        wrap(ids[repeat : repeat + length - offset]),
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
        records["ordering_inputs"] = {
            "short_index": short["index"],
            "long_index": long["index"],
            "long_eg2_tokens": long["eg2_tokens"],
        }
        for repeat in range(REPEATS):
            fresh = {}
            for name, item in (
                ("l1", long),
                ("s1", short),
                ("s2", short),
                ("l2", long),
            ):
                fresh[name] = latency_local.fresh(item["text"], rng)
                if len(tokenize(base, fresh[name])) > CAP:
                    raise RuntimeError(f"ordering text {item['index']} over the cap")
            l1, s1 = pair(
                f"order-long-first-r{repeat}",
                fresh["l1"],
                f"order-short-behind-r{repeat}",
                fresh["s1"],
            )
            s2, l2 = pair(
                f"order-short-first-r{repeat}",
                fresh["s2"],
                f"order-long-second-r{repeat}",
                fresh["l2"],
            )
            errors = [r["error"] for r in (l1, s1, s2, l2) if "error" in r]
            if not errors:
                if s1["start"] <= l1["start"] or l2["start"] <= s2["start"]:
                    errors.append("ordering reversed")
                if s1["start"] >= l1["end"]:
                    errors.append(
                        "short sent after long finished; no queueing measured"
                    )
            records["ordering"].append(
                {
                    "repeat": repeat,
                    "short_behind_long_ms": s1.get("ms"),
                    "long_ms_when_first": l1.get("ms"),
                    "short_first_ms": s2.get("ms"),
                    "long_ms_when_second": l2.get("ms"),
                    "errors": errors,
                }
            )
            flush()
            if abort.is_set():
                raise RuntimeError("window stopped")
    finally:
        vectors.close()


def run_plan(
    base, monitor, abort, sample, kept_by_qwen, queries, scratch, records, flush
):
    """The measurement body of --execute, against any llama-server at `base`.

    --rehearse calls this with a local server, so the requests it audits are the
    ones a window sends.
    """
    count = lambda text: len(tokenize(base, text))
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
    measure(
        base,
        monitor,
        abort,
        texts,
        pair_source,
        scratch,
        records,
        flush,
        queries=queries,
    )


class RehearsalRefused(ValueError):
    pass


# The runner and latency_local as imported: a rehearsal certifies the code that
# ran, not whatever is on disk when it finishes.
CODE_SHA256 = [sha256(Path(f).read_bytes()) for f in (__file__, latency_local.__file__)]


def plan_hash(args, sample, kept):
    """What a rehearsal certifies: the sample, probe args, cap, mode and request plan.

    The input digests are the ones taken when main() parsed the files, and the
    code digests are taken at import: nothing is re-read after a run.
    """
    return hashlib.sha256(
        json.dumps(
            {
                "plan": plan(sample, kept, args.query_list),
                "sample_sha256": args.sample_sha256,
                "queries_sha256": args.queries_sha256,
                # What shapes the requests but is not in plan(): a changed value
                # here can empty a bucket or alter an input without moving the plan.
                "settings": {
                    "model_env": MODEL_ENV,
                    "build": BUILD,
                    "seed": SEED,
                    "prefix_margin": PREFIX_MARGIN,
                    "gap_s": latency_local.GAP_S,
                    "targets": TARGETS,
                    "short_lengths": SHORT_LENGTHS,
                },
                "code_sha256": CODE_SHA256,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()


def require_rehearsal(args, sample, kept):
    """Refuse --execute without a passing rehearsal of this plan; returns window.json's note."""
    if args.skip_rehearsal:
        return {"rehearsal": "skipped (--skip-rehearsal)"}
    if not args.rehearsal:
        raise RehearsalRefused("--execute needs --rehearsal PATH (or --skip-rehearsal)")
    try:
        record = json.loads(args.rehearsal.read_text())
    except (OSError, ValueError) as exc:
        raise RehearsalRefused(f"unreadable rehearsal {args.rehearsal}: {exc}") from exc
    if record.get("passed") is not True:
        failed = [c["name"] for c in record.get("checks", []) if not c.get("ok")]
        raise RehearsalRefused(f"rehearsal {args.rehearsal} did not pass: {failed}")
    expected = plan_hash(args, sample, kept)
    if record.get("plan_hash") != expected:
        raise RehearsalRefused(
            f"rehearsal plan hash {record.get('plan_hash')} != current {expected}"
        )
    return {"rehearsal": str(args.rehearsal), "rehearsal_plan_hash": expected}


def rehearsal_checks(records, queries_mode, failure=None):
    """Each setup defect a window cannot afford, as {name, ok, detail}.

    Gate 4 is not judged: the reference is the local server. Gates 2 and 3 need
    only be measured, since a local card's speed says nothing about manu's.
    """
    verdict = evaluate(records)
    gate1 = verdict["gate1_attention_on_gpu"]
    unmeasured = [
        name
        for name in (
            ("gate1_attention_on_gpu",)
            if queries_mode
            else (
                "gate1_attention_on_gpu",
                "gate2_no_length_cliff",
                "gate3_faster_than_qwen",
            )
        )
        if verdict[name]["pass"] is None
    ]
    verbosity = int(PROBE_ARGS[PROBE_ARGS.index("--verbosity") + 1])
    requests = records.get("requests", [])
    over = [r["key"] for r in requests if r["tokens"] is None or r["tokens"] > CAP]
    specials = records.get("specials") or []

    def unwrapped(r):
        if r["special_free"]:
            return False
        if r["head"] != specials[0]:
            return True
        # The --queries `bosfrag` inputs deliberately carry the leading BOS only.
        return len(specials) > 1 and not r["prefix_only"] and r["tail"] != specials[-1]

    bare = [r["key"] for r in requests if r["array"] and specials and unwrapped(r)]
    ordering = records.get("ordering", [])
    ordering_bad = (
        []
        if queries_mode
        else [f"r{o['repeat']}: {o['errors']}" for o in ordering if o["errors"]]
        + (
            [f"{len(ordering)} of {REPEATS} repeats ran"]
            if len(ordering) < REPEATS
            else []
        )
    )
    counts = Counter(r["target"] for r in records.get("buckets", []))
    empty = [] if queries_mode else [t for t in QWEN_MEDIANS_MS if not counts[t]]
    seen = gate1["enabled_line"] or bool(gate1["fallback_lines"])
    checks = [
        ("plan_ran_to_the_end", failure is None, failure or "no request failed"),
        (
            "gates_1_3_measured" if not queries_mode else "gate_1_measured",
            not unmeasured,
            f"unmeasured: {unmeasured}" if unmeasured else "all measured",
        ),
        (
            "fa_signal_in_server_log",
            seen,
            "FA line found"
            if seen
            else f"no {FA_ENABLED!r} or fallback line in the server log",
        ),
        (
            "probe_verbosity_logs_fa",
            verbosity >= FA_LOG_VERBOSITY,
            f"--verbosity {verbosity}, the FA line logs at {FA_LOG_VERBOSITY}",
        ),
        (
            "ordering_ran_clean",
            not ordering_bad,
            f"ordering: {ordering_bad}" if ordering_bad else "no ordering errors",
        ),
        (
            "every_bucket_has_requests",
            not empty,
            f"empty buckets: {empty}" if empty else "none empty",
        ),
        (
            "no_request_over_cap",
            bool(requests) and not over,
            f"over {CAP}: {over}" if over else f"{len(requests)} requests",
        ),
        (
            "model_specials_resolved",
            bool(specials),
            f"/tokenize of '' with specials: {specials}",
        ),
        (
            "token_arrays_carry_specials",
            bool(specials) and not bare,
            f"special-free arrays not marked in the plan: {bare}"
            if bare
            else "all arrays wrapped, or marked special-free",
        ),
    ]
    return [{"name": n, "ok": bool(ok), "detail": d} for n, ok, d in checks]


def rehearse(args, sample, kept):
    """Run the --execute plan against a local server and write rehearsal.json.

    Returns True when every check passes. No kubectl, no port-forward.
    """
    out = args.output
    scratch = Path(__file__).parent / "scratch" / out.name
    out.mkdir(parents=True, exist_ok=False)
    scratch.mkdir(parents=True, exist_ok=False)
    certified = plan_hash(args, sample, kept)
    records = {"pairs": [], "buckets": [], "ordering": [], "short": [], "guard": None}
    records["startup_log"] = args.server_log.read_text() or None
    monitor = SimpleNamespace(check=lambda: None)
    failure = None
    try:
        run_plan(
            args.base_url,
            monitor,
            threading.Event(),
            sample,
            kept,
            args.query_list,
            scratch,
            records,
            lambda: save(out / "records.json", records),
        )
    except Exception as exc:  # noqa: BLE001 — reported as a failed check
        failure = f"{type(exc).__name__}: {exc}"
    save(out / "records.json", records)
    checks = rehearsal_checks(records, args.query_list is not None, failure)
    passed = all(c["ok"] for c in checks)
    save(
        out / "rehearsal.json",
        {
            "created_at": now(),
            "passed": passed,
            "plan_hash": certified,
            "mode": "queries" if args.query_list is not None else "full",
            "base_url": args.base_url,
            "server_log": str(args.server_log),
            "checks": checks,
        },
    )
    for c in checks:
        print(f"{'ok  ' if c['ok'] else 'FAIL'} {c['name']}: {c['detail']}")
    print(f"rehearsal {'PASSED' if passed else 'FAILED'}: {out / 'rehearsal.json'}")
    return passed


def execute(args, sample, kept_by_qwen):
    note = require_rehearsal(args, sample, kept_by_qwen)
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
            "sample_sha256": args.sample_sha256,
            "runner_sha256": CODE_SHA256[0],
            "plan": plan(sample, kept_by_qwen, args.query_list),
            **note,
            "vectors": str(scratch),
        },
    )
    snapshot = preflight()
    save(out / "production-before.json", snapshot)
    records = {"pairs": [], "buckets": [], "ordering": [], "short": [], "guard": None}
    abort = threading.Event()

    def flush():
        save(out / "records.json", records)

    def body():
        forward = monitor = None
        try:
            kube(
                "wait",
                "--for=condition=Ready",
                "pod/" + POD,
                "--timeout=420s",
                timeout=430,
            )
            capture_startup_log(records, out)
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
                raise RuntimeError(f"probe runs {image_id}, not b{BUILD}")
            flush()
            gate1 = evaluate(records)["gate1_attention_on_gpu"]
            print(f"gate 1 (startup log): {json.dumps(gate1)}", flush=True)
            # Not restore()'s 18085: the restore can start while this one still runs.
            forward = Forward("pod/" + POD, out / "port-forward.log", port=18086)
            forward.ready()
            save(out / "props.json", http(forward.base, "/props"))
            monitor = Monitor(
                out / "telemetry.jsonl",
                forward.base,
                pod=POD,
                rss_limit_kib=RSS_LIMIT_KIB,
            )
            monitor.start()
            run_plan(
                forward.base,
                monitor,
                abort,
                sample,
                kept_by_qwen,
                args.query_list,
                scratch,
                records,
                flush,
            )
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
        run_window(
            out,
            snapshot,
            budget,
            lambda timeout: kube("create", "-f", str(pod_path), timeout=timeout),
            body,
            abort,
            note,
        )
    finally:
        verdict = evaluate(records)
        save(out / "verdict.json", verdict)
        log("VERDICT " + json.dumps(verdict, indent=2))


def prepull():
    if kube("get", "pod", PREPULL, "--ignore-not-found").stdout.strip():
        raise RuntimeError("pre-pull Pod already exists")
    try:
        # Inside the cleanup boundary: kubectl can time out after the API accepted it.
        kube("create", "-f", "-", data=json.dumps(prepull_manifest()))
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
        # e.g. "version: 0.6.0-dev (build 11610, commit …)"
        build = re.search(r"\(build (\d+),", text)
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
    mode.add_argument("--rehearse", action="store_true")
    ap.add_argument("--sample", type=Path)
    ap.add_argument("--output", type=Path)
    ap.add_argument(
        "--cosines", type=Path, help='JSON {"min": x, "median": y} for gate 4'
    )
    ap.add_argument(
        "--queries",
        type=Path,
        help="JSON {queries: [{id, text}]}: embed these and SHORT_LENGTHS token "
        "fragments instead of the pairs, buckets and ordering (gate 4 only)",
    )
    ap.add_argument(
        "--base-url",
        default=REHEARSAL_URL,
        help="--rehearse: local llama-server started with PROBE_ARGS",
    )
    ap.add_argument("--server-log", type=Path, help="--rehearse: that server's log")
    ap.add_argument(
        "--rehearsal",
        type=Path,
        help="--execute: a passing rehearsal.json for this plan",
    )
    ap.add_argument(
        "--skip-rehearsal",
        action="store_true",
        help="--execute without a rehearsal; recorded in window.json",
    )
    args = ap.parse_args()
    # Loaded before any cluster call, so a bad file fails first.
    args.query_list, args.queries_sha256 = (
        load_queries_digested(args.queries) if args.queries else (None, sha256(b""))
    )

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
    sample, args.sample_sha256 = load_sample(args.sample)
    kept = select_inputs(sample)
    if args.rehearse:
        if not (args.output and args.server_log):
            ap.error("--rehearse needs --output and --server-log")
        sys.exit(0 if rehearse(args, sample, kept) else 1)
    if args.execute:
        if not args.output:
            ap.error("--execute needs --output")
        try:
            execute(args, sample, kept)
        except RehearsalRefused as exc:
            ap.error(str(exc))
        return
    print(
        json.dumps(
            {"pod": manifest(), "plan": plan(sample, kept, args.query_list)}, indent=2
        )
    )
    print("dry run: no cluster call made", flush=True)


if __name__ == "__main__":
    main()
