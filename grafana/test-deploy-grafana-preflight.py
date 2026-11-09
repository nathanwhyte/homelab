#!/usr/bin/env python3
"""Run with uv run python grafana/test-deploy-grafana-preflight.py [script].

deploy-grafana.sh must stop before its first cluster change when any file it
uses is missing (BUG-1293). The file list comes from the script itself: every
"$GRAFANA_DIR/<path>" it names. Each run copies grafana/ and longhorn/ to a
temporary directory, deletes one file, stubs kubectl, helm, python3 and the
sub-scripts with loggers, and checks that the script exited non-zero with
nothing logged but reads.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_scripts" / "lib"))
from git_env import scrub_environ  # noqa: E402

scrub_environ()

import os
import re
import shutil
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
script = Path(sys.argv[1]) if len(sys.argv) > 1 else root / "grafana/deploy-grafana.sh"
text = script.read_text()
# A match ending in "/" is a directory prefix such as "manifests/$name"; skip it.
files = sorted(
    {
        f
        for f in re.findall(r"\$GRAFANA_DIR/([A-Za-z0-9_./-]+)", text)
        if not f.endswith("/")
    }
)
listed = re.search(r"REQUIRED_FILES=\((.*?)\)", text, re.S)
subscripts = [f for f in files if f.endswith(".sh")]
# Reads and local-only steps; anything else logged is a cluster change.
reads = ("kubectl cluster-info", "kubectl get", "helm repo")
stub = '#!/bin/sh\necho "$(basename "$0") $*" >> "$TEST_LOG"\nexit 0\n'

failures = []
if not files:
    failures.append("no $GRAFANA_DIR/ paths found in the script")
if listed is None:
    failures.append("no REQUIRED_FILES list in the script")
else:
    unlisted = sorted(set(files) - set(listed.group(1).split()))
    if unlisted:
        failures.append(f"used but not in REQUIRED_FILES: {unlisted}")

for missing in files:
    with tempfile.TemporaryDirectory(prefix="deploy-grafana-preflight-") as directory:
        directory = Path(directory)
        shutil.copytree(root / "grafana", directory / "grafana")
        shutil.copytree(root / "longhorn", directory / "longhorn")
        grafana = directory / "grafana"
        for sub in subscripts:
            (grafana / sub).resolve().write_text(stub)
        (grafana / missing).resolve().unlink()
        binary = directory / "bin"
        binary.mkdir()
        for tool in ("kubectl", "helm", "python3"):
            (binary / tool).write_text(stub)
            (binary / tool).chmod(0o755)
        log = directory / "commands.log"
        log.touch()
        result = subprocess.run(
            ["/bin/bash", str(script)],
            env=dict(
                os.environ,
                PATH=f"{binary}{os.pathsep}{os.environ['PATH']}",
                TEST_LOG=str(log),
                GRAFANA_DIR=str(grafana),
            ),
            capture_output=True,
            text=True,
        )
        changes = [c for c in log.read_text().splitlines() if not c.startswith(reads)]
        if result.returncode == 0 or changes:
            failures.append(
                f"missing {missing}: exit {result.returncode}, cluster changes {changes}"
            )

for failure in failures:
    print(f"FAIL {failure}")
print(f"{len(files)} files checked, {len(failures)} failures")
sys.exit(1 if failures else 0)
