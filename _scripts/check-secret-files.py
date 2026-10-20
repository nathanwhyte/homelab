#!/usr/bin/env python3
"""Pre-commit guard: keep secret files and Secret values out of this public repo.

Fails closed. A staged file fails when it
  1. is named like a secret, or sits in a directory named like one, and is
     not a `.example` placeholder or code, i.e. it was force-added;
  2. is YAML/JSON that is not plain UTF-8, so it cannot be checked;
  3. is YAML/JSON that does not parse (duplicate keys included) and mentions
     `Secret`; or
  4. holds a Kubernetes Secret, anywhere in the document (a list dump, a
     last-applied-configuration annotation, nested under any key), with a
     data or stringData value other than "", REPLACE_ME, CHANGE_ME or
     <UPPER_SNAKE> (plain, or base64 in data).

There are no exemptions for config-looking values: write non-secret settings
(endpoints, regions) as placeholders too, and give their values in a comment.
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
SECRET_DIR = re.compile(r"secret|credential", re.I)
ALLOWED_NAME = re.compile(r"\.example$|\.(py|sh)$", re.I)
CHECKED_SUFFIXES = (".yaml", ".yml", ".json", ".example")
PLACEHOLDER = re.compile(r"^(?:|REPLACE_ME|CHANGE_ME|<[A-Z][A-Z0-9_]*>)$")
MENTIONS_SECRET = re.compile(r"\bSecret\b")
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


def placeholder(value, encoded: bool) -> bool:
    text = str(value)
    if PLACEHOLDER.match(text.strip()):
        return True
    if encoded:
        try:
            text = base64.b64decode(re.sub(r"\s", "", text), validate=True).decode()
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return False
    return bool(PLACEHOLDER.match(text.strip()))


def secret_findings(node, where: str, seen: dict[int, object]):
    # `seen` keeps each visited object alive, so its id() is never reused.
    if not isinstance(node, (list, dict)) or id(node) in seen:
        return
    seen[id(node)] = node
    if isinstance(node, list):
        for i, item in enumerate(node):
            yield from secret_findings(item, f"{where}[{i}]", seen)
        return
    metadata = node.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    annotations = metadata.get("annotations")
    if isinstance(annotations, dict) and LAST_APPLIED in annotations:
        try:
            applied = json.loads(str(annotations[LAST_APPLIED]), object_pairs_hook=_no_duplicates)
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
                if not placeholder(value, encoded):
                    yield (
                        f"Secret {name} {field}.{key} is not a placeholder ({where})"
                    )
    for key, value in node.items():
        if not (is_secret and key in ("data", "stringData")):
            yield from secret_findings(value, f"{where}.{key}", seen)


def check(path: Path) -> list[str]:
    problems = []
    named = SECRET_NAME.search(path.name) or any(
        SECRET_DIR.search(part) for part in path.parts[:-1]
    )
    if named and not ALLOWED_NAME.search(path.name):
        problems.append("named like a secret; commit a `.example` placeholder instead")
    if path.suffix.lower() not in CHECKED_SUFFIXES or not path.is_file():
        return problems
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = None
    if text is None or "\x00" in text:
        return problems + ["YAML/JSON that is not plain UTF-8, so it cannot be checked"]
    seen: dict[int, object] = {}
    try:
        for i, doc in enumerate(yaml.load_all(text, Loader=Loader)):
            problems.extend(secret_findings(doc, f"doc {i}", seen))
    except yaml.YAMLError:
        if MENTIONS_SECRET.search(text):
            problems.append(
                "mentions Secret but does not parse (duplicate keys count), so it cannot be checked"
            )
    return problems


def main(paths: list[str]) -> int:
    failed = 0
    for p in paths:
        for problem in check(Path(p)):
            print(f"{p}: {problem}")
            failed = 1
    if failed:
        print(
            "Use REPLACE_ME, CHANGE_ME or <UPPER_SNAKE> for every Secret value, give real config in a"
            " comment, and keep the real file gitignored."
        )
    return failed


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
