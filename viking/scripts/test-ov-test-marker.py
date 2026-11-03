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
            "TZ": "America/Chicago",
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

    def test_non_utf8_kubectl_stderr_exits_two(self):
        (self.dir / "annotate.fail").write_bytes(b"credential helper failed: \xff\n")
        self.serve(deployment({**OTHER, "session": "me-123"}))
        for args in (["set", "--item", "X"], ["clear"]):
            with self.subTest(args=args):
                proc = self.run_script(*args)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertIn("credential helper failed", proc.stderr)
                self.assertNotIn("Traceback", proc.stderr)

    def run_with_failing_decoder(self, *args):
        """The script with json.loads raising RecursionError on the mark, as
        system Python 3.9 does on 1,500 nested arrays."""
        wrapper = (
            "import json, runpy, sys\n"
            "real = json.loads\n"
            "def loads(s, *a, **k):\n"
            "    if isinstance(s, str) and s.startswith('[[['):\n"
            "        raise RecursionError('maximum recursion depth exceeded')\n"
            "    return real(s, *a, **k)\n"
            "json.loads = loads\n"
            f"sys.argv = [{str(SCRIPT)!r}, *sys.argv[1:]]\n"
            f"runpy.run_path({str(SCRIPT)!r}, run_name='__main__')\n"
        )
        env = {
            **os.environ,
            "PATH": f"{self.dir}{os.pathsep}{os.environ['PATH']}",
            "STUB_DIR": str(self.dir),
            "CLAUDE_CODE_SESSION_ID": "me-123",
        }
        return subprocess.run(
            [sys.executable, "-c", wrapper, *args],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    def test_undecodable_mark_is_unreadable_and_force_clears(self):
        self.serve(deployment(raw="[" * 1500 + "]" * 1500))
        proc = self.run_with_failing_decoder("show")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("unreadable mark", proc.stdout)
        proc = self.run_with_failing_decoder("clear")
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertEqual(self.annotate_calls(), [])
        proc = self.run_with_failing_decoder("clear", "--force")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self.annotate_calls()), 1)

    def test_unexpected_failure_exits_two_without_traceback(self):
        wrapper = (
            "import runpy, sys\n"
            f"sys.argv = [{str(SCRIPT)!r}, 'show']\n"
            f"ns = runpy.run_path({str(SCRIPT)!r})\n"
            "def boom(_args): raise KeyError('x')\n"
            "ns['cmd_show'].__globals__['cmd_show'] = boom\n"
            "sys.exit(ns['main']())\n"
        )
        env = {
            **os.environ,
            "PATH": f"{self.dir}{os.pathsep}{os.environ['PATH']}",
            "STUB_DIR": str(self.dir),
        }
        proc = subprocess.run(
            [sys.executable, "-c", wrapper],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("failed (KeyError", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def run_with_closed(self, stream, *args, unbuffered=False):
        """The script with its stdout or stderr a pipe nobody reads, as when the
        consumer (`| head`, a closed terminal) has gone."""
        read_end, write_end = os.pipe()
        os.close(read_end)
        env = {
            **os.environ,
            "PATH": f"{self.dir}{os.pathsep}{os.environ['PATH']}",
            "STUB_DIR": str(self.dir),
            "CLAUDE_CODE_SESSION_ID": "me-123",
        }
        env.pop("PYTHONUNBUFFERED", None)
        if unbuffered:
            env["PYTHONUNBUFFERED"] = "1"
        streams = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
        streams[stream] = write_end
        try:
            return subprocess.run(
                [sys.executable, str(SCRIPT), *args],
                text=True,
                env=env,
                check=False,
                **streams,
            )
        finally:
            os.close(write_end)

    def test_closed_stdout_keeps_the_exit_code(self):
        mine = {**OTHER, "session": "me-123"}
        cases = (
            (deployment(), ["show"], 0),
            (deployment(), ["set", "--item", "X"], 1),
            (deployment(mine), ["clear"], 1),
        )
        for unbuffered in (False, True):
            for served, args, writes in cases:
                with self.subTest(args=args, unbuffered=unbuffered):
                    self.serve(served)
                    before = len(self.annotate_calls())
                    proc = self.run_with_closed("stdout", *args, unbuffered=unbuffered)
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    self.assertNotIn("Traceback", proc.stderr)
                    self.assertIn("output lost", proc.stderr)
                    self.assertEqual(len(self.annotate_calls()) - before, writes)

    def run_streams(self, *args, stdout="pipe", stderr="pipe", unbuffered=False):
        """The script with each of stdout and stderr a captured pipe ("pipe"), a
        pipe nobody reads ("broken"), or a descriptor closed before the
        interpreter starts ("closed")."""
        env = {
            **os.environ,
            "PATH": f"{self.dir}{os.pathsep}{os.environ['PATH']}",
            "STUB_DIR": str(self.dir),
            "CLAUDE_CODE_SESSION_ID": "me-123",
        }
        env.pop("PYTHONUNBUFFERED", None)
        if unbuffered:
            env["PYTHONUNBUFFERED"] = "1"
        streams, ends, shut = {}, [], []
        for name, fd, mode in (("stdout", 1, stdout), ("stderr", 2, stderr)):
            if mode == "pipe":
                streams[name] = subprocess.PIPE
            elif mode == "broken":
                read_end, write_end = os.pipe()
                os.close(read_end)
                ends.append(write_end)
                streams[name] = write_end
            else:
                streams[name] = subprocess.DEVNULL
                shut.append(fd)

        def close_fds():
            for fd in shut:
                os.close(fd)

        try:
            return subprocess.run(
                [sys.executable, str(SCRIPT), *args],
                text=True,
                env=env,
                check=False,
                preexec_fn=close_fds if shut else None,
                **streams,
            )
        finally:
            for end in ends:
                os.close(end)

    def test_no_stream_state_changes_the_exit_code(self):
        modes = ("pipe", "broken", "closed")
        cases = (
            ("get fails", deployment(), True, ["show"], 2),
            ("refused", deployment(OTHER), False, ["set", "--item", "X"], 3),
            ("set", deployment(), False, ["set", "--item", "X"], 0),
            ("usage error", deployment(), False, ["bogus"], 2),
        )
        for name, served, fail, args, code in cases:
            self.serve(served)
            failing = self.dir / "get.fail"
            if fail:
                failing.write_text("forbidden\n", encoding="utf-8")
            elif failing.exists():
                failing.unlink()
            for out in modes:
                for err in modes:
                    for unbuffered in (False, True):
                        with self.subTest(
                            case=name, stdout=out, stderr=err, unbuffered=unbuffered
                        ):
                            proc = self.run_streams(
                                *args, stdout=out, stderr=err, unbuffered=unbuffered
                            )
                            self.assertEqual(proc.returncode, code)
                            if out == "pipe":  # stderr text never lands on stdout
                                self.assertNotIn("forbidden", proc.stdout)
                                self.assertNotIn("refused", proc.stdout)
                                self.assertNotIn("usage", proc.stdout)

    def test_show_prints_the_start_in_local_time(self):
        self.serve(deployment(OTHER))
        proc = self.run_script("show")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("since 2026-10-31 19:42 CDT (", proc.stdout)

    def test_closed_stderr_keeps_the_exit_code(self):
        self.serve(deployment(OTHER))
        for unbuffered in (False, True):
            with self.subTest(unbuffered=unbuffered):
                proc = self.run_with_closed(
                    "stderr", "set", "--item", "X", unbuffered=unbuffered
                )
                self.assertEqual(proc.returncode, 3)
                (self.dir / "get.fail").write_text("forbidden\n", encoding="utf-8")
                proc = self.run_with_closed("stderr", "show", unbuffered=unbuffered)
                self.assertEqual(proc.returncode, 2)
                (self.dir / "get.fail").unlink()
        self.assertEqual(self.annotate_calls(), [])

    def test_set_without_a_session_is_a_usage_error(self):
        proc = self.run_script("set", "--item", "X", session=None)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--session", proc.stderr)
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
