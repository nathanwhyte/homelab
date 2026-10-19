#!/usr/bin/env python3
"""Isolation and cleanup contracts for the disposable integration-test driver."""

import importlib.util
import os
import signal
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "driver", Path(__file__).with_name("check-runner-count-ownership.py")
)
driver = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(driver)


def result(stdout="", stderr="", code=0):
    return subprocess.CompletedProcess([], code, stdout, stderr)


class DriverTests(unittest.TestCase):
    def test_cached_image_never_needs_registry_and_absent_image_is_pulled(self):
        with patch.object(
            driver,
            "docker",
            side_effect=[result(), AssertionError("cached image must not pull")],
        ) as docker:
            driver.ensure_image("unix:///original.sock")
            self.assertEqual(docker.call_count, 1)
        with patch.object(
            driver, "docker", side_effect=[result(code=1), result()]
        ) as docker:
            driver.ensure_image("unix:///original.sock")
            self.assertEqual(
                docker.call_args.args, ("unix:///original.sock", "pull", driver.IMAGE)
            )

    def test_remote_docker_host_refused_without_starting_anything(self):
        with (
            patch.dict(os.environ, {"DOCKER_HOST": "ssh://cluster"}, clear=True),
            patch.object(driver, "run") as run,
        ):
            with self.assertRaisesRegex(RuntimeError, "refusing remote"):
                driver.local_docker_endpoint()
            run.assert_not_called()

    def test_context_override_matches_docker_precedence(self):
        with (
            patch.dict(
                os.environ,
                {"DOCKER_HOST": "ssh://ignored", "DOCKER_CONTEXT": "desktop"},
                clear=True,
            ),
            patch.object(
                driver, "run", return_value=result("unix:///local.sock\n")
            ) as run,
        ):
            self.assertEqual(driver.local_docker_endpoint(), "unix:///local.sock")
            self.assertIn("desktop", run.call_args.args)

    def test_cleanup_refuses_foreign_identity(self):
        with patch.object(driver, "run", return_value=result("another-owner\n")) as run:
            with self.assertRaisesRegex(RuntimeError, "different owner"):
                driver.cleanup(
                    "unix:///original.sock", "arc-ssa-test-example", "our-token"
                )
            self.assertEqual(run.call_count, 1)

    def test_every_docker_command_pins_original_endpoint(self):
        with (
            patch.dict(
                os.environ, {"DOCKER_CONTEXT": "other", "DOCKER_HOST": "ssh://other"}
            ),
            patch.object(driver, "run", return_value=result()) as run,
        ):
            driver.docker("unix:///original.sock", "inspect", "fixture")
        self.assertEqual(
            run.call_args.args[:3], ("docker", "--host", "unix:///original.sock")
        )
        self.assertNotIn("DOCKER_CONTEXT", run.call_args.kwargs["env"])
        self.assertNotIn("DOCKER_HOST", run.call_args.kwargs["env"])

    def test_interrupted_create_reconciles_late_registration_or_keeps_receipt(self):
        for late in (True, False):
            with self.subTest(late=late), tempfile.TemporaryDirectory() as tmp:
                receipt_dir = Path(tmp) / "recovery"
                receipt_dir.mkdir(mode=0o700)
                original_mkdtemp = tempfile.mkdtemp

                def directory(*args, **kwargs):
                    if kwargs.get("prefix") == "arc-ssa-recovery-":
                        return str(receipt_dir)
                    return original_mkdtemp(*args, **kwargs)

                with (
                    patch.object(driver.tempfile, "mkdtemp", side_effect=directory),
                    patch.object(
                        driver,
                        "docker",
                        side_effect=[
                            result(),
                            subprocess.TimeoutExpired("create", 180),
                        ],
                    ),
                    patch.object(
                        driver,
                        "cleanup",
                        side_effect=([False, False, True] if late else [False] * 11),
                    ) as cleanup,
                    patch.object(driver.time, "sleep"),
                ):
                    with self.assertRaises(
                        subprocess.TimeoutExpired if late else RuntimeError
                    ):
                        with driver.disposable_api("unix:///original.sock"):
                            self.fail("create timeout cannot yield an API")
                self.assertEqual(receipt_dir.exists(), not late)
                self.assertEqual(cleanup.call_count, 3 if late else 11)
                if not late:
                    self.assertIn(
                        "unix:///original.sock",
                        (receipt_dir / "container.json").read_text(),
                    )

    def test_timeout_kills_descendants_before_return(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "survivor"
            child = f"import time; from pathlib import Path; time.sleep(1); Path({str(marker)!r}).touch()"
            parent = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child!r}]); time.sleep(30)"
            with self.assertRaises(subprocess.TimeoutExpired):
                driver.run(sys.executable, "-c", parent, timeout=0.3)
            # A separate process waits after run has returned; no surviving
            # grandchild should be able to write its marker.
            subprocess.run(
                [sys.executable, "-c", "import time; time.sleep(1.2)"], check=True
            )
            self.assertFalse(marker.exists())

    def test_signal_during_popen_cannot_escape_process_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "survivor"
            child = f"import time; from pathlib import Path; time.sleep(1); Path({str(marker)!r}).touch()"
            parent = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child!r}]); time.sleep(30)"
            original_popen = subprocess.Popen
            original_handler = signal.signal(signal.SIGTERM, driver.interrupted)
            launched = []

            def launch(*args, **kwargs):
                process = original_popen(*args, **kwargs)
                launched.append(process)
                os.kill(os.getpid(), signal.SIGTERM)
                return process

            try:
                with (
                    patch.object(driver.subprocess, "Popen", side_effect=launch),
                    self.assertRaises(KeyboardInterrupt),
                ):
                    driver.run(sys.executable, "-c", parent)
                subprocess.run(
                    [sys.executable, "-c", "import time; time.sleep(1.2)"], check=True
                )
                self.assertFalse(marker.exists())
                self.assertIsNotNone(launched[0].poll())
            finally:
                signal.signal(signal.SIGTERM, original_handler)
                for process in launched:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()

    def test_success_and_interruption_remove_only_the_owned_container(self):
        for interrupt in (False, True):
            with self.subTest(interrupt=interrupt):
                state = {}

                def command(*args, **kwargs):
                    if args[:2] == ("docker", "--host"):
                        self.assertEqual(args[2], "unix:///original.sock")
                        self.assertNotIn("DOCKER_CONTEXT", kwargs["env"])
                        args = ("docker", *args[3:])
                    if args[:2] in (
                        ("docker", "pull"),
                        ("docker", "start"),
                        ("docker", "image"),
                    ):
                        return result()
                    if args[:2] == ("docker", "create"):
                        state["name"] = args[args.index("--name") + 1]
                        state["token"] = args[args.index("--label") + 1].split("=", 1)[
                            1
                        ]
                        self.assertNotIn("-v", args)
                        self.assertIn("127.0.0.1::6443", args)
                        self.assertIn(driver.IMAGE, args)
                        return result("container-id")
                    if args[:2] == ("docker", "exec"):
                        return result("isolated credential")
                    if args[:2] == ("docker", "port"):
                        return result("127.0.0.1:49152\n")
                    if args[0] == "kubectl":
                        self.assertIn("--kubeconfig", args)
                        self.assertIn("https://127.0.0.1:49152", args)
                        return result("ok")
                    if args[:2] == ("docker", "inspect"):
                        self.assertEqual(args[2], state["name"])
                        return result(state["token"])
                    if args[:2] == ("docker", "rm"):
                        self.assertIn("--volumes", args)
                        self.assertEqual(args[-1], state["name"])
                        state["removed"] = True
                        return result()
                    self.fail(f"unexpected command: {args}")

                with patch.object(driver, "run", side_effect=command):
                    try:
                        with driver.disposable_api("unix:///original.sock") as (
                            config,
                            server,
                        ):
                            self.assertTrue(config.is_file())
                            self.assertEqual(config.stat().st_uid, os.getuid())
                            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
                            self.assertEqual(server, "https://127.0.0.1:49152")
                            if interrupt:
                                raise KeyboardInterrupt
                    except KeyboardInterrupt:
                        self.assertTrue(interrupt)
                self.assertTrue(state["removed"])
                self.assertFalse(config.exists())


if __name__ == "__main__":
    unittest.main()
