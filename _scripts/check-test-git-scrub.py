#!/usr/bin/env python3
"""Fail a commit when a test suite does not open with the canonical git scrub.

IMPR-1419, ported from dotfiles' tests/check-test-git-scrub.py (IMPR-1266).
`git commit` runs pre-commit hooks with GIT_DIR, GIT_INDEX_FILE and friends
exported; they beat `git -C <dir>`, so a suite run from a hook that touches git
(directly, through a helper, or through the code it tests) acts on the
repository being committed. On homelab#254 a test that ran `git init`/`git add`
in a temp copy rewrote a linked worktree's index from 1498 entries to 99 that
way. Deciding statically which suites touch git misses imported helpers and the
code under test, so the rule is one fixed header for every suite: after the
module docstring and any `from __future__` imports, the first statements are
exactly

    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[N] / "_scripts" / "lib"))
    from git_env import scrub_environ  # noqa: E402

    scrub_environ()

with N the number of directories between the repo root and the suite (1 for
`_scripts/test-x.py`), compared as AST nodes with nothing else before it.

A suite is any `test-*.py` or `test_*.py` under the repo, outside hidden
directories, whether or not a hook runs it today. EXEMPT lists the suites that
run somewhere other than a checkout, where `_scripts/lib` does not exist.

This protects against accidental GIT_* inheritance. It does not stop a suite
that deliberately defeats it (a later `os.environ.update`, a `GIT_DIR` set on
purpose).

Exit 0 when every suite complies; 1 with the list otherwise.
Tests: python3 _scripts/test-check-test-git-scrub.py
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HEADER = """\
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[{depth}] / "_scripts" / "lib"))
from git_env import scrub_environ

scrub_environ()
"""
# Relative path: why it runs outside a checkout.
EXEMPT = {
    "benchmarks/harbor/tasks/hello-world/tests/test_state.py": (
        "tests/test.sh runs it inside the Harbor task container"
    ),
}
SKIP_DIRS = {"node_modules", "__pycache__"}


def canonical(depth: int) -> list[str]:
    tree = ast.parse(HEADER.format(depth=depth))
    return [ast.dump(node) for node in tree.body]


def header_dumps(body: list[ast.stmt], count: int) -> list[str]:
    """The `count` statements after the docstring and `from __future__` imports."""
    i = 0
    first = body[0] if body else None
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        i = 1
    while (
        i < len(body)
        and isinstance(body[i], ast.ImportFrom)
        and body[i].module == "__future__"
    ):
        i += 1
    return [ast.dump(node) for node in body[i : i + count]]


def complies(path: Path, root: Path) -> bool:
    try:
        tree = ast.parse(path.read_text(), filename=str(path))
    except SyntaxError:
        return True  # the language checks report it
    want = canonical(len(path.relative_to(root).parts) - 1)
    return header_dumps(tree.body, len(want)) == want


def is_suite(name: str) -> bool:
    return name.endswith(".py") and name.startswith(("test-", "test_"))


def suites(root: Path) -> list[Path]:
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS
        ]
        found += [Path(dirpath) / f for f in filenames if is_suite(f)]
    return sorted(p for p in found if p.relative_to(root).as_posix() not in EXEMPT)


def offenders(root: Path) -> list[Path]:
    return [p for p in suites(root) if not complies(p, root)]


def main(argv: list[str]) -> int:
    root = Path(argv[1]).resolve() if len(argv) > 1 else REPO
    bad = offenders(root)
    if not bad:
        return 0
    print(
        "check-test-git-scrub: these suites do not open with the canonical scrub "
        "header (see the docstring of _scripts/check-test-git-scrub.py):",
        file=sys.stderr,
    )
    for path in bad:
        rel = path.relative_to(root)
        print(f"  {rel}  (parents[{len(rel.parts) - 1}])", file=sys.stderr)
    print(
        "pre-commit exports GIT_DIR to hooks, which beats `git -C`; a suite that "
        "reaches git would act on the repository being committed.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
