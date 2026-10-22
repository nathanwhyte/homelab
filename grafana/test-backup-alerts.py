#!/usr/bin/env python3
"""Run with uv run --with pyyaml python grafana/test-backup-alerts.py [promtool]."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_scripts" / "lib"))
from git_env import scrub_environ  # noqa: E402

scrub_environ()

import subprocess
import tempfile

import yaml

root = Path(__file__).resolve().parent
with tempfile.TemporaryDirectory(prefix="backup-alert-tests-") as directory:
    directory = Path(directory)
    rules = yaml.safe_load((root / "manifests/backup-alerts.yaml").read_text())["spec"]
    tests = yaml.safe_load((root / "backup-alerts.test.yaml").read_text())
    tests["rule_files"] = [str(directory / "rules.yaml")]
    (directory / "rules.yaml").write_text(yaml.safe_dump(rules))
    (directory / "tests.yaml").write_text(yaml.safe_dump(tests))
    subprocess.run(
        [
            sys.argv[1] if len(sys.argv) > 1 else "promtool",
            "test",
            "rules",
            str(directory / "tests.yaml"),
        ],
        check=True,
    )
