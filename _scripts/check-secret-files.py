#!/usr/bin/env python3
"""Pre-commit guard: keep secret files and Secret values out of this public repo.

Fails when a staged file
  1. is named like a secret, or sits in a directory named like one, and is
     not a `.example` placeholder or code, i.e. it was force-added; or
  2. is YAML/JSON holding a Kubernetes Secret, anywhere in the document
     (a list dump, a last-applied-configuration annotation, nested under any
     key), whose data or stringData carries a value that is not a placeholder;
     or mentions `kind: Secret` but cannot be parsed, so cannot be checked.

Secret values are denied by default: a key passes only when its whole value
is a placeholder, or its name is plain connection config (endpoint, region,
...) holding a config-shaped value. Never prints a value.
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
SECRET_DIR = re.compile(r"secret|credential", re.I)
ALLOWED_NAME = re.compile(r"\.example$|\.(py|sh)$", re.I)
# A placeholder is the whole value, never a substring of one.
PLACEHOLDER = re.compile(
    r"^(?:|(?:REPLACE|CHANGE)_?(?:ME|WITH)(?:_[A-Z0-9_]+)?|[A-Z][A-Z0-9_]*_PLACEHOLDER"
    r"|<[^<>\n]+>|\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|(?i:x{3,}|\*{3,}|redacted|your[-_][\w-]+))$"
)
# A connection URL whose only credential is its password placeholder.
URL_PASSWORD = re.compile(r"^[a-z][\w+.-]*://[^:/@\s]+:([^@\s]+)@[^@\s]+$", re.I)
# Connection config: the key's last `_` segment, or the whole key for a user
# name. The key must not also name a secret, and the value must look like config.
CONFIG_KEY = re.compile(
    r"(?:^|_)(?:type|provider|region|endpoints?|bucket|host|port|scheme)$|^(?:user|username)$",
    re.I,
)
SECRET_WORD = re.compile(
    r"pass|secret|token|key|auth|cred|cert|private|salt|session|sign", re.I
)
CONFIG_VALUE = re.compile(r"^(?:https?://[^\s@/]+(?:/[^\s@]*)?|[A-Za-z0-9][\w.:-]{0,62})$")
SECRET_KIND = re.compile(r"""["']?kind["']?\s*:\s*["']?Secret\b""")
LAST_APPLIED = "kubectl.kubernetes.io/last-applied-configuration"


class Loader(yaml.SafeLoader):
    """SafeLoader that reads unknown tags (`!Ref`, `!!python/...`) as plain data."""


def _untagged(loader, _suffix, node):
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_scalar(node)


Loader.add_multi_constructor("", _untagged)


def decode(value: str) -> str:
    try:
        return base64.b64decode(value, validate=True).decode()
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return value


def allowed(key: str, text: str) -> bool:
    if PLACEHOLDER.match(text.strip()):
        return True
    url = URL_PASSWORD.match(text.strip())
    if url and PLACEHOLDER.match(url.group(1)):
        return True
    return bool(
        CONFIG_KEY.search(key)
        and not SECRET_WORD.search(key)
        and CONFIG_VALUE.match(text)
    )


def secret_findings(node, where: str):
    if isinstance(node, list):
        for i, item in enumerate(node):
            yield from secret_findings(item, f"{where}[{i}]")
        return
    if not isinstance(node, dict):
        return
    metadata = node.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    annotations = metadata.get("annotations")
    if isinstance(annotations, dict) and isinstance(annotations.get(LAST_APPLIED), str):
        try:
            yield from secret_findings(
                json.loads(annotations[LAST_APPLIED]), f"{where}.last-applied"
            )
        except json.JSONDecodeError:
            yield f"unparseable last-applied annotation ({where}); cannot check it"
    is_secret = node.get("kind") == "Secret"
    if is_secret:
        name = metadata.get("name", "?")
        for field, encoded in (("data", True), ("stringData", False)):
            values = node.get(field)
            if values in (None, {}):
                continue
            if not isinstance(values, dict):
                yield f"Secret {name} {field} is not a mapping ({where}); cannot check it"
                continue
            for key, value in values.items():
                text = decode(str(value)) if encoded else str(value)
                if not allowed(str(key), text):
                    yield f"Secret {name} {field}.{key} holds a real value ({where})"
    for key, value in node.items():
        if not (is_secret and key in ("data", "stringData")):
            yield from secret_findings(value, f"{where}.{key}")


def check(path: Path) -> list[str]:
    problems = []
    named = SECRET_NAME.search(path.name) or any(
        SECRET_DIR.search(part) for part in path.parts[:-1]
    )
    if named and not ALLOWED_NAME.search(path.name):
        problems.append("named like a secret; commit a `.example` placeholder instead")
    if path.suffix not in (".yaml", ".yml", ".json", ".example") or not path.is_file():
        return problems
    try:
        text = path.read_text()
    except UnicodeDecodeError:
        return problems
    try:
        for i, doc in enumerate(yaml.load_all(text, Loader=Loader)):
            problems.extend(secret_findings(doc, f"doc {i}"))
    except yaml.YAMLError:
        if SECRET_KIND.search(text):
            problems.append("mentions `kind: Secret` but does not parse, so it cannot be checked")
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
