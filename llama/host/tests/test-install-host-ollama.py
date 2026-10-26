#!/usr/bin/env python3
"""Execute the real installer with an unprivileged fake filesystem and adapters.

The core is a boundary fake: its own suite proves recipe/runtime semantics. This
suite proves installer ordering, failure propagation, installation and readback.
No host command, model API, cluster, or privileged test path is used.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "_scripts" / "lib"))
from git_env import scrub_environ  # noqa: E402

scrub_environ()

import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest

HOST = Path(__file__).resolve().parents[1]

CORE = r"""
import json, os, pathlib, sys
root = pathlib.Path(os.environ["FAKE_ROOT"])
args = sys.argv[1:]
def option(name, default):
    return args[args.index(name) + 1] if name in args else default
commands = ("version", "prepare", "warm", "verify", "render", "files-check", "pair")
command = next(arg for arg in args if arg in commands)
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write("core " + command + " " + " ".join(args) + "\n")
if os.environ.get("FAIL_CORE") == command:
    sys.exit("injected " + command + " failure")
if command == "version":
    print("0.40.11")
elif command == "files-check":
    installed = pathlib.Path(option("--installed", ""))
    source = pathlib.Path(option("--config", ""))
    if source.read_bytes() != (installed / "model-posture.json").read_bytes():
        sys.exit("changed posture")
    for recipe in pathlib.Path(option("--recipes", "")).glob("*.Modelfile"):
        if recipe.read_bytes() != (installed / "modelfiles" / recipe.name).read_bytes():
            sys.exit("changed recipe revision")
elif command in ("warm", "verify"):
    state = pathlib.Path(option("--state", str(root / "var/lib/ollama-preparation")))
    receipt = state / "verified.json"
    generation = (root / "generation").read_text()
    if command == "warm":
        state.mkdir(parents=True, exist_ok=True)
        receipt.write_text(json.dumps({"generation": generation}))
    else:
        if os.environ.get("FAKE_STALE_RECEIPT") or json.loads(receipt.read_text())["generation"] != generation:
            if "--stop-on-failure" in args:
                with open(os.environ["FAKE_LOG"], "a") as log:
                    log.write("core stop-captured-generation " + generation + "\n")
                (root / "online").write_text("no")
            sys.exit("receipt does not describe current daemon")
    print('{"verified": true}')
else:
    print('{}')
"""

COMMAND = r"""
import json, os, pathlib, subprocess, sys
root = pathlib.Path(os.environ["FAKE_ROOT"])
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(name + " " + " ".join(args) + "\n")
if name == "hostname":
    print("timmy")
elif name == "ollama":
    print("Warning: client version is 0.40.11")
elif name == "id":
    if "-nG" in args:
        print("ollama video render")
elif name == "ss":
    print('LISTEN 0 4096 0.0.0.0:11434 0.0.0.0:* users:(("ollama",pid=123,fd=1))')
elif name == "iptables-save":
    pass
elif name == "flock":
    sys.exit(1 if os.environ.get("FAKE_LOCK_BUSY") else 0)
elif name == "curl":
    if (root / "online").read_text() != "yes":
        sys.exit(7)
    print('{"version":"0.40.11"}')
elif name == "k3s":
    if os.environ.get("FAKE_CUTOVER_FAIL"):
        sys.exit(1)
    if "service" in args:
        print('{"spec":{"type":"ClusterIP"}}')
    elif "endpointslice" in args:
        print('{"items":[{"metadata":{"name":"ollama-timmy-host"},"endpoints":[{"addresses":["192.168.1.19"]}]}]}')
    elif "name" not in args:
        print('{"items":[]}')
elif name == "systemctl":
    action = args[0]
    if action == "is-enabled":
        print("enabled")
    elif action == "show":
        print("loaded" if (root / "etc/systemd/system" / (args[1] + ".service")).exists() else "not-found")
    elif action == "is-active":
        active = (root / "online").read_text() == "yes"
        if args[-1] == "ollama-warm":
            active = (root / "warm-active").exists()
        if "--quiet" not in args:
            print("active" if active else "inactive")
        sys.exit(0 if active else 3)
    elif action == "stop":
        if "ollama-warm" in args:
            (root / "warm-active").unlink(missing_ok=True)
        if "ollama" in args:
            (root / "online").write_text("no")
    elif action == "restart" and args[-1] == "ollama":
        (root / "generation").write_text(str(int((root / "generation").read_text()) + 1))
        (root / "online").write_text("yes")
    elif action == "start" and args[-1] == "ollama-warm":
        if not (root / "warm-active").exists():
            result = subprocess.run([sys.executable, str(root / "opt/ollama-host/ollama_prepare.py"), "warm"])
            if result.returncode:
                sys.exit(result.returncode)
            (root / "warm-active").touch()
    elif action not in ("enable", "daemon-reload", "restart"):
        sys.exit("unexpected systemctl command")
elif name == "journalctl":
    print("fake journal")
else:
    sys.exit("unexpected host command: " + name)
"""


@unittest.skipIf(os.geteuid() == 0, "integration seam deliberately rejects root")
class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ollama-install-test-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "root"
        self.repo = self.base / "repo"
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.src = self.repo / "llama/host"
        self.recipes = self.repo / "llama/ollama"
        self.src.mkdir(parents=True)
        self.recipes.mkdir(parents=True)
        (self.src / "ollama.service.d").mkdir()
        for rel in ("install-host-ollama.sh", "ollama-warm.sh", "ollama-warm.service"):
            shutil.copy2(HOST / rel, self.src / rel)
        (self.src / "ollama.service.d/homelab.conf").write_text(
            '[Service]\nEnvironment="OLLAMA_NUM_PARALLEL=4"\n'
        )
        (self.src / "model-posture.json").write_text(
            '{"version":"0.40.11","resident":"gemma4:vlm"}\n'
        )
        (self.src / "ollama_prepare.py").write_text(textwrap.dedent(CORE))
        for module in ("ollama_recovery.py", "ollama_runtime.py"):
            (self.src / module).write_text("# fixture module\n")
        for recipe in (
            "gemma4-vlm",
            "qwen2.5-coder-fim",
            "deepseek-coder-v2-fim",
            "deepseek-coder-v2-instruct",
        ):
            (self.recipes / (recipe + ".Modelfile")).write_text(
                "FROM fixture\nPARAMETER num_ctx 1024\nPARAMETER temperature 0\n"
            )
        (self.recipes / "ollama-exporter.py").write_text("# fixture exporter\n")
        for path in (
            "usr/local/bin",
            "etc/systemd/system",
            "run/lock",
            "usr/share/ollama/.ollama/models",
        ):
            (self.root / path).mkdir(parents=True)
        self.log = self.base / "calls.log"
        self.log.touch()
        (self.root / "online").write_text("yes")
        (self.root / "generation").write_text("1")
        self.env = dict(
            os.environ,
            PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
            REPO_DIR=str(self.repo),
            HOMELAB_INSTALL_TEST_ROOT=str(self.root),
            FAKE_ROOT=str(self.root),
            FAKE_LOG=str(self.log),
        )
        for key in (
            "OLLAMA_VERSION",
            "RESIDENT_TAG",
            "FAIL_CORE",
            "FAKE_STALE_RECEIPT",
            "FAKE_LOCK_BUSY",
            "FAKE_CUTOVER_FAIL",
        ):
            self.env.pop(key, None)
        for command in (
            "hostname",
            "id",
            "ss",
            "iptables-save",
            "curl",
            "k3s",
            "systemctl",
            "journalctl",
            "flock",
            "apt-get",
            "useradd",
            "usermod",
            "tar",
            "zstd",
            "rsync",
            "chown",
            "findmnt",
        ):
            self.write_command(self.bin / command)
        self.write_command(self.root / "usr/local/bin/ollama")

    def write_command(self, path):
        path.write_text("#!" + sys.executable + "\n" + textwrap.dedent(COMMAND))
        path.chmod(0o755)

    def run_installer(self, *args, **env):
        return subprocess.run(
            ["bash", str(self.src / "install-host-ollama.sh"), *args],
            env=dict(self.env, **env),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
        )

    def calls(self):
        return self.log.read_text().splitlines()

    def mutation_calls(self):
        return [
            line
            for line in self.calls()
            if line.startswith(
                (
                    "systemctl stop",
                    "systemctl restart",
                    "systemctl start",
                    "systemctl daemon-reload",
                    "systemctl enable",
                    "useradd",
                    "usermod",
                    "apt-get",
                )
            )
        ]

    def assert_no_adoption(self, result):
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.mutation_calls(), [])
        self.assertFalse((self.root / "opt/ollama-host").exists())

    def installed(self):
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.log.write_text("")

    def test_normal_unreachable_refuses_before_host_mutation(self):
        (self.root / "online").write_text("no")
        result = self.run_installer()
        self.assert_no_adoption(result)
        self.assertIn("--bootstrap-recovery", result.stdout)
        self.assertFalse((self.root / "run/lock/ollama-install.lock").exists())

    def test_prepare_failure_precedes_install_and_restart(self):
        result = self.run_installer(FAIL_CORE="prepare")
        self.assert_no_adoption(result)
        self.assertFalse((self.root / "etc/systemd/system/ollama.service").exists())

    def test_normal_install_waits_for_fresh_receipt_and_installs_every_recipe(self):
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stdout)
        calls = self.calls()
        prepare = next(
            i for i, line in enumerate(calls) if line.startswith("core prepare ")
        )
        restart = calls.index("systemctl restart ollama")
        warm = next(i for i, line in enumerate(calls) if line.startswith("core warm "))
        verify = next(
            i for i, line in enumerate(calls) if line.startswith("core verify ")
        )
        self.assertLess(prepare, restart)
        self.assertLess(restart, warm)
        self.assertLess(warm, verify)
        self.assertLess(verify, calls.index("systemctl restart ollama-exporter"))
        self.assertFalse(any("--no-block" in line for line in calls))
        installed = self.root / "opt/ollama-host"
        self.assertEqual(
            {p.name for p in (installed / "modelfiles").glob("*")},
            {p.name for p in self.recipes.glob("*.Modelfile")},
        )
        for file in (
            "ollama_prepare.py",
            "ollama_runtime.py",
            "ollama_recovery.py",
            "model-posture.json",
        ):
            self.assertEqual(
                (installed / file).read_bytes(), (self.src / file).read_bytes()
            )
        self.assertEqual(
            json.loads(
                (self.root / "var/lib/ollama-preparation/verified.json").read_text()
            )["generation"],
            "2",
        )

    def test_bootstrap_recovery_offline_gets_same_final_verification(self):
        (self.root / "online").write_text("no")
        result = self.run_installer("--bootstrap-recovery")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn(
            "systemctl show ollama --property=LoadState --value", self.calls()
        )
        self.assertNotIn("systemctl stop ollama", self.calls())
        self.assertTrue(any(line.startswith("core warm ") for line in self.calls()))
        self.assertTrue(any(line.startswith("core verify ") for line in self.calls()))

    def test_bootstrap_does_not_bypass_cutover_preflight(self):
        self.assert_no_adoption(
            self.run_installer("--bootstrap-recovery", FAKE_CUTOVER_FAIL="1")
        )

    def test_recovery_stops_existing_units_before_setup(self):
        self.installed()
        result = self.run_installer("--bootstrap-recovery")
        self.assertEqual(result.returncode, 0, result.stdout)
        calls = self.calls()
        self.assertLess(
            calls.index("systemctl stop ollama-warm"),
            calls.index("systemctl stop ollama"),
        )
        self.assertLess(
            calls.index("systemctl stop ollama"), calls.index("systemctl daemon-reload")
        )

    def test_load_acknowledgement_cannot_hide_stale_verification(self):
        result = self.run_installer(FAKE_STALE_RECEIPT="1")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("current model/runtime verification failed", result.stdout)
        self.assertNotIn("systemctl restart ollama-exporter", self.calls())
        self.assertIn("core stop-captured-generation 2", self.calls())
        self.assertEqual((self.root / "online").read_text(), "no")

    def test_warm_failure_is_not_api_readiness_success(self):
        result = self.run_installer(FAIL_CORE="warm")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("model preparation failed", result.stdout)
        self.assertFalse(any(line.startswith("core verify ") for line in self.calls()))

    def test_files_only_requires_complete_migration(self):
        result = self.run_installer("--files-only")
        self.assert_no_adoption(result)
        self.assertIn("full migration", result.stdout)

    def test_files_only_rejects_non_context_recipe_drift_before_copy(self):
        self.installed()
        recipe = self.recipes / "gemma4-vlm.Modelfile"
        original = recipe.read_text()
        recipe.write_text(original.replace("temperature 0", "temperature 1"))
        result = self.run_installer("--files-only")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.mutation_calls(), [])
        self.assertEqual(
            (self.root / "opt/ollama-host/modelfiles" / recipe.name).read_text(),
            original,
        )

    def test_files_only_refuses_units_and_posture_changes(self):
        for relative in (
            "ollama.service.d/homelab.conf",
            "ollama-warm.service",
            "model-posture.json",
        ):
            with self.subTest(relative=relative):
                self.installed()
                path = self.src / relative
                before = path.read_text()
                path.write_text(before + "changed directive\n")
                result = self.run_installer("--files-only")
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertEqual(self.mutation_calls(), [])
                path.write_text(before)

    def test_files_only_unit_comment_change_never_restarts(self):
        self.installed()
        with (self.src / "ollama.service.d/homelab.conf").open("a") as file:
            file.write("# comment-only change\n")
        result = self.run_installer("--files-only")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.mutation_calls(), ["systemctl daemon-reload"])
        self.assertTrue(
            any(line.startswith("core files-check ") for line in self.calls())
        )

    def test_files_only_recipe_comment_requires_new_receipt(self):
        self.installed()
        with (self.recipes / "gemma4-vlm.Modelfile").open("a") as file:
            file.write("# recipe commentary\n")
        result = self.run_installer("--files-only")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.mutation_calls(), [])

    def test_declared_version_cannot_be_overridden(self):
        result = self.run_installer(OLLAMA_VERSION="9.9.9")
        self.assert_no_adoption(result)
        self.assertIn("contradicts model-posture.json", result.stdout)

    def test_competing_install_refuses(self):
        result = self.run_installer(FAKE_LOCK_BUSY="1")
        self.assert_no_adoption(result)
        self.assertIn("another host adoption", result.stdout)

    def test_test_root_and_mode_are_explicit(self):
        result = self.run_installer(HOMELAB_INSTALL_TEST_ROOT="")
        self.assert_no_adoption(result)
        self.assertIn("run with sudo", result.stdout)
        result = self.run_installer("--check", "--bootstrap-recovery")
        self.assertEqual(result.returncode, 2, result.stdout)

    def test_compatibility_wrapper_dispatches_only_known_modes(self):
        for args, command in (
            ([], "warm"),
            (["--reconcile-only"], "prepare"),
            (["--ov-pair-only"], "pair"),
        ):
            with self.subTest(command=command):
                self.log.write_text("")
                result = subprocess.run(
                    ["bash", str(self.src / "ollama-warm.sh"), *args],
                    env=self.env,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(
                    any(
                        line.startswith("core " + command + " ")
                        for line in self.calls()
                    )
                )
        self.log.write_text("")
        result = subprocess.run(
            ["bash", str(self.src / "ollama-warm.sh"), "--other"],
            env=self.env,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.calls(), [])


class PrivilegeTests(unittest.TestCase):
    @unittest.skipUnless(
        os.geteuid() == 0, "root-only negative test; normal suite is unprivileged"
    )
    def test_root_cannot_enable_filesystem_redirect(self):
        result = subprocess.run(
            ["bash", str(HOST / "install-host-ollama.sh")],
            env=dict(os.environ, HOMELAB_INSTALL_TEST_ROOT="/tmp"),
            text=True,
            capture_output=True,
            timeout=5,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("test root is forbidden", result.stderr)


if __name__ == "__main__":
    unittest.main()
