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
import signal
import shutil
import subprocess
import tempfile
import textwrap
import unittest

HOST = Path(__file__).resolve().parents[1]
UDEV_RULE = "99-homelab-dgpu-runpm.rules"
UDEV_CALLS = [
    "udevadm control --reload",
    "udevadm trigger --action=add --subsystem-match=pci --attr-match=vendor=0x1002 --attr-match=device=0x7550",
    "udevadm settle",
]

CORE = r"""
import json, os, pathlib, sys
root = pathlib.Path(os.environ["FAKE_ROOT"])
args = sys.argv[1:]
def option(name, default):
    return args[args.index(name) + 1] if name in args else default
commands = ("version", "prepare", "warm", "verify", "render", "files-check", "pair")
command = next(arg for arg in args if arg in commands)
if command in ('prepare', 'files-check') and os.environ.get('EXPECT_INSTALLER_LOCK'):
    if option('--lock-fd', None) != '8':
        sys.exit('installer did not delegate preparation lock')
    actual = os.fstat(8)
    expected = (root / 'var/lib/ollama-preparation/preparation.lock').stat()
    if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
        sys.exit('installer delegated wrong preparation lock')
if command == 'verify' and '--lock-fd' in args:
    sys.exit('installer retained preparation lock across warmup')
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
elif name == 'diff':
    comparison = sum(line.startswith('diff ') for line in pathlib.Path(os.environ['FAKE_LOG']).read_text().splitlines())
    if os.environ.get('FAKE_DIFF_ERROR') == str(comparison):
        sys.stderr.write('injected comparison read failure\n')
        sys.exit(2)
    os.execv(os.environ['REAL_DIFF'], [os.environ['REAL_DIFF'], *args])
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
            if "flock 8\n" in pathlib.Path(os.environ["FAKE_LOG"]).read_text():
                os.fstat(8)  # Once acquired, retain the lock through stopping old warm.
            (root / "warm-active").unlink(missing_ok=True)
        if "ollama" in args:
            (root / "online").write_text("no")
    elif action == "restart" and args[-1] == "ollama":
        try:
            os.fstat(8)
        except OSError:
            pass
        else:
            sys.exit('installer holds preparation lock across daemon restart')
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
elif name == 'udevadm':
    expected = {
        'control': ['control', '--reload'],
        'trigger': ['trigger', '--action=add', '--subsystem-match=pci', '--attr-match=vendor=0x1002', '--attr-match=device=0x7550'],
        'settle': ['settle'],
    }
    if not args or args != expected.get(args[0]):
        sys.exit('unexpected udev command')
    rule = root / 'etc/udev/rules.d/99-homelab-dgpu-runpm.rules'
    directive = 'ACTION=="add|bind", SUBSYSTEM=="pci", ATTR{vendor}=="0x1002", ATTR{device}=="0x7550", ATTR{power/control}="on"'
    if directive not in rule.read_text().splitlines():
        sys.exit('runtime-PM rule must be installed before udev activation')
    if os.environ.get('FAIL_UDEV') == args[0]:
        sys.exit('injected udev ' + args[0] + ' failure')
    if args[0] == 'trigger' and not os.environ.get('FAKE_UDEV_NO_EFFECT'):
        # A boundary fake for this exact selector, not a udev rule interpreter.
        gpu = root / 'sys/bus/pci/devices/0000:03:00.0/power'
        (gpu / 'control').write_text('on\n')
        (gpu / 'runtime_status').write_text('active\n')
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
        for rel in (
            "install-host-ollama.sh",
            "ollama-warm.sh",
            "ollama-warm.service",
            UDEV_RULE,
        ):
            shutil.copy2(HOST / rel, self.src / rel)
        (self.src / "ollama.service.d/homelab.conf").write_text(
            '[Service]\nEnvironment="OLLAMA_NUM_PARALLEL=4"\n'
        )
        (self.src / "model-posture.json").write_text(
            '{"version":"0.40.11","resident":"gemma4:vlm"}\n'
        )
        (self.src / "ollama_prepare.py").write_text(textwrap.dedent(CORE))
        for module in (
            "ollama_recovery.py",
            "ollama_runtime.py",
            "ollama_attestation.py",
            "ollama_signals.py",
        ):
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
            "etc/udev/rules.d",
            "run/lock",
            "usr/share/ollama/.ollama/models",
        ):
            (self.root / path).mkdir(parents=True)
        self.log = self.base / "calls.log"
        self.log.touch()
        (self.root / "online").write_text("yes")
        (self.root / "generation").write_text("1")
        self.dgpu = self.root / "sys/bus/pci/devices/0000:03:00.0/power"
        self.igpu = self.root / "sys/bus/pci/devices/0000:13:00.0/power"
        for gpu in (self.dgpu, self.igpu):
            gpu.mkdir(parents=True)
            (gpu / "control").write_text("auto\n")
            (gpu / "runtime_status").write_text("suspended\n")
        self.env = dict(
            os.environ,
            PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
            REPO_DIR=str(self.repo),
            HOMELAB_INSTALL_TEST_ROOT=str(self.root),
            FAKE_ROOT=str(self.root),
            FAKE_LOG=str(self.log),
            REAL_DIFF=shutil.which("diff"),
        )
        for key in (
            "OLLAMA_VERSION",
            "RESIDENT_TAG",
            "FAIL_CORE",
            "FAKE_STALE_RECEIPT",
            "FAKE_LOCK_BUSY",
            "FAKE_CUTOVER_FAIL",
            "FAKE_DIFF_ERROR",
            "FAIL_UDEV",
            "FAKE_UDEV_NO_EFFECT",
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
            "diff",
            "udevadm",
        ):
            self.write_command(self.bin / command)
        self.write_command(self.root / "usr/local/bin/ollama")

    def write_command(self, path):
        path.write_text("#!" + sys.executable + "\n" + textwrap.dedent(COMMAND))
        path.chmod(0o755)

    def run_installer(self, *args, umask=-1, **env):
        return subprocess.run(
            ["bash", str(self.src / "install-host-ollama.sh"), *args],
            env=dict(self.env, EXPECT_INSTALLER_LOCK="1", **env),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
            umask=umask,
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
                    "udevadm ",
                )
            )
        ]

    def assert_no_adoption(self, result):
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.mutation_calls(), [])
        self.assertFalse((self.root / "opt/ollama-host").exists())
        self.assertFalse((self.root / "etc/udev/rules.d" / UDEV_RULE).exists())
        self.assertEqual((self.dgpu / "control").read_text(), "auto\n")

    def installed(self):
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.log.write_text("")

    def test_interrupted_shell_lock_creation_retries_under_common_umasks(self):
        interrupt = self.base / "interrupt-lock-creation.sh"
        interrupt.write_text(
            "set -T\ntrap 'if [[ -e $KILL_LOCK_PATH ]]; then kill -KILL \"$$\"; fi' DEBUG\n"
        )
        for mask in (0o000, 0o002, 0o022, 0o077):
            for relative in (
                "run/lock/ollama-install.lock",
                "var/lib/ollama-preparation/preparation.lock",
            ):
                with self.subTest(lock=relative, umask=f"{mask:04o}"):
                    lock = self.root / relative
                    lock.unlink(missing_ok=True)
                    result = self.run_installer(
                        umask=mask, BASH_ENV=str(interrupt), KILL_LOCK_PATH=str(lock)
                    )
                    self.assertEqual(result.returncode, -signal.SIGKILL, result.stdout)
                    self.assertEqual(lock.stat().st_mode & 0o777, 0o600)
                    self.assertEqual(lock.read_bytes(), b"")
                    # The next adoption must accept the killed creator's safe
                    # file and retain shared-read access for the recovery reader.
                    result = self.run_installer(umask=mask)
                    self.assertEqual(result.returncode, 0, result.stdout)
                    shared = self.root / "var/lib/ollama-preparation/preparation.lock"
                    self.assertEqual(shared.stat().st_mode & 0o777, 0o644)

    def test_existing_unsafe_shell_locks_are_refused_without_repair(self):
        for relative in (
            "run/lock/ollama-install.lock",
            "var/lib/ollama-preparation/preparation.lock",
        ):
            with self.subTest(lock=relative):
                lock = self.root / relative
                lock.parent.mkdir(parents=True, exist_ok=True)
                lock.write_text("existing lock bytes\n")
                lock.chmod(0o664)
                result = self.run_installer()
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("not group/world-writable", result.stdout)
                self.assertEqual(self.mutation_calls(), [])
                self.assertEqual(lock.read_text(), "existing lock bytes\n")
                self.assertEqual(lock.stat().st_mode & 0o777, 0o664)
                lock.unlink()
                self.log.write_text("")

    def test_shell_locks_do_not_truncate_existing_trusted_files(self):
        locks = [
            self.root / "run/lock/ollama-install.lock",
            self.root / "var/lib/ollama-preparation/preparation.lock",
        ]
        for lock in locks:
            lock.parent.mkdir(parents=True, exist_ok=True)
            lock.write_text("existing lock bytes\n")
            lock.chmod(0o600)
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue(
            all(lock.read_text() == "existing lock bytes\n" for lock in locks)
        )
        self.assertEqual(locks[1].stat().st_mode & 0o777, 0o644)

    def test_unsafe_state_is_refused_without_normalizing_or_replacing_it(self):
        state = self.root / "var/lib/ollama-preparation"
        state.parent.mkdir(parents=True)
        for mode in (0o775, 0o777):
            with self.subTest(mode=f"{mode:04o}"):
                state.mkdir()
                (state / "keep").write_text("operator data")
                state.chmod(mode)
                result = self.run_installer()
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("not group/world-writable", result.stdout)
                self.assertIn("explicitly correct", result.stdout)
                self.assertEqual(state.stat().st_mode & 0o777, mode)
                self.assertEqual((state / "keep").read_text(), "operator data")
                self.assertEqual(self.mutation_calls(), [])
                self.assertFalse((state / "preparation.lock").exists())
                shutil.rmtree(state)
                self.log.write_text("")
        target = self.base / "operator-directory"
        target.mkdir(mode=0o700)
        (target / "keep").write_text("operator data")
        state.symlink_to(target, target_is_directory=True)
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("nonsymlink directory", result.stdout)
        self.assertTrue(state.is_symlink())
        self.assertEqual(target.stat().st_mode & 0o777, 0o700)
        self.assertEqual((target / "keep").read_text(), "operator data")
        self.assertEqual(self.mutation_calls(), [])
        state.unlink()
        state.write_text("operator file")
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("nonsymlink directory", result.stdout)
        self.assertEqual(state.read_text(), "operator file")
        self.assertEqual(self.mutation_calls(), [])

    def test_trusted_private_state_can_become_readable_after_validation(self):
        state = self.root / "var/lib/ollama-preparation"
        state.mkdir(parents=True, mode=0o700)
        inode = state.stat().st_ino
        (state / "keep").write_text("operator data")
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(state.stat().st_mode & 0o777, 0o755)
        self.assertEqual(state.stat().st_ino, inode)
        self.assertEqual((state / "keep").read_text(), "operator data")

    def test_interrupted_state_creation_and_permission_adjustment_retry_safely(self):
        hooks = self.base / "creation-hooks"
        hooks.mkdir()
        (hooks / "sitecustomize.py").write_text(
            textwrap.dedent("""
            import os, pathlib, signal
            state = pathlib.Path(os.environ['INTERRUPT_STATE'])
            boundary = os.environ['INTERRUPT_BOUNDARY']
            mkdir, fchmod = os.mkdir, os.fchmod
            def created(path, *args, **kwargs):
                result = mkdir(path, *args, **kwargs)
                target = state.parent if boundary == 'parent' else state
                if boundary != 'before-chmod' and pathlib.Path(path) == target:
                    os.kill(os.getpid(), signal.SIGKILL)
                return result
            def changed(fd, *args, **kwargs):
                actual = os.fstat(fd)
                if boundary == 'before-chmod' and state.exists():
                    expected = state.stat()
                    if (actual.st_dev, actual.st_ino) == (expected.st_dev, expected.st_ino):
                        os.kill(os.getpid(), signal.SIGKILL)
                return fchmod(fd, *args, **kwargs)
            os.mkdir, os.fchmod = created, changed
        """)
        )
        state = self.root / "var/lib/ollama-preparation"
        for mask in (0o000, 0o002, 0o022, 0o077):
            for boundary in ("parent", "state", "before-chmod"):
                with self.subTest(boundary=boundary, umask=f"{mask:04o}"):
                    shutil.rmtree(self.root / "var", ignore_errors=True)
                    self.log.write_text("")
                    result = self.run_installer(
                        umask=mask,
                        PYTHONPATH=str(hooks),
                        INTERRUPT_STATE=str(state),
                        INTERRUPT_BOUNDARY=boundary,
                    )
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertIn("preparation state was not adopted", result.stdout)
                    target = state.parent if boundary == "parent" else state
                    self.assertTrue(target.is_dir())
                    self.assertEqual(target.stat().st_mode & 0o777, 0o755 & ~mask)
                    self.assertEqual(self.mutation_calls(), [])
                    result = self.run_installer(umask=mask)
                    self.assertEqual(result.returncode, 0, result.stdout)
                    self.assertEqual(state.stat().st_mode & 0o777, 0o755)

    def test_normal_unreachable_refuses_before_host_mutation(self):
        (self.root / "online").write_text("no")
        result = self.run_installer()
        self.assert_no_adoption(result)
        self.assertIn("--bootstrap-recovery", result.stdout)
        self.assertFalse((self.root / "run/lock/ollama-install.lock").exists())

    def test_check_reports_runtime_pm_without_applying_it(self):
        result = self.run_installer("--check")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("control=auto status=suspended (want on / active)", result.stdout)
        self.assertEqual(self.mutation_calls(), [])
        self.assertFalse((self.root / "etc/udev/rules.d" / UDEV_RULE).exists())
        self.assertFalse((self.root / "run/lock/ollama-install.lock").exists())

    def test_udev_failures_abort_before_restart_or_warm(self):
        for failure in ("control", "trigger", "settle", "readback"):
            with self.subTest(failure=failure):
                self.log.write_text("")
                (self.dgpu / "control").write_text("auto\n")
                result = self.run_installer(
                    **(
                        {"FAKE_UDEV_NO_EFFECT": "1"}
                        if failure == "readback"
                        else {"FAIL_UDEV": failure}
                    )
                )
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn(
                    "power/control is not 'on'"
                    if failure == "readback"
                    else "injected udev " + failure + " failure",
                    result.stdout,
                )
                self.assertFalse(
                    any(
                        line.startswith(
                            (
                                "systemctl restart",
                                "systemctl start",
                                "core warm ",
                                "core verify ",
                            )
                        )
                        for line in self.calls()
                    )
                )

    def test_files_only_applies_runtime_pm_without_restart_or_warm(self):
        self.installed()
        (self.dgpu / "control").write_text("auto\n")
        (self.dgpu / "runtime_status").write_text("suspended\n")
        rule = self.root / "etc/udev/rules.d" / UDEV_RULE
        rule.unlink()
        result = self.run_installer("--files-only")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(
            self.mutation_calls(), UDEV_CALLS + ["systemctl daemon-reload"]
        )
        self.assertEqual(rule.read_bytes(), (self.src / UDEV_RULE).read_bytes())
        self.assertIn(str(rule), result.stdout)
        self.assertEqual((self.dgpu / "control").read_text(), "on\n")
        self.assertEqual((self.igpu / "control").read_text(), "auto\n")
        calls = self.calls()
        checked = next(
            i for i, call in enumerate(calls) if call.startswith("core files-check ")
        )
        self.assertLess(checked, calls.index(UDEV_CALLS[0]))
        self.assertFalse(
            any(call.startswith(("core warm ", "core verify ")) for call in calls)
        )

    def test_prepare_failure_precedes_install_and_restart(self):
        result = self.run_installer(FAIL_CORE="prepare")
        self.assert_no_adoption(result)
        self.assertFalse((self.root / "etc/systemd/system/ollama.service").exists())

    def test_files_only_comparison_error_refuses_before_installed_file_changes(self):
        self.installed()
        installed = self.root / "opt/ollama-host/ollama-warm.sh"
        before = installed.read_bytes()
        with (self.src / "ollama-warm.sh").open("a") as script:
            script.write(
                "\n# A harmless script change must still wait for comparison.\n"
            )
        for comparison, label in ((1, "homelab.conf"), (3, "ollama-exporter.service")):
            with self.subTest(unit=label):
                self.log.write_text("")
                result = self.run_installer(
                    "--files-only", FAKE_DIFF_ERROR=str(comparison)
                )
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("comparison failed", result.stdout)
                self.assertIn(label, result.stdout)
                self.assertIn("diff exit 2", result.stdout)
                self.assertIn("injected comparison read failure", result.stdout)
                self.assertEqual(self.mutation_calls(), [])
                self.assertEqual(installed.read_bytes(), before)

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
        self.assertEqual(
            [call for call in calls if call.startswith("udevadm ")], UDEV_CALLS
        )
        self.assertLess(prepare, calls.index(UDEV_CALLS[0]))
        self.assertLess(calls.index(UDEV_CALLS[-1]), restart)
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
            "ollama_attestation.py",
            "ollama_signals.py",
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
        for name in ("ollama_attestation.py", "ollama_recovery.py"):
            self.assertEqual(
                (self.root / "opt/ollama-exporter" / name).read_bytes(),
                (self.src / name).read_bytes(),
            )
        unit = (self.root / "etc/systemd/system/ollama-exporter.service").read_text()
        self.assertIn("--preparation-state /var/lib/ollama-preparation", unit)
        self.assertEqual(
            (self.root / "etc/udev/rules.d" / UDEV_RULE).read_bytes(),
            (self.src / UDEV_RULE).read_bytes(),
        )
        self.assertEqual((self.dgpu / "control").read_text(), "on\n")
        self.assertEqual((self.igpu / "control").read_text(), "auto\n")

    def test_recovery_stops_old_producer_before_acquiring_its_lock(self):
        self.installed()
        result = self.run_installer("--bootstrap-recovery")
        self.assertEqual(result.returncode, 0, result.stdout)
        calls = self.calls()
        self.assertLess(
            calls.index("systemctl stop ollama-warm"), calls.index("flock 8")
        )
        self.assertLess(calls.index("systemctl stop ollama"), calls.index("flock 8"))

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
        calls = self.calls()
        self.assertEqual(
            [call for call in calls if call.startswith("udevadm ")], UDEV_CALLS
        )
        self.assertLess(
            calls.index(UDEV_CALLS[-1]), calls.index("systemctl restart ollama")
        )
        self.assertEqual((self.dgpu / "control").read_text(), "on\n")

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
        with (self.src / "ollama-warm.service").open("a") as file:
            file.write("# comment-only change\n")
        result = self.run_installer("--files-only")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(
            self.mutation_calls(), UDEV_CALLS + ["systemctl daemon-reload"]
        )
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

    def test_installer_real_render_and_files_check_ignore_ambient_resident(self):
        self.installed()
        installed = self.root / "opt/ollama-host"
        for directory in (self.src, installed):
            (directory / "model-posture.json").write_bytes(
                (HOST / "model-posture.json").read_bytes()
            )
        # Execute the production CLI/profile/render/files-check implementation.
        # Its only API operation is a pinned-version fake; no model or network
        # adapter is available, so accidental compilation cannot pass this test.
        (self.src / "ollama_prepare.py").write_text(
            textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {str(HOST)!r})
            import ollama_prepare as implementation
            class VersionOnly:
                def __init__(self, url): self.url = url
                def call(self, path, **kwargs):
                    assert path == '/api/version', path
                    return {{'version': '0.40.11'}}
            implementation.Http = VersionOnly
            implementation.main()
        """)
        )
        rendered = subprocess.run(
            [
                sys.executable,
                str(self.src / "ollama_prepare.py"),
                "--config",
                str(self.src / "model-posture.json"),
                "--recipes",
                str(self.recipes),
                "render",
                "--root",
                str(self.repo),
            ],
            env=dict(self.env, RESIDENT_TAG="qwen2.5-coder:fim"),
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        dropin = self.src / "ollama.service.d/homelab.conf"
        self.assertIn("OLLAMA_MAX_LOADED_MODELS=1", dropin.read_text())
        shutil.copy2(
            dropin, self.root / "etc/systemd/system/ollama.service.d/homelab.conf"
        )
        for ambient in ("qwen2.5-coder:fim", "not-a-profile"):
            with self.subTest(ambient=ambient):
                self.log.write_text("")
                result = self.run_installer("--files-only", RESIDENT_TAG=ambient)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertEqual(
                    self.mutation_calls(), UDEV_CALLS + ["systemctl daemon-reload"]
                )
                self.assertEqual(
                    json.loads((installed / "model-posture.json").read_text())[
                        "resident"
                    ],
                    "gemma4:vlm",
                )

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
