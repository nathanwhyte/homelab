#!/usr/bin/env python3
"""Reject Harbor configs that bypass local safety policy."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REQUIRED_INSTRUCTION = "instructions/bug-1068-readiness.md"
REQUIRED_POLICY_LANGUAGE = (
    "foreground shell command",
    "existence",
    "content",
    "visible shell prompt",
    "Inspect",
    "`task_complete`",
)


def uncommented(line: str) -> str:
    return line.split("#", 1)[0].rstrip()


def top_level_list(text: str, key: str) -> list[str] | None:
    lines = text.splitlines()
    header = re.compile(rf"^{re.escape(key)}:\s*(.*)$")
    for index, raw_line in enumerate(lines):
        line = uncommented(raw_line)
        match = header.match(line)
        if not match:
            continue
        inline = match.group(1).strip()
        if inline:
            if inline.startswith("[") and inline.endswith("]"):
                return [item.strip().strip("'\"") for item in inline[1:-1].split(",")]
            return []
        values: list[str] = []
        for following in lines[index + 1 :]:
            clean = uncommented(following)
            if not clean.strip():
                continue
            if not clean.startswith((" ", "\t")):
                break
            item = re.match(r"^\s+-\s+(.+?)\s*$", clean)
            if item:
                values.append(item.group(1).strip("'\""))
        return values
    return None


def check_config(path: Path, root: Path) -> list[str]:
    text = path.read_text()
    errors: list[str] = []
    instructions = top_level_list(text, "extra_instruction_paths")
    if instructions is None or REQUIRED_INSTRUCTION not in instructions:
        errors.append(f"missing extra_instruction_paths entry {REQUIRED_INSTRUCTION!r}")

    concurrency = [
        int(match.group(1))
        for raw_line in text.splitlines()
        if (
            match := re.match(
                r"^\s*n_concurrent_trials:\s*(\d+)\b", uncommented(raw_line)
            )
        )
    ]
    if concurrency != [1]:
        errors.append(
            "n_concurrent_trials must be 1 (one local-model consumer); "
            f"found {concurrency or 'no value'}"
        )

    terminus_agents = len(
        re.findall(r"^\s+-\s+name:\s+terminus-2\s*$", text, re.MULTILINE)
    )
    recording_disabled = len(
        re.findall(
            r"^\s+record_terminal_session:\s+false(?:\s+#.*)?$",
            text,
            re.MULTILINE,
        )
    )
    if recording_disabled < terminus_agents:
        errors.append(
            "every terminus-2 agent must set record_terminal_session: false; "
            f"found {recording_disabled} settings for {terminus_agents} agents"
        )

    return errors


def check_instruction(root: Path) -> list[str]:
    instruction_path = root / REQUIRED_INSTRUCTION
    if not instruction_path.is_file():
        return [f"required instruction file does not exist: {instruction_path}"]
    text = instruction_path.read_text()
    missing = [phrase for phrase in REQUIRED_POLICY_LANGUAGE if phrase not in text]
    if missing:
        return [
            "readiness instruction is missing required completion language: "
            + ", ".join(repr(phrase) for phrase in missing)
        ]
    return []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent,
        help="Harbor benchmark directory (default: script directory)",
    )
    args = parser.parse_args()
    root = args.root.resolve()
    configs = sorted(
        set((root / "configs").glob("*.yaml")) | set((root / "configs").glob("*.yml"))
    )
    if not configs:
        print(f"error: no YAML configs found under {root / 'configs'}", file=sys.stderr)
        return 1

    failures = 0
    for error in check_instruction(root):
        print(f"{root / REQUIRED_INSTRUCTION}: {error}", file=sys.stderr)
        failures += 1
    for config in configs:
        for error in check_config(config, root):
            print(f"{config}: {error}", file=sys.stderr)
            failures += 1
    if failures:
        return 1

    print(f"Harbor config policy passed: {len(configs)} config(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
