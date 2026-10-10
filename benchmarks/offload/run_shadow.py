"""Run proposer-shadow.py over the batch-gate corpus for each configuration, one local model at a time.

Fails loudly instead of skipping: every admin call is status-checked, the applied
resident fraction is read back from the settings response, Ollama must have nothing
loaded before an oMLX configuration starts, each proposer-shadow run must exit 0,
and every expected lane must write a results file with the expected case count.
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

TRIAL = Path.home() / "code" / "moe-offload-trial"
VAULT = Path.home() / "code" / "compendium"
SHADOW = VAULT / "_scripts" / "batch" / "proposer-shadow.py"
RESULTS = Path.home() / "code" / "homelab" / "main" / "benchmarks" / "results"
OMLX = "http://127.0.0.1:8000"
OLLAMA = "http://127.0.0.1:11434"
PROXY = "http://127.0.0.1:11500"
MODEL = "Jundot--Qwen3.8-Flash-Next-oQ4e-mtp"
OMLX_LOG = Path(os.environ.get("OMLX_LOG", TRIAL / "omlx-serve-ds.log"))

# (case file, --only lane, expected case count); each lane runs on its own because
# `--only triage` also runs every other triage lane and crashes on a case file
# that lacks that lane's cases key.
LANES = [
    ("batch-skill-gate-20260828-postfix2", "summary", 20),
    ("fence-mistag-20260830", "fence", 15),
    ("triage-skills-20260830", "blocker", 23),
    ("triage-skills-20260830", "staleness", 12),
    ("compaction-triage-20260831", "compaction", 20),
]


def http(method, url, body=None, timeout=900):
    req = urllib.request.Request(
        url,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:500]


def die(msg):
    print(f"FAIL: {msg}", flush=True)
    sys.exit(1)


def ollama_loaded():
    try:
        st, r = http("GET", f"{OLLAMA}/api/ps", timeout=10)
    except urllib.error.URLError:
        return []  # Ollama not running: nothing can be loaded
    if st != 200:
        die(f"ollama /api/ps returned {st}")
    return [m["name"] for m in r["models"]]


def omlx_unload(model=MODEL):
    st, r = http("POST", f"{OMLX}/admin/api/models/{model}/unload", {})
    if st not in (200, 202, 404) and "Model not loaded" not in str(r):
        die(f"oMLX unload returned {st}: {r}")
    time.sleep(5)


def omlx_generic(label, model, fraction, extra):
    """Load `model` with expert offload at `fraction` plus `extra` settings, then run the lanes.

    The proxy on PROXY must already be forwarding to `model`. `extra` may carry
    `_expect_layers` (not sent to oMLX): the load must then log exactly that many
    wrapped MoE layers, with no skips, in OMLX_LOG.
    """
    if ollama_loaded():
        die(f"Ollama has models loaded: {ollama_loaded()}")
    extra = dict(extra)
    expect_layers = extra.pop("_expect_layers", None)
    omlx_unload(MODEL)
    omlx_unload(model)
    settings = {
        **extra,
        "moe_expert_offload_enabled": True,
        "moe_expert_offload_resident_fraction": fraction,
    }
    st, r = http("PUT", f"{OMLX}/admin/api/models/{model}/settings", settings)
    applied = (r.get("settings") or {}) if isinstance(r, dict) else {}
    mismatch = {
        k: (v, applied.get(k)) for k, v in settings.items() if applied.get(k) != v
    }
    if st != 200 or mismatch:
        die(f"oMLX settings PUT {st}, mismatch={mismatch}")
    log_offset = OMLX_LOG.stat().st_size
    st, r = http("POST", f"{OMLX}/admin/api/models/{model}/load", {})
    if st != 200:
        die(f"oMLX load returned {st}: {r}")
    if expect_layers is not None:
        with open(OMLX_LOG, errors="replace") as fh:
            fh.seek(log_offset)
            text = fh.read()
        counts = re.findall(r"moe expert offload: wrapped (\d+) layers", text)
        skips = re.findall(r"moe expert offload: skipping (.*)", text)
        if not counts or int(counts[-1]) != expect_layers or skips:
            omlx_unload(model)
            die(
                f"offload wrapped {counts} layers (expected {expect_layers}), skips={skips}"
            )
    print(f"oMLX loaded {model} at {fraction} with {extra}", flush=True)
    shadow(label, PROXY, model)
    omlx_unload(model)


def omlx_config(fraction):
    if ollama_loaded():
        die(f"Ollama has models loaded: {ollama_loaded()}")
    omlx_unload()
    settings = {
        "moe_expert_offload_enabled": True,
        "moe_expert_offload_resident_fraction": fraction,
        "qwen4_ple_ssd_offload": True,
        "mtp_enabled": True,
        "mtp_adaptive_max_depth": None,
        "qwen35_oq_a8_enabled": False,
    }
    st, r = http("PUT", f"{OMLX}/admin/api/models/{MODEL}/settings", settings)
    applied = (r.get("settings") or {}) if isinstance(r, dict) else {}
    if st != 200 or applied.get("moe_expert_offload_resident_fraction") != fraction:
        die(
            f"oMLX settings PUT {st}, applied={applied.get('moe_expert_offload_resident_fraction')}"
        )
    st, r = http("POST", f"{OMLX}/admin/api/models/{MODEL}/load", {})
    if st != 200:
        die(f"oMLX load returned {st}: {r}")
    print(f"oMLX loaded at {fraction}", flush=True)


def results_ok(path, n):
    if not path.exists():
        return False
    rows = json.loads(path.read_text())
    rows = rows if isinstance(rows, list) else rows.get("rows", rows.get("results", []))
    return len(rows) == n


def shadow(label, host, model):
    for case, lane, n in LANES:
        out = TRIAL / "shadow" / f"{label}-{case}"
        result = out / f"{lane}-results.json"
        if results_ok(result, n):
            print(f"== {label} {lane} already complete, skipping", flush=True)
            continue
        log = TRIAL / "shadow" / f"{label}-{case}-{lane}.log"
        out.parent.mkdir(parents=True, exist_ok=True)
        print(f"== {label} {lane} {time.strftime('%H:%M:%S')}", flush=True)
        cmd = [
            "uv",
            "run",
            "python",
            str(SHADOW),
            "--model",
            model,
            "--host",
            host,
            "--cases",
            str(RESULTS / case / "cases.json"),
            "--only",
            lane,
            "--out",
            str(out),
        ]
        with open(log, "w") as f:
            rc = subprocess.run(
                cmd, cwd=VAULT, stdout=f, stderr=subprocess.STDOUT, check=False
            ).returncode
        if rc != 0:
            die(f"proposer-shadow exited {rc}; see {log}")
        if not results_ok(result, n):
            die(f"{result} missing or not {n} rows")
        print(f"   ok {lane} ({n})", flush=True)


def run_ollama(label, tag):
    omlx_unload()
    loaded = [m for m in ollama_loaded() if m != tag]
    if loaded:
        die(f"Ollama has other models loaded: {loaded}")
    shadow(label, OLLAMA, tag)
    subprocess.run(["ollama", "stop", tag], check=False)


CONFIGS = {
    "omlx-60": lambda: (
        omlx_config(0.6),
        shadow("omlx-60", PROXY, "omlx-qwen38-flash-next"),
    ),
    "omlx-12": lambda: (
        omlx_config(0.125),
        shadow("omlx-12", PROXY, "omlx-qwen38-flash-next"),
    ),
    "ollama-qwen36-35b": lambda: run_ollama("ollama-qwen36-35b", "qwen3.6:35b-mlx"),
    # Production proposer on timmy, run as deployed: it is pinned (KEEP_ALIVE=-1)
    # next to the FIM model and serves OpenViking, so it is never stopped here and
    # no num_ctx is sent (a differing num_ctx would reload it).
    "timmy-gemma4-vlm": lambda: shadow(
        "timmy-gemma4-vlm", "http://100.95.215.105:11434", "gemma4:vlm"
    ),
    # qwen3.8:27b-mlx ships presence_penalty 0; the -pp15 tag sets 1.5 to match.
    "ollama-qwen38-27b": lambda: run_ollama(
        "ollama-qwen38-27b", "qwen3.8:27b-mlx-pp15"
    ),
}


def main():
    names = sys.argv[1:] or ["omlx-60", "omlx-12", "ollama-qwen36-35b"]
    for name in names:
        if name.startswith("omlx:"):
            # omlx:<label>:<model id>:<fraction>:<extra settings JSON>
            _, label, model, fraction, extra = name.split(":", 4)
            omlx_generic(label, model, float(fraction), json.loads(extra))
        elif name.startswith("proxy:"):
            # proxy:<label>:<model name>, for a server already running behind the
            # proxy on PROXY (e.g. llama-server via --upstream); nothing is loaded here.
            _, label, model = name.split(":", 2)
            if ollama_loaded():
                die(f"Ollama has models loaded: {ollama_loaded()}")
            shadow(label, PROXY, model)
        else:
            CONFIGS[name]()
    print(f"== done {time.strftime('%H:%M:%S')}", flush=True)


if __name__ == "__main__":
    main()
