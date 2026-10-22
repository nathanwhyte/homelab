#!/usr/bin/env python3
"""IDEA-1090 pairing measurements, one GPU lane, Ollama 0.33.1, MAX_LOADED_MODELS=2.

1. unload the deepseek FIM runner
2. slot probe (n_seq_max) for each partner candidate
3. FIM side: qwen2.5-coder:fim TTFT probe + 8-case smoke, resident alone
4. cohabitation: FIM runner + partner runner, contention probe with the
   background load on the partner (BG_MODEL), VRAM snapshot per pair
5. restore MAX_LOADED_MODELS=1 + rollout (warm hook re-loads deepseek)
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

H = "http://192.168.1.19:11434"
FIM = "qwen2.5-coder:fim"
PARTNERS = ["qwen3.5:9b-q4_K_M", "gemma4:e4b-it-qat", "gemma4:12b-it-qat"]
OUT = Path(__file__).resolve().parent
TOOLS = OUT.parents[1] / "ollama" / "tools"


def api(path, body=None, timeout=600):
    req = urllib.request.Request(
        H + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def ps():
    return [
        (m["name"], round(m.get("size_vram", 0) / 2**30, 2), m.get("context_length"))
        for m in api("/api/ps")["models"]
    ]


def unload(model):
    try:
        api("/api/generate", {"model": model, "keep_alive": 0})
    except Exception as e:  # noqa: BLE001
        print("  unload", model, "->", e)


def load(model, keep="15m", chat=False):
    if chat:
        return api(
            "/api/chat",
            {
                "model": model,
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
                "think": False,
                "keep_alive": keep,
                "options": {"num_predict": 1, "num_ctx": 16384},
            },
        )
    return api(
        "/api/generate",
        {
            "model": model,
            "prompt": "x",
            "stream": False,
            "keep_alive": keep,
            "options": {"num_predict": 1, "num_ctx": 16384},
        },
    )


def pod_log(since="2m"):
    return subprocess.run(
        [
            "kubectl",
            "logs",
            "-n",
            "llama",
            "deploy/ollama",
            "-c",
            "ollama",
            f"--since={since}",
        ],
        capture_output=True,
        text=True,
    ).stdout


def sh(cmd, log_path, env=None):
    with open(log_path, "w") as f:
        r = subprocess.run(
            cmd,
            stdout=f,
            stderr=subprocess.STDOUT,
            text=True,
            env={**os.environ, **(env or {})},
        )
    return r.returncode


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main():
    log(f"ollama {api('/api/version')['version']}; ps={ps()}")
    for m, _, _ in ps():
        unload(m)
    time.sleep(3)
    log(f"after unload ps={ps()}")

    # 2. slot probes (skip with --skip-slots when resuming; slot-probe.json exists)
    slots = {}
    for p in [] if "--skip-slots" in sys.argv else PARTNERS:
        t0 = time.time()
        load(p, keep=0, chat=True)
        time.sleep(2)
        lines = [
            l
            for l in pod_log("3m").splitlines()
            if re.search(r"n_seq_max|new slot, n_ctx|n_ctx_seq\s+=", l)
        ]
        slots[p] = lines[-6:]
        log(
            f"slot probe {p} ({time.time() - t0:.0f}s): "
            + " | ".join(l.strip()[:60] for l in lines[-3:])
        )
        unload(p)
        time.sleep(2)
    if slots:
        (OUT / "slot-probe.json").write_text(json.dumps(slots, indent=1))

    # 3. FIM side alone
    load(FIM, keep=-1)
    time.sleep(2)
    log(f"FIM resident ps={ps()}")
    if "--skip-fim-side" not in sys.argv:
        rc = sh(
            [
                "bash",
                str(TOOLS / "fim-ttft-probe.sh"),
                f"timmy-fim3b={H}/v1/completions={FIM}",
            ],
            OUT / "fim-ttft-probe.log",
        )
        log(f"fim-ttft-probe rc={rc}")
        rc = sh(["python3", str(TOOLS / "fim-smoke.py"), FIM], OUT / "fim-smoke.log")
        log(
            f"fim-smoke rc={rc}: "
            + (OUT / "fim-smoke.log").read_text().strip().splitlines()[-1]
        )

    # 4. cohabitation per partner
    vram = {}
    for p in PARTNERS:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", p)
        load(p, keep="15m", chat=True)
        time.sleep(2)
        vram[p] = ps()
        log(f"pair {FIM} + {p}: ps={vram[p]}")
        env = {
            "FIM_MODEL": FIM,
            "BG_MODEL": p,
            "BG_NUM_CTX": "16384",  # pin: no reload at OLLAMA_CONTEXT_LENGTH
            "PROBE_JSON": str(OUT / f"cohab-{safe}.json"),
        }
        rc = sh(
            ["python3", str(TOOLS / "fim-contention-probe.py"), "8"],
            OUT / f"cohab-{safe}.log",
            env,
        )
        tail = [
            l
            for l in (OUT / f"cohab-{safe}.log").read_text().splitlines()
            if l[:1] in "ABCDE"
        ]
        log(f"cohab {p} rc={rc}\n    " + "\n    ".join(tail))
        unload(p)
        time.sleep(3)
    (OUT / "vram-pairs.json").write_text(json.dumps(vram, indent=1))

    # 5. restore
    unload(FIM)
    log("restoring MAX_LOADED_MODELS=1 and rolling the deployment")
    subprocess.run(
        [
            "kubectl",
            "set",
            "env",
            "deploy/ollama",
            "-n",
            "llama",
            "OLLAMA_MAX_LOADED_MODELS=1",
        ],
        check=True,
    )
    subprocess.run(
        [
            "kubectl",
            "rollout",
            "status",
            "deploy/ollama",
            "-n",
            "llama",
            "--timeout=420s",
        ],
        check=True,
    )
    for _ in range(30):
        time.sleep(4)
        if ps():
            break
    log(f"restored; ps={ps()}")


if __name__ == "__main__":
    sys.exit(main())
