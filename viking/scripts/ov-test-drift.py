#!/usr/bin/env python3
"""Show how ov-test's live patch ConfigMap differs from this checkout's render.

IMPR-1456. A production apply of openviking-nav-patch leaves ov-test's own
ov-nav-patch behind, and check-nav-patch-files.py cannot see that, because it
compares repo files with rendered manifests, never with the cluster. Run this
before planning an ov-test canary: an apply ships every drifted file, not only
the patch under test.

Read-only: it runs `kubectl kustomize` and `kubectl get`, and prints line counts,
never ConfigMap contents. It renders the checkout it sits in, so run it from an
up-to-date main/ to compare with main, or pass --root. It compares the ConfigMap
only: a pod keeps the patches it loaded until it restarts.

Exit 0: no drift. Exit 1: drift. Exit 2: could not compare.
Usage: uv run --no-project --with pyyaml python viking/scripts/ov-test-drift.py
"""

import argparse
import difflib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

CONFIGMAP = "ov-nav-patch"
REQUEST_TIMEOUT = "15s"
EXIT_DRIFT = 1
EXIT_UNKNOWN = 2


def load_checker():
    """check-nav-patch-files.py, the one definition of how a stack is rendered."""
    path = Path(__file__).resolve().parent / "check-nav-patch-files.py"
    spec = importlib.util.spec_from_file_location("_check_nav_patch_files", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def rendered_data(checker, root):
    """The keys of the ConfigMap the test kustomization generates."""
    stack = next(s for s in checker.STACKS if s["configmap"] == CONFIGMAP)
    objects = checker.render(root, stack["dir"])
    found = checker.named(objects, "ConfigMap", CONFIGMAP, checker.NAMESPACE)
    if len(found) != 1:
        raise RuntimeError(f"render has {len(found)} ConfigMaps named {CONFIGMAP}")
    return stack["dir"], config_data(found[0])


def live_data(namespace):
    """The keys of the ConfigMap in the cluster."""
    args = [
        "kubectl",
        "-n",
        namespace,
        f"--request-timeout={REQUEST_TIMEOUT}",
        "get",
        "configmap",
        CONFIGMAP,
        "-o",
        "json",
    ]
    proc = subprocess.run(args, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"kubectl get: {proc.stderr.strip()[:300]}")
    return config_data(json.loads(proc.stdout))


def config_data(configmap):
    """Every key of a ConfigMap; a binaryData value compares as its base64 text."""
    return {**(configmap.get("binaryData") or {}), **(configmap.get("data") or {})}


def line_changes(live, rendered):
    """Lines an apply would add and remove, keeping line ends so that a change to
    one alone still counts."""
    old = live.splitlines(keepends=True)
    new = rendered.splitlines(keepends=True)
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    added = removed = 0
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if tag != "equal":
            removed += old_end - old_start
            added += new_end - new_start
    return added, removed


def compare(rendered, live):
    """One row per key that differs, in key order."""
    rows = []
    for key in sorted(set(rendered) | set(live)):
        if key not in live:
            rows.append(
                f"  {key}: missing from live (+{len(rendered[key].splitlines())})"
            )
        elif key not in rendered:
            rows.append(f"  {key}: only in live (-{len(live[key].splitlines())})")
        elif rendered[key] != live[key]:
            added, removed = line_changes(live[key], rendered[key])
            rows.append(f"  {key}: +{added} -{removed}")
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parent.parent.parent
    )
    args = parser.parse_args(argv)
    # An uncaught failure would exit 1, which here means drift.
    try:
        checker = load_checker()
        kdir, rendered = rendered_data(checker, args.root)
        live = live_data(checker.NAMESPACE)
    except (SystemExit, Exception) as exc:  # noqa: BLE001 — SystemExit is the
        # checker's missing-PyYAML message.
        print(f"ov-test-drift: could not compare: {exc}", file=sys.stderr)
        return EXIT_UNKNOWN
    rows = compare(rendered, live)
    source = args.root / kdir
    if not rows:
        print(f"live {CONFIGMAP} matches the render of {source} ({len(live)} keys)")
        return 0
    print(f"live {CONFIGMAP} differs from the render of {source}:")
    print("\n".join(rows))
    print("(+ lines an apply would add, - lines it would remove)")
    return EXIT_DRIFT


if __name__ == "__main__":
    sys.exit(main())
