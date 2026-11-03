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
only: a pod keeps the patches it loaded until it restarts. After the comparison
it prints the in-use mark ov-test-marker.py keeps, so a canary plan can see who
else is experimenting on the stack.

Exit 0: no drift. Exit 1: drift. Exit 2: could not compare. A closed stdout
or stderr loses the text and never changes the exit code.
Usage: uv run --no-project --with pyyaml python viking/scripts/ov-test-drift.py
"""

import argparse
import base64
import difflib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

CONFIGMAP = "ov-nav-patch"
REQUEST_TIMEOUT = "15s"
EXIT_DRIFT = 1
EXIT_UNKNOWN = 2


class CompareError(Exception):
    """A failed comparison, with a message free of ConfigMap contents."""


def load_checker():
    """check-nav-patch-files.py, the one definition of how a stack is rendered."""
    path = Path(__file__).resolve().parent / "check-nav-patch-files.py"
    spec = importlib.util.spec_from_file_location("_check_nav_patch_files", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_marker():
    """ov-test-marker.py, the one definition of the in-use mark."""
    path = Path(__file__).resolve().parent / "ov-test-marker.py"
    spec = importlib.util.spec_from_file_location("_ov_test_marker", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def marker_line():
    """Who has marked ov-test as in use, as one printable line. Nothing here
    may raise, since the exit code reports drift only."""
    try:
        marker = load_marker()
    except Exception as exc:  # noqa: BLE001 — a missing mark must not hide drift
        return f"in use: unknown ({type(exc).__name__})"
    try:
        mark, _ = marker.read_deployment()
        return marker.describe(mark)
    except Exception as exc:  # noqa: BLE001
        reason = str(exc)[:200] or type(exc).__name__
        try:
            return marker.printable(f"in use: unknown ({reason})")
        except Exception:  # noqa: BLE001
            return f"in use: unknown ({type(exc).__name__})"


def open_streams():
    """Give a descriptor closed before start (sys.stdout or sys.stderr None) a
    /dev/null stream, so that nothing, argparse included, falls back from a
    missing stderr to stdout."""
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
            except OSError:
                pass


def escaped(text, out):
    """Text that `out`'s encoding can carry, so that a path or key it cannot
    encode is escaped rather than taken for a lost stream."""
    encoding = getattr(out, "encoding", None) or "utf-8"
    try:
        return text.encode(encoding, "backslashreplace").decode(encoding)
    except LookupError:
        return text.encode("ascii", "backslashreplace").decode("ascii")


def emit(lines, stream="stdout"):
    """Print lines and flush them now. A closed or broken stream loses the text
    and nothing else; a lost report is noted on stderr when stderr works."""
    out = getattr(sys, stream, None)
    try:
        for line in lines:
            print(escaped(line, out), file=out)
        out.flush()
    except (OSError, ValueError, AttributeError):  # AttributeError: stream is None
        if stream == "stdout":
            emit(["ov-test-drift: stdout closed, report lost"], "stderr")


def finish(code):
    """Exit with `code` and nothing else. Both streams are flushed here, where
    a failure loses only their text, and os._exit skips the interpreter's
    flush at exit, the one place a stream could still turn the code into 120."""
    for name in ("stdout", "stderr"):
        try:
            getattr(sys, name).flush()  # None (AttributeError) is caught too
        except Exception:  # noqa: BLE001
            pass
    os._exit(code)


def rendered_data(checker, root):
    """The keys of the ConfigMap the test kustomization generates."""
    stack = next(s for s in checker.STACKS if s["configmap"] == CONFIGMAP)
    if shutil.which("kubectl") is None:
        raise CompareError("kubectl not found")
    try:
        objects = checker.render(root, stack["dir"])
    except Exception as exc:  # noqa: BLE001 — its text can quote manifest contents
        raise CompareError(
            f"rendering failed ({type(exc).__name__}); run kubectl kustomize on "
            f"{stack['dir']} to see why"
        ) from None
    found = checker.named(objects, "ConfigMap", CONFIGMAP, checker.NAMESPACE)
    if len(found) != 1:
        raise CompareError(f"render has {len(found)} ConfigMaps named {CONFIGMAP}")
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
        raise CompareError(f"kubectl get: {proc.stderr.strip()[:300]}")
    return config_data(json.loads(proc.stdout))


def section(configmap, name):
    """A ConfigMap section; only an absent or null one counts as empty."""
    value = configmap.get(name)
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(isinstance(k, str) for k in value):
        raise CompareError(f"{name} is not a map with string keys")
    return value


def config_data(configmap):
    """Every key of a ConfigMap, as the bytes a pod mounts. The one check of its
    shape: anything but a well-formed ConfigMap cannot be compared."""
    if not isinstance(configmap, dict) or configmap.get("kind") != "ConfigMap":
        raise CompareError("not a ConfigMap")
    data = section(configmap, "data")
    binary = section(configmap, "binaryData")
    both = sorted(set(data) & set(binary))
    if both:
        raise CompareError(f"in both data and binaryData: {', '.join(both)}")
    values = {}
    for key, value in data.items():
        if not isinstance(value, str):
            raise CompareError(f"data key {key} is not a string")
        values[key] = value.encode("utf-8")
    for key, value in binary.items():
        try:
            values[key] = base64.b64decode(value, validate=True)
        except (TypeError, ValueError):
            raise CompareError(f"binaryData key {key} is not base64") from None
    return values


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
        rows = compare(rendered, live)
    except (CompareError, SystemExit) as exc:  # SystemExit: no PyYAML
        emit([f"ov-test-drift: could not compare: {exc}"], "stderr")
        return EXIT_UNKNOWN
    except Exception as exc:  # noqa: BLE001 — the type alone, for the same reason
        emit([f"ov-test-drift: could not compare: {type(exc).__name__}"], "stderr")
        return EXIT_UNKNOWN
    source = args.root / kdir
    if not rows:
        report = [f"live {CONFIGMAP} matches the render of {source} ({len(live)} keys)"]
    else:
        report = [
            f"live {CONFIGMAP} differs from the render of {source}:",
            *rows,
            "(+ lines an apply would add, - lines it would remove)",
        ]
    emit([*report, marker_line()])
    return EXIT_DRIFT if rows else 0


if __name__ == "__main__":
    open_streams()
    try:
        exit_code = main()
    except SystemExit as exc:  # argparse: 0 for --help, 2 for a usage error
        exit_code = 0 if exc.code in (None, 0) else EXIT_UNKNOWN
    except Exception:  # noqa: BLE001 — main() maps failures itself; a backstop
        exit_code = EXIT_UNKNOWN
    finish(exit_code)
