#!/usr/bin/env python3
"""Pre-commit guard: keep secret files and Secret values out of this public repo.

Fails closed. A staged file fails when it
  1. is named like a secret, or sits in a directory named like one, and is
     not a `.example` placeholder or code, i.e. it was force-added;
  2. is YAML/JSON (.yaml/.yml/.json, or one of those plus .example) that is
     not plain UTF-8, so it cannot be checked;
  3. is YAML/JSON that does not load, repeats a mapping key, uses a tag other
     than the plain core ones (no !!binary, !!omap, !!pairs, !!set or custom
     tags), walks past a fixed node budget, or has a document that mentions
     `Secret` and uses aliases or merge keys. These are checked on the
     composed nodes, before construction can merge, retag or share anything;
     or
  4. holds a Kubernetes Secret, anywhere in the document (a list dump, a
     SecretList item, a last-applied-configuration annotation, nested under
     any key), with a data or stringData value other than exactly "",
     REPLACE_ME, CHANGE_ME or <UPPER_SNAKE>, written as is (no base64, even
     in data: gitleaks flags encoded values).

There are no exemptions for config-looking values: write non-secret settings
(endpoints, regions) as placeholders too, and give their values in a comment.
Never prints a value.
"""

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
PLACEHOLDER = re.compile(r"(?:|REPLACE_ME|CHANGE_ME|<[A-Z][A-Z0-9_]*>)")
LAST_APPLIED = "kubectl.kubernetes.io/last-applied-configuration"
# Aliases make the walk revisit shared nodes; past this many, fail closed.
NODE_BUDGET = 1_000_000


MERGE = "tag:yaml.org,2002:merge"
PLAIN_TAGS = {
    f"tag:yaml.org,2002:{t}"
    for t in ("str", "int", "float", "bool", "null", "timestamp", "seq", "map")
} | {MERGE}


class TooManyNodes(Exception):
    pass


def node_problems(loader: yaml.SafeLoader, doc) -> list[str]:
    """Check a composed document before construction merges, retags or shares nodes."""
    seen, stack = set(), [doc]
    shared = merges = mentions_secret = False
    while stack:
        node = stack.pop()
        if id(node) in seen:
            shared = True
            continue
        seen.add(id(node))
        if node.tag not in PLAIN_TAGS:
            # The tag itself is not printed: it could be the value.
            return ["uses a YAML tag other than the plain core ones"]
        if isinstance(node, yaml.ScalarNode):
            mentions_secret = mentions_secret or "Secret" in node.value
        elif isinstance(node, yaml.SequenceNode):
            stack.extend(node.value)
        else:
            keys = set()
            for key_node, value_node in node.value:
                stack.extend((key_node, value_node))
                if key_node.tag == MERGE:
                    merges = True
                    continue
                if not isinstance(key_node, yaml.ScalarNode):
                    continue  # construction rejects unhashable keys
                key = loader.construct_object(key_node)
                if key in keys:
                    return ["repeats a mapping key"]
                keys.add(key)
    if mentions_secret and (shared or merges):
        return ["mentions Secret and uses YAML aliases or merge keys; write it out"]
    return []


def _no_duplicates(pairs):
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate key")
    return dict(pairs)


def placeholder(value) -> bool:
    return isinstance(value, str) and bool(PLACEHOLDER.fullmatch(value))


def label(key) -> str:
    # Keys are printed; a non-scalar key could hold a value.
    return str(key) if isinstance(key, (str, int, float, bool)) else "?"


def secret_findings(node, where: str, budget: list[int], ancestors=(), kind=None):
    # Shared (aliased) nodes are walked again on every path, since each path
    # can imply a different kind; `ancestors` only stops cycles.
    # `kind` is the item kind a typed list (SecretList) implies for its items.
    if not isinstance(node, (list, dict)) or id(node) in ancestors:
        return
    budget[0] -= 1
    if budget[0] < 0:
        raise TooManyNodes
    ancestors = (*ancestors, id(node))
    if isinstance(node, list):
        for i, item in enumerate(node):
            yield from secret_findings(item, f"{where}[{i}]", budget, ancestors, kind)
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
            yield from secret_findings(
                applied, f"{where}.last-applied", budget, ancestors
            )
    own_kind = node.get("kind")
    is_secret = own_kind == "Secret" or (own_kind is None and kind == "Secret")
    if is_secret:
        name = metadata.get("name", "?")
        name = name if isinstance(name, str) else "?"
        for field in ("data", "stringData"):
            values = node.get(field)
            if values in (None, {}):
                continue
            if not isinstance(values, dict):
                yield f"Secret {name} {field} is not a mapping ({where}); cannot check it"
                continue
            for key, value in values.items():
                if not placeholder(value):
                    yield (
                        f"Secret {name} {field}.{label(key)} is not a placeholder"
                        f" ({where})"
                    )
    item_kind = (
        own_kind[: -len("List")]
        if isinstance(own_kind, str) and own_kind.endswith("List")
        else None
    )
    for key, value in node.items():
        if not (is_secret and key in ("data", "stringData")):
            yield from secret_findings(
                value,
                f"{where}.{label(key)}",
                budget,
                ancestors,
                item_kind if key == "items" else None,
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
    budget = [NODE_BUDGET]
    loader = yaml.SafeLoader(text)
    try:
        i = 0
        while loader.check_node():
            node = loader.get_node()
            if found := node_problems(loader, node):
                return problems + [f"doc {i} {p}" for p in found]
            doc = loader.construct_document(node)
            problems.extend(secret_findings(doc, f"doc {i}", budget))
            i += 1
    except Exception as e:  # noqa: BLE001 - any load failure fails closed
        # The type only: a constructor error's message can quote the value.
        problems.append(f"does not load ({type(e).__name__}), so it cannot be checked")
    finally:
        loader.dispose()
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
