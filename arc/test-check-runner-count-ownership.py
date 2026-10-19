#!/usr/bin/env python3
"""Isolation and cleanup contracts for the disposable integration-test driver."""

import importlib.util
import os
from pathlib import Path
import subprocess
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
                driver.cleanup("arc-ssa-test-example", "our-token")
            self.assertEqual(run.call_count, 1)

    def test_failed_start_checks_for_its_own_orphan(self):
        with patch.object(
            driver,
            "run",
            side_effect=[
                RuntimeError("launch failed"),
                result(stderr="No such object: absent", code=1),
            ],
        ) as run:
            with self.assertRaisesRegex(RuntimeError, "launch failed"):
                with driver.disposable_api():
                    self.fail("launch failure must not yield an API")
            self.assertEqual(run.call_args.args[:2], ("docker", "inspect"))

    def test_success_and_interruption_remove_only_the_owned_container(self):
        for interrupt in (False, True):
            with self.subTest(interrupt=interrupt):
                state = {}

                def command(*args, **kwargs):
                    if args[:2] == ("docker", "run"):
                        state["name"] = args[args.index("--name") + 1]
                        state["token"] = args[args.index("--label") + 1].split("=", 1)[
                            1
                        ]
                        output = Path(
                            args[args.index("-v") + 1].removesuffix(":/output")
                        )
                        (output / "kubeconfig").write_text("isolated credential")
                        self.assertIn("127.0.0.1::6443", args)
                        self.assertIn(driver.IMAGE, args)
                        return result("container-id")
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
                        self.assertEqual(args[-1], state["name"])
                        state["removed"] = True
                        return result()
                    self.fail(f"unexpected command: {args}")

                with patch.object(driver, "run", side_effect=command):
                    try:
                        with driver.disposable_api() as (config, server):
                            self.assertTrue(config.is_file())
                            self.assertEqual(server, "https://127.0.0.1:49152")
                            if interrupt:
                                raise KeyboardInterrupt
                    except KeyboardInterrupt:
                        self.assertTrue(interrupt)
                self.assertTrue(state["removed"])
                self.assertFalse(config.exists())


if __name__ == "__main__":
    unittest.main()
