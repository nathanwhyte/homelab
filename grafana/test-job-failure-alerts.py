#!/usr/bin/env python3
"""Run with uv run --with pyyaml python grafana/test-job-failure-alerts.py [promtool].

Extracts spec from manifests/job-failure-alerts.yaml, checks rule syntax, then runs
the behavior fixtures in job-failure-alerts.test.yaml.
"""

from pathlib import Path
import subprocess
import sys
import tempfile

import yaml

promtool = sys.argv[1] if len(sys.argv) > 1 else "promtool"
root = Path(__file__).resolve().parent
with tempfile.TemporaryDirectory(prefix="job-failure-alert-tests-") as directory:
    directory = Path(directory)
    rules = yaml.safe_load((root / "manifests/job-failure-alerts.yaml").read_text())["spec"]
    tests = yaml.safe_load((root / "job-failure-alerts.test.yaml").read_text())
    tests["rule_files"] = [str(directory / "rules.yaml")]
    (directory / "rules.yaml").write_text(yaml.safe_dump(rules))
    (directory / "tests.yaml").write_text(yaml.safe_dump(tests))
    subprocess.run([promtool, "check", "rules", str(directory / "rules.yaml")], check=True)
    subprocess.run([promtool, "test", "rules", str(directory / "tests.yaml")], check=True)
