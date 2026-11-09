#!/usr/bin/env python3
"""Run with uv run python grafana/test-deploy-grafana-preflight.py [script].

Two checks on deploy-grafana.sh (BUG-1293), each run against a temporary copy
of grafana/ and longhorn/ with kubectl, helm, python3 and the sub-scripts
replaced by loggers:

1. Order: the alloy-logs PodMonitor and alert are applied before the
   k8s-monitoring upgrade, and when that apply fails the upgrade never runs,
   so the longer retries cannot go live without their alert.
2. Preflight: every file the script names as "$GRAFANA_DIR/<path>" is in
   REQUIRED_FILES, and deleting any one of them stops the script before its
   first cluster change. Files that helm-deploy.py or the sub-scripts read
   are outside this check.
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
# Logs like stub, but fails the apply that carries the alloy-logs alert.
failing_kubectl = (
    '#!/bin/sh\necho "$(basename "$0") $*" >> "$TEST_LOG"\n'
    'case "$*" in *alloy-logs-alerts.yaml*) exit 1 ;; esac\nexit 0\n'
)


def run(missing=None, kubectl=stub):
    """Run the script on a stubbed copy; return (exit code, logged commands)."""
    with tempfile.TemporaryDirectory(prefix="deploy-grafana-preflight-") as directory:
        directory = Path(directory)
        shutil.copytree(root / "grafana", directory / "grafana")
        shutil.copytree(root / "longhorn", directory / "longhorn")
        grafana = directory / "grafana"
        for sub in subscripts:
            (grafana / sub).resolve().write_text(stub)
        if missing:
            (grafana / missing).resolve().unlink()
        binary = directory / "bin"
        binary.mkdir()
        for tool, body in (("kubectl", kubectl), ("helm", stub), ("python3", stub)):
            (binary / tool).write_text(body)
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
        return result.returncode, log.read_text().splitlines()


def first(commands, predicate):
    return next((i for i, c in enumerate(commands) if predicate(c)), None)


def is_alert_apply(command):
    return command.startswith("kubectl apply") and "alloy-logs-alerts.yaml" in command


def is_podmonitor_apply(command):
    return (
        command.startswith("kubectl apply") and "alloy-logs-podmonitor.yaml" in command
    )


def is_upgrade(command):
    return command.startswith("python3 ") and " k8s-monitoring " in command


failures = []

# 1. Order.
code, commands = run()
alert, podmonitor, upgrade = (
    first(commands, p) for p in (is_alert_apply, is_podmonitor_apply, is_upgrade)
)
if code != 0:
    failures.append(f"full stubbed run exited {code}")
if upgrade is None or alert is None or podmonitor is None:
    failures.append(
        f"full run: alert apply {alert}, PodMonitor apply {podmonitor}, upgrade {upgrade}"
    )
elif not (alert < upgrade and podmonitor < upgrade):
    failures.append(
        f"k8s-monitoring upgrade (command {upgrade}) runs before the alloy-logs "
        f"alert (command {alert}) or PodMonitor (command {podmonitor}) apply"
    )
code, commands = run(kubectl=failing_kubectl)
if code == 0 or first(commands, is_upgrade) is not None:
    failures.append(
        f"failed alloy-logs alert apply: exit {code}, upgrade still ran "
        f"{first(commands, is_upgrade) is not None}"
    )

# 2. Preflight.
if not files:
    failures.append("no $GRAFANA_DIR/ paths found in the script")
if listed is None:
    failures.append("no REQUIRED_FILES list in the script")
else:
    unlisted = sorted(set(files) - set(listed.group(1).split()))
    if unlisted:
        failures.append(f"used but not in REQUIRED_FILES: {unlisted}")
for missing in files:
    code, commands = run(missing=missing)
    changes = [c for c in commands if not c.startswith(reads)]
    if code == 0 or changes:
        failures.append(f"missing {missing}: exit {code}, cluster changes {changes}")

for failure in failures:
    print(f"FAIL {failure}")
print(f"order checked, {len(files)} files checked, {len(failures)} failures")
sys.exit(1 if failures else 0)
