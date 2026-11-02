"""Tests for ov-test-marker.py:

    python3 viking/scripts/test-ov-test-marker.py

No cluster: each case runs the script as a subprocess with a stub `kubectl` first
on PATH, which serves a Deployment from a file and records every call.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_scripts" / "lib"))
from git_env import scrub_environ  # noqa: E402

scrub_environ()

import json
import os
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parent / "ov-test-marker.py"
KEY = "homelab.nathanwhyte.dev/ov-test-in-use"
STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$STUB_DIR/calls"
case "$*" in
  *" get deployment "*) name=get ;;
  *" annotate "*) name=annotate ;;
  *) echo "unexpected: $*" >&2; exit 9 ;;
esac
if [ -f "$STUB_DIR/$name.fail" ]; then
  cat "$STUB_DIR/$name.fail" >&2
  exit 1
fi
[ "$name" = get ] && cat "$STUB_DIR/deploy.json"
exit 0
"""
OTHER = {
    "item": "BUG-1282",
    "session": "f4978b92",
    "host": "pop",
    "since": "2026-11-01T00:42:00Z",
}


def deployment(mark=None, raw=None):
    meta = {"name": "openviking-test", "namespace": "viking", "resourceVersion": "41"}
    if mark is not None or raw is not None:
        meta["annotations"] = {KEY: raw if raw is not None else json.dumps(mark)}
    return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": meta}


class MarkerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        stub = self.dir / "kubectl"
        stub.write_text(STUB, encoding="utf-8")
        stub.chmod(0o755)
        self.serve(deployment())

    def serve(self, obj):
        (self.dir / "deploy.json").write_text(json.dumps(obj), encoding="utf-8")

    def run_script(self, *args, session="me-123"):
        env = {
            **os.environ,
            "PATH": f"{self.dir}{os.pathsep}{os.environ['PATH']}",
            "STUB_DIR": str(self.dir),
        }
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        if session:
            env["CLAUDE_CODE_SESSION_ID"] = session
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    def calls(self):
        path = self.dir / "calls"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def annotate_calls(self):
        return [c for c in self.calls() if " annotate " in c]

    def written_mark(self):
        (call,) = self.annotate_calls()
        return json.loads(call.split(f"{KEY}=", 1)[1])

    def test_show_unmarked(self):
        proc = self.run_script("show")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "in use: not marked")
        self.assertEqual(self.annotate_calls(), [])

    def test_show_marked(self):
        self.serve(deployment(OTHER))
        proc = self.run_script("show")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("in use: BUG-1282, session f4978b92, host pop", proc.stdout)

    def test_show_unreadable_mark(self):
        self.serve(deployment(raw="someone was here"))
        proc = self.run_script("show")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("unreadable mark 'someone was here'", proc.stdout)

    def test_set_on_a_free_stack(self):
        proc = self.run_script("set", "--item", "BUG-1285", "--note", "canary")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        (call,) = self.annotate_calls()
        self.assertIn(
            "annotate deployment openviking-test --resource-version=41 --overwrite ",
            call,
        )
        mark = self.written_mark()
        self.assertEqual(mark["item"], "BUG-1285")
        self.assertEqual(mark["session"], "me-123")
        self.assertEqual(mark["note"], "canary")
        self.assertRegex(mark["since"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertTrue(mark["host"])

    def test_set_refuses_another_sessions_mark(self):
        self.serve(deployment(OTHER))
        proc = self.run_script("set", "--item", "BUG-1285")
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertIn("BUG-1282", proc.stderr)
        self.assertEqual(self.annotate_calls(), [])

    def test_set_refuses_an_unreadable_mark(self):
        self.serve(deployment(raw="someone was here"))
        proc = self.run_script("set", "--item", "BUG-1285")
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertEqual(self.annotate_calls(), [])

    def test_set_force_takes_the_mark(self):
        self.serve(deployment(OTHER))
        proc = self.run_script("set", "--item", "BUG-1285", "--force")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.written_mark()["session"], "me-123")

    def test_refresh_keeps_the_start_time(self):
        self.serve(deployment({**OTHER, "session": "me-123"}))
        proc = self.run_script("set", "--item", "BUG-1285")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        mark = self.written_mark()
        self.assertEqual(mark["since"], OTHER["since"])
        self.assertEqual(mark["item"], "BUG-1285")

    def test_clear_own_mark(self):
        self.serve(deployment({**OTHER, "session": "me-123"}))
        proc = self.run_script("clear")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        (call,) = self.annotate_calls()
        self.assertTrue(
            call.endswith(
                f"annotate deployment openviking-test --resource-version=41 {KEY}-"
            ),
            call,
        )

    def test_clear_refuses_another_sessions_mark(self):
        self.serve(deployment(OTHER))
        proc = self.run_script("clear")
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertEqual(self.annotate_calls(), [])

    def test_clear_force(self):
        self.serve(deployment(OTHER))
        proc = self.run_script("clear", "--force")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self.annotate_calls()), 1)

    def test_clear_unmarked_is_a_no_op(self):
        proc = self.run_script("clear")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("nothing to clear", proc.stdout)
        self.assertEqual(self.annotate_calls(), [])

    def test_concurrent_change_exits_two(self):
        (self.dir / "annotate.fail").write_text(
            "the object has been modified; please apply your changes\n",
            encoding="utf-8",
        )
        proc = self.run_script("set", "--item", "BUG-1285")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("has been modified", proc.stderr)

    def test_unreachable_cluster_exits_two(self):
        (self.dir / "get.fail").write_text(
            "Unable to connect to the server\n", encoding="utf-8"
        )
        for args in (["show"], ["set", "--item", "X"], ["clear"]):
            with self.subTest(args=args):
                proc = self.run_script(*args)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertIn("Unable to connect", proc.stderr)
                self.assertNotIn("Traceback", proc.stderr)

    def test_not_a_deployment_exits_two(self):
        (self.dir / "deploy.json").write_text("[]", encoding="utf-8")
        proc = self.run_script("show")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_kubectl_missing_exits_two(self):
        env_path = str(self.dir / "empty")
        os.mkdir(env_path)
        for args in (["show"], ["set", "--item", "X"], ["clear"]):
            with self.subTest(args=args):
                proc = subprocess.run(
                    [sys.executable, str(SCRIPT), *args],
                    capture_output=True,
                    text=True,
                    env={
                        **os.environ,
                        "PATH": env_path,
                        "CLAUDE_CODE_SESSION_ID": "me",
                    },
                    check=False,
                )
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertIn("ov-test-marker: kubectl get:", proc.stderr)
                self.assertNotIn("Traceback", proc.stderr)

    def test_show_escapes_an_unprintable_mark(self):
        self.serve(deployment({**OTHER, "item": "X\ud800", "note": "a\nb"}))
        for encoding in ("utf-8", "ascii"):
            with self.subTest(encoding=encoding):
                env = {
                    **os.environ,
                    "PATH": f"{self.dir}{os.pathsep}{os.environ['PATH']}",
                    "STUB_DIR": str(self.dir),
                    "PYTHONIOENCODING": encoding,
                }
                proc = subprocess.run(
                    [sys.executable, str(SCRIPT), "show"],
                    capture_output=True,
                    text=True,
                    env=env,
                    check=False,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(len(proc.stdout.splitlines()), 1, proc.stdout)
                self.assertIn("X\\ud800", proc.stdout)

    def test_set_without_a_session_is_a_usage_error(self):
        proc = self.run_script("set", "--item", "X", session=None)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--session", proc.stderr)
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
