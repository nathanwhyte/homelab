#!/usr/bin/env python3
"""Run the nav-patch suite (viking/manifests/test/nav-patch/test_*.py) in one process.

IMPR-1413. Each patch guards on ``openviking.__version__ == "v0.4.20"``, the string
the image reports; the PyPI wheel (the ``openviking`` uv tool) reports ``0.4.20``, so
the real-module cases failed on every local run and each review round had to diff
"the same failures as main" by hand. This runner maps the wheel's string to the
image's before any patch target is imported (``import openviking`` loads only
``openviking._version``), so the suite is green wherever the installed package is
the release the patches pin. Any other version is left alone and fails as it should.

It also runs ``test_ov_nav_patch.py``'s module-level ``test_*`` functions, which
``unittest discover`` does not collect.

Interpreter: ``$NAV_PATCH_PYTHON``, else the ``openviking`` uv tool's Python when it
exists, else this one. With no openviking importable (CI), the real-module cases
skip and a SKIP line says so; ``NAV_PATCH_REQUIRE_OPENVIKING=1`` makes that a failure.

Usage: viking/scripts/run-nav-patch-tests.py [-v]
"""

import ast
import importlib
import os
import sys
import unittest
from pathlib import Path

NAV_DIR = Path(__file__).resolve().parent.parent / "manifests" / "test" / "nav-patch"
CHILD_FLAG = "_NAV_PATCH_TESTS_CHILD"


def tool_python():
    """The ``openviking`` uv tool's interpreter, if it is installed."""
    base = os.environ.get("UV_TOOL_DIR") or os.path.join(
        os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"),
        "uv",
        "tools",
    )
    py = Path(base) / "openviking" / "bin" / "python"
    return str(py) if py.exists() else None


def expected_version():
    """The one ``EXPECTED_VERSION`` every ``ov_*_patch.py`` pins, read with ``ast``."""
    found = {}
    for path in sorted(NAV_DIR.glob("ov_*_patch.py")):
        for node in ast.parse(path.read_text()).body:
            if (
                isinstance(node, ast.Assign)
                and any(
                    isinstance(t, ast.Name) and t.id == "EXPECTED_VERSION"
                    for t in node.targets
                )
                and isinstance(node.value, ast.Constant)
            ):
                found[path.name] = node.value.value
    versions = set(found.values())
    if len(versions) != 1:
        sys.exit(f"run-nav-patch-tests: patches pin different versions: {found}")
    return versions.pop()


def pin_image_version(expected):
    """Return the installed openviking module (or None), its version mapped to ``expected``."""
    try:
        import openviking
    except ImportError:
        return None
    if "v" + str(openviking.__version__) == expected:
        openviking.__version__ = expected
        # Some cases swap a fake into sys.modules["openviking"] and drop it after,
        # so the next import re-runs __init__, which reads this module.
        version_module = sys.modules.get("openviking._version")
        if version_module is not None:
            version_module.version = version_module.__version__ = expected
    return openviking


def function_tests(module):
    """Wrap a module's top-level ``test_*`` functions, which discover skips."""
    return [
        unittest.FunctionTestCase(fn)
        for name, fn in sorted(vars(module).items())
        if name.startswith("test_")
        and callable(fn)
        and not isinstance(fn, type)
        and getattr(fn, "__module__", None) == module.__name__
    ]


def build_suite(loader):
    suite = unittest.TestSuite()
    for path in sorted(NAV_DIR.glob("test_*.py")):
        name = path.stem
        try:
            module = importlib.import_module(name)
        except unittest.SkipTest as exc:
            suite.addTest(skipped(name, str(exc)))
            continue
        except Exception as exc:  # noqa: BLE001 — an import error is a failing test
            suite.addTest(import_failure(name, exc))
            continue
        suite.addTests(loader.loadTestsFromModule(module))
        suite.addTests(function_tests(module))
    return suite


def skipped(name, reason):
    @unittest.skip(reason)
    def case():
        pass

    case.__name__ = f"{name}_skipped"
    return unittest.FunctionTestCase(case)


def import_failure(name, exc):
    def case():
        raise ImportError(f"failed to import {name}: {exc!r}") from exc

    case.__name__ = f"{name}_import"
    return unittest.FunctionTestCase(case)


def main():
    if os.environ.get(CHILD_FLAG) != "1":
        py = os.environ.get("NAV_PATCH_PYTHON") or tool_python() or sys.executable
        env = dict(os.environ)
        env[CHILD_FLAG] = "1"
        # On PYTHONPATH, not just sys.path, so ``site`` imports the loader at
        # startup as the image does and the "applied on import" cases see it.
        env["PYTHONPATH"] = os.pathsep.join(
            p for p in (str(NAV_DIR), env.get("PYTHONPATH")) if p
        )
        os.execve(py, [py, __file__, *sys.argv[1:]], env)

    # The patches and their tests use PEP 604 unions (``X | None``).
    if sys.version_info < (3, 10):
        sys.exit(
            f"run-nav-patch-tests: {sys.executable} is Python "
            f"{sys.version.split()[0]}; the nav-patch suite needs 3.10 or newer "
            "(set NAV_PATCH_PYTHON to one)"
        )

    expected = expected_version()
    openviking = pin_image_version(expected)
    if openviking is None:
        msg = (
            f"SKIP real-module cases: openviking is not importable by {sys.executable}"
        )
        if os.environ.get("NAV_PATCH_REQUIRE_OPENVIKING") == "1":
            sys.exit(f"run-nav-patch-tests: {msg}")
        print(msg)
    else:
        print(f"openviking {openviking.__version__} (patches pin {expected})")
    verbosity = 2 if "-v" in sys.argv[1:] else 1
    # buffer: the patches log every refusal the negative cases provoke; show a
    # case's output only when it fails.
    result = unittest.TextTestRunner(verbosity=verbosity, buffer=True).run(
        build_suite(unittest.defaultTestLoader)
    )
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
