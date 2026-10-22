#!/usr/bin/env python3
"""_scripts/check-test-git-scrub.py: every suite opens with the canonical scrub.

IMPR-1419. Run: python3 _scripts/test-check-test-git-scrub.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_scripts" / "lib"))
from git_env import scrub_environ  # noqa: E402

scrub_environ()

import os  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402
from unittest import mock  # noqa: E402

import git_env  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
CHECK = REPO / "_scripts" / "check-test-git-scrub.py"


def header(depth: int = 1, call: str = "scrub_environ()") -> str:
    return (
        "import sys\nfrom pathlib import Path\n\n"
        "sys.path.insert(0, str(Path(__file__).resolve()"
        f'.parents[{depth}] / "_scripts" / "lib"))\n'
        f"from git_env import scrub_environ  # noqa: E402\n\n{call}\n"
    )


CANON = header()


class CheckTestGitScrub(unittest.TestCase):
    def run_check(self, **suites: str) -> subprocess.CompletedProcess:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for name, body in suites.items():
            # "a__b__test-x.py" stands for a/b/test-x.py; a bare name is _scripts/.
            parts = name.split("__")
            path = root.joinpath(*parts) if len(parts) > 1 else root / "_scripts" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
        return subprocess.run(
            [sys.executable, str(CHECK), str(root)],
            capture_output=True,
            text=True,
            check=False,
        )

    def passes(self, **suites: str):
        r = self.run_check(**suites)
        self.assertEqual(r.returncode, 0, r.stderr)

    def fails(self, *named: str, **suites: str):
        r = self.run_check(**suites)
        self.assertEqual(r.returncode, 1, r.stderr)
        for name in named:
            self.assertIn(name, r.stderr)

    def test_the_canonical_header_passes(self):
        self.passes(**{"test-a.py": CANON + "import json\n"})

    def test_a_docstring_shebang_and_future_imports_may_come_first(self):
        body = (
            '#!/usr/bin/env python3\n"""Suite."""\n\n'
            "from __future__ import annotations\n\n" + CANON
        )
        self.passes(**{"test-a.py": body})

    def test_the_depth_matches_the_suites_directory(self):
        self.passes(
            **{
                "viking__scripts__test-b.py": header(2),
                "viking__manifests__test__nav-patch__test_ov_c.py": header(4),
            }
        )
        self.fails("viking/scripts/test-b.py", **{"viking__scripts__test-b.py": CANON})
        self.fails("_scripts/test-a.py", **{"test-a.py": header(2)})

    def test_a_suite_at_the_repo_root_uses_parents_0(self):
        # run_check puts a bare name in _scripts/, so build the root suite here.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "test-r.py").write_text(header(0))
        r = subprocess.run(
            [sys.executable, str(CHECK), str(root)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_comments_and_blank_lines_do_not_matter(self):
        body = "# a comment\n" + CANON.replace("\n\n", "\n\n\n  \n")
        self.passes(**{"test-a.py": body})

    def test_a_call_only_in_an_except_branch_fails(self):
        body = (
            "import sys\nfrom pathlib import Path\n\n"
            'sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_scripts" / "lib"))\n'
            "try:\n    from git_env import scrub_environ\n"
            "except ImportError:\n    from git_env import scrub_environ\n"
            "    scrub_environ()\n"
        )
        self.fails("_scripts/test-a.py", **{"test-a.py": body})

    def test_a_qualified_call_on_another_object_fails(self):
        self.fails(
            "_scripts/test-a.py",
            **{"test-a.py": header(call="fixture.scrub_environ()")},
        )

    def test_a_shadowed_scrub_environ_fails(self):
        # Rebinding the name after the real call ran is harmless; in place of the
        # import, the header no longer calls the helper.
        self.passes(**{"test-a.py": CANON + "scrub_environ = lambda: None\n"})
        shadowed = CANON.replace(
            "from git_env import scrub_environ  # noqa: E402\n",
            "scrub_environ = lambda: None\n",
        )
        self.fails("_scripts/test-b.py", **{"test-b.py": shadowed})

    def test_work_before_the_scrub_fails(self):
        saved = "import os\nSAVED_ENV = os.environ.copy()\n" + CANON
        self.fails("_scripts/test-a.py", **{"test-a.py": saved})
        sub = "import subprocess\nsubprocess.run(['git', 'init'])\n" + CANON
        self.fails("_scripts/test-b.py", **{"test-b.py": sub})
        self.fails("_scripts/test-c.py", **{"test-c.py": "import json\n" + CANON})

    def test_a_call_in_a_function_or_under_a_conditional_fails(self):
        func = CANON.replace("\nscrub_environ()\n", "\ndef f():\n    scrub_environ()\n")
        self.fails("_scripts/test-a.py", **{"test-a.py": func})
        main = CANON.replace(
            "\nscrub_environ()\n", "\nif __name__ == '__main__':\n    scrub_environ()\n"
        )
        self.fails("_scripts/test-b.py", **{"test-b.py": main})

    def test_clean_env_or_a_bare_import_fails(self):
        self.fails(
            "_scripts/test-a.py", **{"test-a.py": "from git_env import clean_env\n"}
        )
        self.fails(
            "_scripts/test-b.py",
            **{"test-b.py": CANON.replace("scrub_environ()\n", "")},
        )

    def test_a_suite_with_no_git_still_needs_the_header(self):
        self.fails("_scripts/test-b.py", **{"test-b.py": "print('no git here')\n"})

    def test_hyphen_and_underscore_suites_anywhere_are_scanned(self):
        self.fails(
            "grafana/test-x.py",
            "viking/tools/test_y.py",
            **{
                "grafana__test-x.py": "pass\n",
                "viking__tools__test_y.py": "pass\n",
                "grafana__test-ok.py": header(1),
            },
        )

    def test_non_suites_and_hidden_directories_are_not_scanned(self):
        self.passes(
            **{
                "check-k.py": "pass\n",
                "testing.py": "pass\n",
                ".worktrees__x__test-a.py": "pass\n",
                "viking__node_modules__test-a.py": "pass\n",
            }
        )

    def test_the_exempt_suite_is_not_scanned_and_only_it(self):
        self.passes(
            **{"benchmarks__harbor__tasks__hello-world__tests__test_state.py": "pass\n"}
        )
        self.fails(
            "benchmarks/harbor/tasks/other/tests/test_state.py",
            **{"benchmarks__harbor__tasks__other__tests__test_state.py": "pass\n"},
        )

    def test_the_7002e82_case_without_the_header_fails(self):
        # homelab#254 round 4: a suite whose case ran `git init`/`git add` in a
        # temp copy, with no scrub, rewrote a linked worktree's index (R5.a-1).
        body = (
            '"""Tests for check-nav-patch-files.py."""\n\n'
            "import subprocess\nimport tempfile\nimport unittest\n\n\n"
            "class T(unittest.TestCase):\n"
            "    def test_untracked_patch_is_not_in_a_git_trees_patch_set(self):\n"
            "        with tempfile.TemporaryDirectory() as d:\n"
            "            subprocess.run(['git', 'init', '-q', d], check=True)\n"
            "            subprocess.run(['git', '-C', d, 'add', '-A'], check=True)\n"
        )
        self.fails(
            "viking/scripts/test-check-nav-patch-files.py",
            **{"viking__scripts__test-check-nav-patch-files.py": body},
        )

    def test_repo_suites_comply(self):
        r = subprocess.run(
            [sys.executable, str(CHECK)], capture_output=True, text=True, check=False
        )
        self.assertEqual(r.returncode, 0, r.stderr)


class GitEnv(unittest.TestCase):
    ENV = {
        "GIT_DIR": "/x/.git",
        "GIT_INDEX_FILE": "/x/index",
        "GIT_NEW": "1",
        "HOME": "/h",
    }

    def test_clean_env_drops_every_git_variable(self):
        self.assertEqual(git_env.clean_env(self.ENV), {"HOME": "/h"})

    def test_scrub_environ_clears_this_process_and_its_children(self):
        with mock.patch.dict(os.environ, self.ENV):
            git_env.scrub_environ()
            self.assertFalse([k for k in os.environ if k.startswith("GIT_")])
            out = subprocess.run(
                [sys.executable, "-c", "import os; print(os.environ.get('GIT_DIR'))"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            self.assertEqual(out.strip(), "None")


if __name__ == "__main__":
    unittest.main()
