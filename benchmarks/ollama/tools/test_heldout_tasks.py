"""Self-test the held-out fixtures against unfixed, correct, and wrong fixes."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from coding_tasks.heldout import HELDOUT_TASKS
from test_coding_tasks import run_verifier

REFERENCE_SOLUTIONS = {
    "h1a-label-spacing": {
        "labels.py": """\
def display_label(value):
    return " ".join(part.capitalize() for part in value.split())
"""
    },
    "h1b-relative-path": {
        "paths.py": """\
def normalize_relative_path(path):
    parts = []
    for part in path.replace("\\\\", "/").split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
        else:
            parts.append(part)
    return "/".join(parts)
"""
    },
    "h1c-duration-unit": {
        "durations.py": """\
def parse_duration(value):
    unit = value[-1]
    amount = int(value[:-1])
    scales = {"s": 1, "m": 60, "h": 3600}
    if unit not in scales:
        raise ValueError("unsupported duration unit")
    return amount * scales[unit]
"""
    },
    "h2a-tenant-key-collision": {
        "cache_keys.py": """\
def cache_key(tenant, name):
    return (tenant.lower(), name.lower())
"""
    },
    "h2b-settings-parser": {
        "settings.py": """\
from settings_parser import parse_line


def decode_settings(raw):
    result = {}
    for line in raw.splitlines():
        if line.strip():
            key, value = parse_line(line)
            result[key] = value
    return result
""",
        "settings_parser.py": """\
def parse_line(line):
    if "=" not in line:
        raise ValueError("setting must contain =")
    key, value = line.split("=", 1)
    return key.strip(), value.strip()
""",
    },
    "h2c-field-regression-test": {
        "fields.py": """\
def decode_field(value):
    return value
""",
        "test_fields_regression.py": """\
import unittest

from fields import decode_field


class DecodeFieldRegressionTest(unittest.TestCase):
    def test_backslash_n_stays_literal(self):
        self.assertEqual(decode_field(r"line1\\nline2"), r"line1\\nline2")


if __name__ == "__main__":
    unittest.main()
""",
    },
    "h3a-ledger-snapshots": {
        "ledger.py": """\
class Ledger:
    def __init__(self):
        self._entries = []

    def append(self, event):
        self._entries.append(event)

    def entries(self, since=0, limit=None):
        if since < 0 or (limit is not None and limit < 0):
            raise ValueError("since and limit must be non-negative")
        result = list(self._entries[since:])
        return result if limit is None else result[:limit]
"""
    },
    "h3b-env-filtering": {
        "env_settings.py": """\
def collect_settings(environment, prefix="APP_", allowlist=None, transform=None):
    result = {}
    allowed = None if allowlist is None else {name.lower() for name in allowlist}
    for key, value in environment.items():
        if not key.startswith(prefix):
            continue
        suffix = key[len(prefix):].lower()
        if allowed is not None and suffix not in allowed:
            continue
        result[suffix] = transform(value) if transform is not None else value
    return result
"""
    },
    "h3c-stable-task-planner": {
        "planner.py": """\
def plan(items, reverse=False, limit=None):
    ordered = sorted(items, key=lambda item: item["priority"])
    if reverse:
        groups = []
        for item in ordered:
            if not groups or groups[-1][0] != item["priority"]:
                groups.append((item["priority"], []))
            groups[-1][1].append(item)
        ordered = [item for _, group in reversed(groups) for item in group]
    if limit is not None:
        ordered = ordered[:limit]
    return ordered
"""
    },
}

WRONG_SOLUTIONS = {
    "h1a-label-spacing": [
        (
            "strip only outer whitespace",
            {
                "labels.py": 'def display_label(value):\n    return " ".join(part.capitalize() for part in value.strip().split(" "))\n'
            },
        )
    ],
    "h1b-relative-path": [
        (
            "discard all parent segments",
            {
                "paths.py": 'def normalize_relative_path(path):\n    return "/".join(p for p in path.replace("\\\\", "/").split("/") if p not in ("", ".", ".."))\n'
            },
        )
    ],
    "h1c-duration-unit": [
        (
            "treat unknown units as seconds",
            {
                "durations.py": 'def parse_duration(value):\n    return int(value[:-1]) * {"s": 1, "m": 60, "h": 3600}.get(value[-1], 1)\n'
            },
        )
    ],
    "h2a-tenant-key-collision": [
        (
            "preserve case-sensitive names",
            {
                "cache_keys.py": 'def cache_key(tenant, name):\n    return f"{tenant.lower()}:{name}"\n'
            },
        )
    ],
    "h2b-settings-parser": [
        (
            "silently skip malformed lines",
            {
                "settings.py": 'def decode_settings(raw):\n    out = {}\n    for line in raw.splitlines():\n        if "=" in line:\n            key, value = line.split("=", 1)\n            out[key.strip()] = value.strip()\n    return out\n'
            },
        )
    ],
    "h2c-field-regression-test": [
        (
            "unescape backslash-n",
            {
                "fields.py": 'def decode_field(value):\n    return value.replace(r"\\n", "\\n")\n'
            },
        )
    ],
    "h3a-ledger-snapshots": [
        (
            "return the internal list",
            {
                "ledger.py": "class Ledger:\n    def __init__(self): self._entries = []\n    def append(self, event): self._entries.append(event)\n    def entries(self, since=0, limit=None): return self._entries if since == 0 and limit is None else self._entries[since:since+limit]\n"
            },
        )
    ],
    "h3b-env-filtering": [
        (
            "ignore allowlist",
            {
                "env_settings.py": 'def collect_settings(environment, prefix="APP_", allowlist=None, transform=None):\n    return {key[len(prefix):].lower(): (transform(value) if transform else value) for key, value in environment.items() if key.startswith(prefix)}\n'
            },
        )
    ],
    "h3c-stable-task-planner": [
        (
            "reverse the sorted list",
            {
                "planner.py": 'def plan(items, reverse=False, limit=None):\n    result = sorted(items, key=lambda item: item["priority"])\n    if reverse: result.reverse()\n    return result if limit is None else result[:limit]\n'
            },
        )
    ],
}


def main() -> int:
    failures = 0
    for task in HELDOUT_TASKS:
        if (
            task.task_id not in REFERENCE_SOLUTIONS
            or task.task_id not in WRONG_SOLUTIONS
        ):
            print(f"[{task.task_id}] missing reference or hack")
            failures += 1
            continue
        code, out = run_verifier(task.files, task.verifier)
        if code == 0:
            print(f"[{task.task_id}] BROKEN: unfixed fixture passes")
            failures += 1
        else:
            print(f"[{task.task_id}] unfixed -> fails as expected")
        fixed = dict(task.files)
        fixed.update(REFERENCE_SOLUTIONS[task.task_id])
        code, out = run_verifier(fixed, task.verifier)
        if code:
            print(f"[{task.task_id}] reference -> FAIL {out[-300:]}")
            failures += 1
        else:
            print(f"[{task.task_id}] reference -> passes")
        for label, edits in WRONG_SOLUTIONS[task.task_id]:
            hacked = dict(task.files)
            hacked.update(edits)
            code, _ = run_verifier(hacked, task.verifier)
            if code == 0:
                print(f"[{task.task_id}] FALSE POSITIVE: {label}")
                failures += 1
            else:
                print(f"[{task.task_id}] wrong fix rejected ({label})")
    print(
        f"\n{len(HELDOUT_TASKS)} held-out tasks, {sum(map(len, WRONG_SOLUTIONS.values()))} adversarial fixes, {failures} problem(s)"
    )
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
