#!/usr/bin/env python3
"""Regression cases for run-nav-patch-tests.py's interpreter choice (IMPR-1431 row 3).

Session af7cb2bf ran the suite with pop's ``openviking`` uv tool still at 0.4.20 after
the server moved to v0.5.1.1: the runner printed the two versions and then failed 48
real-module cases, with nothing naming the mismatch as the cause. These cases pin the
replacement: a mismatched uv tool is named in one line and left out (or, under
``NAV_PATCH_REQUIRE_OPENVIKING=1``, refused), and a mismatched interpreter the child
lands on is refused before any case runs. Stdlib only; nothing is executed.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_scripts" / "lib"))
from git_env import scrub_environ  # noqa: E402

scrub_environ()

import importlib.util
import types
import unittest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("runner", HERE / "run-nav-patch-tests.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

EXPECTED = runner.expected_version()
PIN = EXPECTED.removeprefix("v")
TOOL = "/fake/uv/tools/openviking/bin/python"


def no_probe(_py):
    raise AssertionError("the tool's version must not be probed")


class ChoosePython(unittest.TestCase):
    def choose(self, environ, tool=TOOL, found=PIN):
        return runner.choose_python(
            environ, EXPECTED, tool=lambda: tool, version=lambda _py: found
        )

    def test_explicit_interpreter_wins_without_a_probe(self):
        py, notice = runner.choose_python(
            {"NAV_PATCH_PYTHON": "/x/python"},
            EXPECTED,
            tool=lambda: TOOL,
            version=no_probe,
        )
        self.assertEqual((py, notice), ("/x/python", None))

    def test_no_tool_falls_back_to_this_interpreter(self):
        self.assertEqual(self.choose({}, tool=None), (sys.executable, None))

    def test_tool_on_the_pinned_release_is_used(self):
        self.assertEqual(self.choose({}), (TOOL, None))

    def test_unreadable_tool_version_keeps_the_tool(self):
        # The child still reports what it can import; the parent only acts on a
        # version it actually read.
        self.assertEqual(self.choose({}, found=None), (TOOL, None))

    def test_mismatched_tool_is_named_and_left_out(self):
        py, notice = self.choose({}, found="0.4.20")
        self.assertEqual(py, sys.executable)
        for part in (
            "0.4.20",
            EXPECTED,
            f'uv tool install "openviking=={PIN}" --force',
            "NAV_PATCH_PYTHON",
        ):
            self.assertIn(part, notice)

    def test_mismatched_tool_is_refused_when_openviking_is_required(self):
        with self.assertRaises(SystemExit) as caught:
            self.choose({"NAV_PATCH_REQUIRE_OPENVIKING": "1"}, found="0.4.20")
        self.assertIn("0.4.20", str(caught.exception.code))
        self.assertIn(f"openviking=={PIN}", str(caught.exception.code))


class Mismatch(unittest.TestCase):
    def test_absent_module_is_not_a_mismatch(self):
        self.assertIsNone(runner.mismatch(None, EXPECTED))

    def test_pinned_module_is_not_a_mismatch(self):
        module = types.SimpleNamespace(__version__=EXPECTED)
        self.assertIsNone(runner.mismatch(module, EXPECTED))

    def test_other_release_is_refused_with_both_versions(self):
        refusal = runner.mismatch(types.SimpleNamespace(__version__="0.4.20"), EXPECTED)
        for part in ("0.4.20", EXPECTED, "NAV_PATCH_PYTHON", f"openviking=={PIN}"):
            self.assertIn(part, refusal)


if __name__ == "__main__":
    unittest.main()
