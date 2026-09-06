#!/usr/bin/env python3
"""Run rule syntax and behavior checks without a cluster or TSDB access.

Usage: uv run --with pyyaml python longhorn/tests/check-alerts.py --promtool /path/to/promtool
"""

import argparse
from pathlib import Path
import subprocess
import tempfile

import yaml


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--promtool", default="promtool")
    args = parser.parse_args()
    source = Path(__file__).resolve().parent
    manifest = yaml.safe_load((source.parent / "alerts.yaml").read_text())
    with tempfile.TemporaryDirectory(prefix="longhorn-alert-tests-") as directory:
        directory = Path(directory)
        rules = directory / "longhorn-rules.yaml"
        rules.write_text(yaml.safe_dump(manifest["spec"]))
        tests = directory / "alerts.test.yaml"
        tests.write_text((source / "alerts.test.yaml").read_text())
        subprocess.run([args.promtool, "check", "rules", str(rules)], check=True)
        subprocess.run([args.promtool, "test", "rules", str(tests)], check=True)


if __name__ == "__main__":
    main()
