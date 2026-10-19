#!/usr/bin/env python3
"""Pre-commit guard: keep secret files and Secret values out of this public repo.

Fails when a staged file
  1. is named like a secret (the patterns .gitignore ignores) but is not a
     `.example` placeholder or code, i.e. it was force-added; or
  2. is YAML/JSON holding a Kubernetes Secret, anywhere in the document
     (a list dump, a last-applied-configuration annotation), whose data or
     stringData carries a value that is not a placeholder.

Secret values are denied by default: a key passes only when its value is a
placeholder or its name is plain connection config (endpoint, region, ...).
Never prints a value.
"""

import base64
import binascii
import json
import re
import sys
from pathlib import Path

import yaml

SECRET_NAME = re.compile(
    r"secret|credential|token.*\.txt$|\.(key|pem|crt|p12|pfx|env)$|^\.env(\..+)?$"
    r"|^htpasswd$|^rclone\.conf$|kubeconfig|^rotated-creds-.*\.txt$",
    re.I,
)
ALLOWED_NAME = re.compile(r"\.example$|\.(py|sh)$", re.I)
PLACEHOLDER = re.compile(
    r"(?i:^$|replace|change[_-]?me|<[^>]+>|\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|^x{3,}$|\*{3,}"
    r"|redacted|example|^your[-_])"
    # An all-caps name with no digits (ROOT_API_KEY) is a placeholder, not a key.
    r"|^[A-Z][A-Z_]+$"
)
CONFIG_KEY = re.compile(
    r"(^|_)(type|provider|region|endpoints?|bucket|no_check_bucket|host|port|scheme|path"
    r"|user|username)$",
    re.I,
)
LAST_APPLIED = "kubectl.kubernetes.io/last-applied-configuration"


def decode(value: str) -> str:
    try:
        return base64.b64decode(value, validate=True).decode()
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return value


def secret_findings(node, where: str):
    if isinstance(node, list):
        for i, item in enumerate(node):
            yield from secret_findings(item, f"{where}[{i}]")
        return
    if not isinstance(node, dict):
        return
    annotation = (node.get("metadata") or {}).get("annotations", {}) or {}
    if isinstance(annotation, dict) and isinstance(annotation.get(LAST_APPLIED), str):
        try:
            yield from secret_findings(
                json.loads(annotation[LAST_APPLIED]), f"{where}.last-applied"
            )
        except json.JSONDecodeError:
            pass
    if node.get("kind") == "Secret":
        name = (node.get("metadata") or {}).get("name", "?")
        for field, encoded in (("data", True), ("stringData", False)):
            for key, value in (node.get(field) or {}).items():
                text = decode(str(value)) if encoded else str(value)
                if CONFIG_KEY.search(key) or PLACEHOLDER.search(text):
                    continue
                yield f"Secret {name} {field}.{key} holds a real value ({where})"
    for key, value in node.items():
        if key not in ("data", "stringData"):
            yield from secret_findings(value, f"{where}.{key}")


def check(path: Path) -> list[str]:
    problems = []
    if SECRET_NAME.search(path.name) and not ALLOWED_NAME.search(path.name):
        problems.append("named like a secret; commit a `.example` placeholder instead")
    if path.suffix in (".yaml", ".yml", ".json", ".example") and path.is_file():
        try:
            docs = list(yaml.safe_load_all(path.read_text()))
        except (yaml.YAMLError, UnicodeDecodeError):
            docs = []
        for i, doc in enumerate(docs):
            problems.extend(secret_findings(doc, f"doc {i}"))
    return problems


def main(paths: list[str]) -> int:
    failed = 0
    for p in paths:
        for problem in check(Path(p)):
            print(f"{p}: {problem}")
            failed = 1
    if failed:
        print(
            "Replace values with placeholders (REPLACE_ME, <NAME>) and keep the real file gitignored."
        )
    return failed


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
