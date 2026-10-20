#!/usr/bin/env python3
"""Pre-commit guard: keep secret files and Secret values out of this public repo.

Fails closed. A staged file fails when it
  1. is named like a secret, or sits in a directory named like one, and is
     not a `.example` placeholder or code, i.e. it was force-added;
  2. is YAML/JSON (.yaml/.yml/.json, or one of those plus .example) that is
     not plain UTF-8, so it cannot be checked;
  3. is YAML/JSON that does not load (duplicate keys and bad tags included);
     or
  4. holds a Kubernetes Secret, anywhere in the document (a list dump, a
     SecretList item, a last-applied-configuration annotation, nested under
     any key), with a data or stringData value other than "", REPLACE_ME,
     CHANGE_ME or <UPPER_SNAKE> (written as is, even in data, or base64 of
     REPLACE_ME/CHANGE_ME).

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
CHECKED = re.compile(r"\.(?:ya?ml|json)(?:\.example)?$", re.I)
PLACEHOLDER = re.compile(r"^(?:|REPLACE_ME|CHANGE_ME|<[A-Z][A-Z0-9_]*>)$")
# gitleaks flags base64 of <UPPER_SNAKE>, so data takes only these two encoded.
ENCODED_PLACEHOLDER = re.compile(r"^(?:REPLACE_ME|CHANGE_ME)$")
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
        return bool(ENCODED_PLACEHOLDER.match(text.strip()))
    return False


def secret_findings(node, where: str, seen: dict[int, object], kind=None):
    # `seen` keeps each visited object alive, so its id() is never reused.
    # `kind` is the item kind a typed list (SecretList) implies for its items.
    if not isinstance(node, (list, dict)) or id(node) in seen:
        return
    seen[id(node)] = node
    if isinstance(node, list):
        for i, item in enumerate(node):
            yield from secret_findings(item, f"{where}[{i}]", seen, kind)
        return
    metadata = node.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    annotations = metadata.get("annotations")
    if isinstance(annotations, dict) and LAST_APPLIED in annotations:
        try:
            applied = json.loads(
                str(annotations[LAST_APPLIED]), object_pairs_hook=_no_duplicates
            )
        except ValueError:
            yield f"unparseable last-applied annotation ({where}); cannot check it"
        else:
            yield from secret_findings(applied, f"{where}.last-applied", seen)
    own_kind = node.get("kind")
    is_secret = own_kind == "Secret" or (own_kind is None and kind == "Secret")
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
    item_kind = (
        own_kind[: -len("List")]
        if isinstance(own_kind, str) and own_kind.endswith("List")
        else None
    )
    for key, value in node.items():
        if not (is_secret and key in ("data", "stringData")):
            yield from secret_findings(
                value, f"{where}.{key}", seen, item_kind if key == "items" else None
            )


def check(path: Path) -> list[str]:
    problems = []
    if any(SECRET_DIR.search(part) for part in path.parts[:-1]):
        problems.append(
            "inside a directory named like a secret, which stays wholly ignored"
        )
    elif SECRET_NAME.search(path.name) and not ALLOWED_NAME.search(path.name):
        problems.append("named like a secret; commit a `.example` placeholder instead")
    if not CHECKED.search(path.name) or not path.is_file():
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
    except Exception as e:  # noqa: BLE001 - any load failure fails closed
        # The type only: a constructor error's message can quote the value.
        problems.append(f"does not load ({type(e).__name__}), so it cannot be checked")
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
