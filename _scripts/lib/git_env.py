"""Clear git's environment before a test runs git in a throwaway repo (IMPR-1419).

Ported from dotfiles' tests/lib/git_env.py (IMPR-1266). `git commit` runs
pre-commit hooks with GIT_DIR, GIT_INDEX_FILE and friends exported, and they beat
both `git -C <dir>` and the working directory. A test suite run from a hook
inherits them, so its fixture `git init` or `git add` acts on the repository
being committed: on homelab#254 a round-4 test rewrote a linked worktree's index
from 1498 entries to 99 that way. Every GIT_* variable goes, not a fixed list,
because git keeps adding names (GIT_TRACE2_EVENT, …).

Every suite imports it through the header _scripts/check-test-git-scrub.py
requires, with N the number of directories between the repo root and the suite:

    sys.path.insert(0, str(Path(__file__).resolve().parents[N] / "_scripts" / "lib"))
    from git_env import scrub_environ
"""

from __future__ import annotations

import os
from typing import Mapping


def is_git_var(name: str) -> bool:
    return name.startswith("GIT_")


def clean_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """A copy of env (default os.environ) without any GIT_* variable."""
    source = os.environ if env is None else env
    return {k: v for k, v in source.items() if not is_git_var(k)}


def scrub_environ() -> None:
    """Remove every GIT_* variable from os.environ, for this process and its children."""
    for name in [k for k in os.environ if is_git_var(k)]:
        del os.environ[name]
