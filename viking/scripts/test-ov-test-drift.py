"""Tests for ov-test-drift.py. Needs PyYAML, as the script does:

    uv run --no-project --with pyyaml python viking/scripts/test-ov-test-drift.py

No cluster: each case runs the script as a subprocess with a stub `kubectl` first
on PATH. The stub's output is JSON, which is also YAML.
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

SCRIPT = Path(__file__).resolve().parent / "ov-test-drift.py"
STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$STUB_DIR/calls"
case "$1" in
  kustomize) name=render ;;
  *) name=live ;;
esac
if [ -f "$STUB_DIR/$name.fail" ]; then
  cat "$STUB_DIR/$name.fail" >&2
  exit 1
fi
cat "$STUB_DIR/$name.json"
"""
SITE = "line 1\nline 2\n"
PATCH = "a\nb\nc\n"


def configmap(data, name="ov-nav-patch", namespace="viking", binary=None):
    obj = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": name, "namespace": namespace},
        "data": data,
    }
    if binary:
        obj["binaryData"] = binary
    return obj


class DriftTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        stub = self.dir / "kubectl"
        stub.write_text(STUB, encoding="utf-8")
        stub.chmod(0o755)
        self.write(
            "render", configmap({"sitecustomize.py": SITE, "ov_x_patch.py": PATCH})
        )
        self.write(
            "live", configmap({"sitecustomize.py": SITE, "ov_x_patch.py": PATCH})
        )

    def write(self, name, obj):
        (self.dir / f"{name}.json").write_text(json.dumps(obj), encoding="utf-8")

    def run_script(self, *args):
        env = {
            **os.environ,
            "PATH": f"{self.dir}{os.pathsep}{os.environ['PATH']}",
            "STUB_DIR": str(self.dir),
        }
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    def test_no_drift_exits_zero(self):
        proc = self.run_script()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("matches", proc.stdout)
        self.assertIn("2 keys", proc.stdout)

    def test_changed_key_counts_lines(self):
        self.write(
            "render",
            configmap({"sitecustomize.py": SITE, "ov_x_patch.py": "a\nB\nc\nd\n"}),
        )
        proc = self.run_script()
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("ov_x_patch.py: +2 -1", proc.stdout)
        self.assertNotIn("sitecustomize.py", proc.stdout)

    def test_key_missing_from_live(self):
        self.write("live", configmap({"sitecustomize.py": SITE}))
        proc = self.run_script()
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("ov_x_patch.py: missing from live (+3)", proc.stdout)

    def test_key_only_in_live(self):
        self.write("render", configmap({"sitecustomize.py": SITE}))
        proc = self.run_script()
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("ov_x_patch.py: only in live (-3)", proc.stdout)

    def test_trailing_newline_alone_is_drift(self):
        self.write(
            "live",
            configmap({"sitecustomize.py": SITE, "ov_x_patch.py": PATCH.rstrip("\n")}),
        )
        proc = self.run_script()
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("ov_x_patch.py: +1 -1", proc.stdout)

    def test_binary_data_key_is_compared(self):
        self.write(
            "live",
            configmap(
                {"sitecustomize.py": SITE},
                binary={"ov_x_patch.py": "YQo="},
            ),
        )
        proc = self.run_script()
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("ov_x_patch.py: +3 -1", proc.stdout)

    def test_every_file_in_one_screen(self):
        names = [f"ov_{i}_patch.py" for i in range(13)]
        self.write("render", configmap({n: "new\n" for n in names}))
        self.write("live", configmap({n: "old\n" for n in names}))
        proc = self.run_script()
        self.assertEqual(proc.returncode, 1, proc.stderr)
        for name in names:
            self.assertIn(f"{name}: +1 -1", proc.stdout)
        self.assertLessEqual(len(proc.stdout.splitlines()), 24)

    def test_unreachable_cluster_exits_two(self):
        (self.dir / "live.fail").write_text(
            "Unable to connect to the server: dial tcp: i/o timeout\n", encoding="utf-8"
        )
        proc = self.run_script()
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn("Unable to connect", proc.stderr)
        self.assertEqual(proc.stdout, "")

    def test_render_failure_exits_two(self):
        (self.dir / "render.fail").write_text(
            "accumulating resources\n", encoding="utf-8"
        )
        proc = self.run_script()
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn("accumulating resources", proc.stderr)

    def test_unparseable_render_exits_two(self):
        (self.dir / "render.json").write_text("{: [\n", encoding="utf-8")
        proc = self.run_script()
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertNotIn("Traceback", proc.stderr)

    def test_unparseable_live_exits_two(self):
        (self.dir / "live.json").write_text("not json", encoding="utf-8")
        proc = self.run_script()
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertNotIn("Traceback", proc.stderr)

    def test_render_without_the_configmap_exits_two(self):
        self.write("render", configmap({}, name="something-else"))
        proc = self.run_script()
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn("0 ConfigMaps", proc.stderr)

    def test_runs_only_read_commands(self):
        self.run_script()
        calls = (self.dir / "calls").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0].startswith("kustomize "), calls)
        self.assertRegex(
            calls[1],
            r"^-n viking --request-timeout=\d+s get configmap ov-nav-patch -o json$",
        )


if __name__ == "__main__":
    unittest.main()
