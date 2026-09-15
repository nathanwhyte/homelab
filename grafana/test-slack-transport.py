#!/usr/bin/env python3
"""Run check-slack-transport.sh against its fixtures and assert each exit code.

The checker exists to catch the BUG-1135 class: a receiver whose transport makes
its `channel` field a lie. A check that has never been observed failing is not
evidence, so every fixture here declares the exit code the checker must produce.

Usage: uv run --no-project --with pyyaml python3 grafana/test-slack-transport.py
"""

from pathlib import Path
import subprocess
import sys
import tempfile

import yaml

ROOT = Path(__file__).resolve().parent
CHECKER = ROOT / "check-slack-transport.sh"
FIXTURES = ROOT / "check-slack-transport.test.yaml"


def load_documents():
    """Yield (config_document, expected_exit) pairs.

    Each YAML document in the fixture file is preceded by a comment line
    `# expected: <pass|fail>`; pyyaml drops comments, so walk the raw text to
    pair each marker with the document that follows it.
    """
    raw = FIXTURES.read_text()
    expectations = []
    for line in raw.splitlines():
        if line.startswith("# expected:"):
            expectations.append(line.split(":", 1)[1].strip())
    documents = list(yaml.safe_load_all(raw))
    if len(expectations) != len(documents):
        raise SystemExit(
            f"fixture mismatch: {len(expectations)} '# expected:' markers "
            f"but {len(documents)} documents"
        )
    return list(zip(documents, expectations))


def main():
    if not CHECKER.exists():
        raise SystemExit(f"checker not found: {CHECKER}")

    failures = []
    documents = load_documents()
    with tempfile.TemporaryDirectory(prefix="slack-transport-tests-") as directory:
        directory = Path(directory)
        for index, (document, expected) in enumerate(documents):
            path = directory / f"fixture-{index}.yaml"
            path.write_text(yaml.safe_dump(document))
            expected_code = 0 if expected == "pass" else 1
            result = subprocess.run(
                [str(CHECKER), "--config-file", str(path), "--verbose"],
                capture_output=True,
                text=True,
            )
            status = "ok" if result.returncode == expected_code else "MISMATCH"
            if result.returncode != expected_code:
                failures.append((index, expected, result.returncode, result.stdout))
            print(
                f"  {status:<9} fixture-{index} expected={expected} "
                f"exit={result.returncode}"
            )
            if result.returncode != expected_code:
                print(result.stdout)

    print()
    if failures:
        print(
            f"check-slack-transport fixtures: FAIL — {len(failures)} of {len(documents)} mismatched"
        )
        return 1
    print(
        f"check-slack-transport fixtures: PASS — {len(documents)} documents, every exit code as declared"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
