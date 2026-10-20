#!/usr/bin/env python3
"""Pre-commit guard: keep secret files and Secret values out of this public repo.

Fails when a staged file
  1. is named like a secret, or sits in a directory named like one, and is
     not a `.example` placeholder or code, i.e. it was force-added; or
  2. is YAML/JSON holding a Kubernetes Secret, anywhere in the document
     (a list dump, a last-applied-configuration annotation, nested under any
     key), whose data or stringData carries a value that is not a placeholder;
     or mentions `kind: Secret` but cannot be parsed (duplicate keys included),
     so cannot be checked.

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
CHECKED_SUFFIXES = (".yaml", ".yml", ".json", ".example")
# A placeholder is the whole value, never a substring of one.
PLACEHOLDER = re.compile(
    r"^(?:|(?:REPLACE|CHANGE)_?(?:ME|WITH)(?:_[A-Z0-9_]+)?|[A-Z][A-Z0-9_]*_PLACEHOLDER"
    r"|<[^<>\n]+>|\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|(?i:x{3,}|\*{3,}|redacted|your[-_][\w-]+))$"
)
# A URL with no user info, query or fragment: nowhere to carry a credential.
# The host may hold `<NAME>` placeholders.
PLAIN_URL = r"https?://(?:[A-Za-z0-9.-]|<[A-Z_]+>)+(?::\d+)?(?:/[\w./-]*)?"
# A connection URL whose only credential is a placeholder password.
URL_PASSWORD = re.compile(
    r"^[a-z][a-z0-9+.-]*://[a-z_][a-z0-9_-]{0,31}:([^@\s/?#]+)@[A-Za-z0-9.-]+(?::\d+)?(?:/[\w.-]*)?$"
)
# Connection config: the key's last `_` segment, or the whole key for a user
# name. The key must not also name a secret, and the value must look like config.
CONFIG_KEY = re.compile(
    r"(?:^|_)(?:type|provider|region|endpoints?|bucket|host|port|scheme)$|^(?:user|username)$",
    re.I,
)
SECRET_WORD = re.compile(
    r"pass|secret|token|key|auth|cred|cert|private|salt|session|sign", re.I
)
CONFIG_VALUE = re.compile(rf"^(?:{PLAIN_URL}|[A-Za-z0-9][\w.:-]{{0,62}})$")
SECRET_KIND = re.compile(r"""["']?kind["']?\s*:\s*["']?Secret\b""")
# Go/Helm template actions. A whole-line action is dropped; an inline one becomes
# a placeholder, unless it carries a quoted literal (`default "x"`), which might
# be a real value and so stays checkable.
TEMPLATE_LINE = re.compile(r"^\s*\{\{-?[^\n]*?-?\}\}\s*$", re.M)
TEMPLATE_INLINE = re.compile(r"\{\{-?([^\n]*?)-?\}\}")
LAST_APPLIED = "kubectl.kubernetes.io/last-applied-configuration"


class Loader(yaml.SafeLoader):
    """SafeLoader that reads unknown tags as plain data and refuses duplicate keys."""

    def construct_mapping(self, node, deep=False):
        if isinstance(node, yaml.MappingNode):
            seen = set()
            for key_node, _ in node.value:
                if (
                    not isinstance(key_node, yaml.ScalarNode)
                    or key_node.tag == "tag:yaml.org,2002:merge"
                ):
                    continue
                key = (key_node.tag, key_node.value)
                if key in seen:
                    raise yaml.constructor.ConstructorError(
                        None, None, "duplicate mapping key", key_node.start_mark
                    )
                seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _untagged(loader, _suffix, node):
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_scalar(node)


Loader.add_multi_constructor("", _untagged)


def _no_duplicates(pairs):
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate key")
    return dict(pairs)


def decode(value: str) -> str:
    try:
        return base64.b64decode(value, validate=True).decode()
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return value


def allowed(key: str, text: str) -> bool:
    text = text.strip()
    if PLACEHOLDER.match(text):
        return True
    url = URL_PASSWORD.match(text)
    if url and PLACEHOLDER.match(url.group(1)):
        return True
    return bool(
        CONFIG_KEY.search(key)
        and not SECRET_WORD.search(key)
        and CONFIG_VALUE.match(text)
    )


def secret_findings(node, where: str, seen: set[int]):
    if not isinstance(node, (list, dict)) or id(node) in seen:
        return
    seen.add(id(node))
    if isinstance(node, list):
        for i, item in enumerate(node):
            yield from secret_findings(item, f"{where}[{i}]", seen)
        return
    metadata = node.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    annotations = metadata.get("annotations")
    if isinstance(annotations, dict) and isinstance(annotations.get(LAST_APPLIED), str):
        try:
            applied = json.loads(annotations[LAST_APPLIED], object_pairs_hook=_no_duplicates)
        except ValueError:
            yield f"unparseable last-applied annotation ({where}); cannot check it"
        else:
            yield from secret_findings(applied, f"{where}.last-applied", seen)
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
            yield from secret_findings(value, f"{where}.{key}", seen)


def untemplate(text: str) -> str:
    text = TEMPLATE_LINE.sub("", text)
    return TEMPLATE_INLINE.sub(
        lambda m: "TEMPLATE_LITERAL" if re.search(r"[\"'`]", m.group(1)) else "TEMPLATE_PLACEHOLDER",
        text,
    )


def parse_findings(text: str) -> list[str]:
    """Findings for every document; raises yaml.YAMLError if any document fails."""
    problems = []
    for i, doc in enumerate(yaml.load_all(text, Loader=Loader)):
        problems.extend(secret_findings(doc, f"doc {i}", set()))
    return problems


def read_text(path: Path) -> str | None:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-16", "utf-32"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def check(path: Path) -> list[str]:
    problems = []
    named = SECRET_NAME.search(path.name) or any(
        SECRET_DIR.search(part) for part in path.parts[:-1]
    )
    if named and not ALLOWED_NAME.search(path.name):
        problems.append("named like a secret; commit a `.example` placeholder instead")
    if path.suffix.lower() not in CHECKED_SUFFIXES or not path.is_file():
        return problems
    text = read_text(path)
    if text is None:
        problems.append("YAML/JSON in an unreadable encoding, so it cannot be checked")
        return problems
    found = []
    try:
        found = parse_findings(text)
    except yaml.YAMLError:
        try:
            found = parse_findings(untemplate(text))
        except yaml.YAMLError:
            if SECRET_KIND.search(text) or SECRET_KIND.search(path.read_bytes().decode("latin-1")):
                found = [
                    "mentions `kind: Secret` but does not parse (duplicate keys count), "
                    "so it cannot be checked"
                ]
    return problems + found


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
