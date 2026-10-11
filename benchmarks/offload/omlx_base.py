"""Create a fresh oMLX base folder for benchmark runs and print the serve command.

oMLX keeps its server settings, per-model settings, logs and caches under
`--base-path`. A benchmark needs only two things there: admin API access without
an API key (the harness drives settings, load and unload through `/admin/api`), and
an empty `--model-dir`, so oMLX discovers models from the Hugging Face cache alone.
oMLX fills every other setting with its defaults on first start and keeps the auth
flag. Per-model offload settings are not written here: `ds_probe.py`, `ds_bench.py
--extra` and `run_shadow.py`'s `omlx:` form set them through the admin API on every
load.

`skip_api_key_verification` is honoured only on a loopback bind; keep `--host` at its
default 127.0.0.1.

Usage: python3 omlx_base.py <dir> [--port 8000] [--memory-guard aggressive]
"""

import argparse
import json
import shlex
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "dir", type=Path, help="folder to create (base/ and models/ go inside)"
    )
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument(
        "--memory-guard",
        default="aggressive",
        help="oMLX --memory-guard tier, or a number for --memory-guard-gb",
    )
    a = ap.parse_args()
    root = a.dir.expanduser().resolve()
    base, models = root / "base", root / "models"
    base.mkdir(parents=True, exist_ok=True)
    models.mkdir(exist_ok=True)
    settings_path = base / "settings.json"
    settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    settings.setdefault("auth", {})["skip_api_key_verification"] = True
    settings_path.write_text(json.dumps(settings, indent=2) + "\n")
    guard = (
        ["--memory-guard-gb", a.memory_guard]
        if a.memory_guard.replace(".", "", 1).isdigit()
        else ["--memory-guard", a.memory_guard]
    )
    cmd = [
        "omlx",
        "serve",
        "--base-path",
        str(base),
        "--model-dir",
        str(models),
        "--port",
        str(a.port),
        *guard,
        "--no-cache",
    ]
    print(shlex.join(cmd))


if __name__ == "__main__":
    main()
