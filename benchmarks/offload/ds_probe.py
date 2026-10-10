"""Probe which resident fractions oMLX admits for a model: load, read the offload wrap summary, unload.

Usage: python3 ds_probe.py --model <id> --log <omlx serve log> [--base URL] <fraction> [<fraction> ...]
"""

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.environ.get("OMLX_BASE") or "http://127.0.0.1:8000"


def http(method, path, body=None):
    req = urllib.request.Request(
        BASE + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=3600) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:400]


def main():
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--log", required=True, type=Path)
    ap.add_argument("--base", default=BASE, help="oMLX base URL ($OMLX_BASE)")
    ap.add_argument("fractions", nargs="+", type=float)
    a = ap.parse_args()
    BASE = a.base
    for f in a.fractions:
        http("POST", f"/admin/api/models/{a.model}/unload", {})
        time.sleep(5)
        settings = {
            "moe_expert_offload_enabled": True,
            "moe_expert_offload_resident_fraction": f,
        }
        st, r = http("PUT", f"/admin/api/models/{a.model}/settings", settings)
        if st != 200:
            print(json.dumps({"fraction": f, "settings_status": st, "detail": r}))
            continue
        off = a.log.stat().st_size
        t = time.monotonic()
        st, r = http("POST", f"/admin/api/models/{a.model}/load", {})
        with open(a.log, errors="replace") as fh:
            fh.seek(off)
            text = fh.read()
        wrap = re.findall(r"offload: wrapped .*", text)
        skips = re.findall(r"offload: skipping .*", text)
        _, models = http("GET", "/admin/api/models")
        row = next(
            (m for m in models.get("models", []) if m["id"] == a.model),
            {},
        )
        print(
            json.dumps(
                {
                    "fraction": f,
                    "status": st,
                    "load_s": round(time.monotonic() - t, 1),
                    "loaded": row.get("loaded"),
                    "actual_size": row.get("actual_size"),
                    "wrap": wrap[-1] if wrap else None,
                    "skips": skips[:3],
                    "detail": r if st != 200 else None,
                }
            ),
            flush=True,
        )
    http("POST", f"/admin/api/models/{a.model}/unload", {})


if __name__ == "__main__":
    main()
