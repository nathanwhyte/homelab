#!/usr/bin/env python3
"""Hermetic version-selection and script dry-run safety tests."""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "helm_deploy", ROOT / "scripts/helm-deploy.py"
)
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


class VersionTests(unittest.TestCase):
    def rows(self, chart="my-chart-1.2.3-rc.1+build.2", status="deployed"):
        return json.dumps(
            [dict(name="release", namespace="ns", chart=chart, status=status)]
        )

    def test_prerelease_and_build_suffix_survive(self):
        with patch.object(deploy, "output", return_value=self.rows()):
            self.assertEqual(
                deploy.installed_version("release", "ns", "my-chart"),
                "1.2.3-rc.1+build.2",
            )

    def test_v_prefix_survives(self):
        with patch.object(deploy, "output", return_value=self.rows("my-chart-v26.3.3")):
            self.assertEqual(
                deploy.installed_version("release", "ns", "my-chart"), "v26.3.3"
            )

    def test_only_empty_array_is_first_install(self):
        with patch.object(deploy, "output", return_value="[]"):
            self.assertIsNone(deploy.installed_version("release", "ns", "my-chart"))
        for result in (
            "",
            "null",
            "{}",
            "[{}]",
            self.rows(status="failed"),
            self.rows(status="pending-upgrade"),
            self.rows("different-1.2.3"),
        ):
            with (
                self.subTest(result=result),
                patch.object(deploy, "output", return_value=result),
            ):
                with self.assertRaises(ValueError):
                    deploy.installed_version("release", "ns", "my-chart")

    def test_lookup_error_never_runs_upgrade(self):
        with (
            patch.object(
                deploy,
                "output",
                side_effect=subprocess.CalledProcessError(1, "helm list"),
            ),
            patch.object(deploy.subprocess, "run") as run,
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                deploy.main(["release", "ns", "repo/my-chart"])
            run.assert_not_called()

    def test_local_mismatch_never_runs_upgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(
                    deploy,
                    "output",
                    side_effect=["name: my-chart\nversion: 9.0.0\n", self.rows()],
                ),
                patch.object(deploy.subprocess, "run") as run,
            ):
                with self.assertRaisesRegex(ValueError, "version mismatch"):
                    deploy.main(["release", "ns", directory])
                run.assert_not_called()

    def test_resolved_remote_mismatch_never_runs_upgrade(self):
        with (
            patch.object(
                deploy,
                "output",
                side_effect=[self.rows(), "name: my-chart\nversion: 9.0.0\n"],
            ),
            patch.object(deploy.subprocess, "run") as run,
        ):
            with self.assertRaisesRegex(ValueError, "version mismatch"):
                deploy.main(["release", "ns", "repo/my-chart"])
            run.assert_not_called()

    def test_first_install_resolves_once_then_pins(self):
        with (
            patch.object(
                deploy,
                "output",
                side_effect=["[]", "name: my-chart\nversion: '1.2.3'\n"],
            ),
            patch.object(deploy.subprocess, "run") as run,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            deploy.main(
                ["release", "ns", "repo/my-chart", "--dry-run", "-f", "values.yaml"]
            )
            argv = run.call_args.args[0]
            self.assertEqual(argv[argv.index("--version") + 1], "1.2.3")
            self.assertIn("--dry-run=server", argv)
            self.assertIn("--hide-secret", argv)
            self.assertEqual(run.call_args.kwargs["stdout"], subprocess.DEVNULL)

    def test_no_competing_flags(self):
        for flag in (
            "--version=9.9.9",
            "--namespace=other",
            "--dry-run=none",
            "--kube-context=other",
        ):
            with self.subTest(flag=flag), patch.object(deploy, "output") as output:
                with self.assertRaises(ValueError):
                    deploy.main(["release", "ns", "repo/my-chart", "--dry-run", flag])
                output.assert_not_called()

    def test_diff_is_pinned_and_suppresses_secrets(self):
        with (
            patch.object(
                deploy,
                "output",
                side_effect=[
                    self.rows("my-chart-1.2.3"),
                    "name: my-chart\nversion: 1.2.3\n",
                ],
            ),
            patch.object(deploy.subprocess, "run") as run,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            deploy.main(["release", "ns", "repo/my-chart", "--diff"])
            argv = run.call_args.args[0]
            self.assertEqual(argv[:3], ["helm", "diff", "upgrade"])
            self.assertIn("--version", argv)
            self.assertIn("--suppress-secrets", argv)


FAKE_HELM = r"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ['TEST_LOG'], 'a') as log:
    log.write(json.dumps(args)+'\n')
versions = {'kube-prometheus-stack': '87.17.0', 'k8s-monitoring': '3.8.4', 'loki': '7.1.0', 'harbor': '1.19.1', 'open-webui': '15.2.0', 'kubernetes-dashboard': '7.14.0', 'garage': '0.9.2', 'headlamp': '0.44.0'}
if args[0] == 'list':
    release = args[args.index('--filter')+1].strip('^$').replace('\\-', '-')
    print('[]' if release == 'headlamp' else json.dumps([dict(name=release, namespace=args[args.index('--namespace')+1], status='deployed', chart=release+'-'+versions[release])]))
elif args[:2] == ['show', 'chart']:
    name = Path(args[2]).name
    print('name: '+name+'\nversion: '+versions[name])
elif args[0] == 'upgrade':
    assert '--dry-run=server' in args and '--hide-secret' in args, args
    release = args[2]
    assert args[args.index('--version')+1] == versions[release], args
else:
    sys.exit('unexpected Helm command: '+str(args))
"""


class ScriptDryRunTests(unittest.TestCase):
    def test_all_six_scripts_from_unrelated_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            binary = directory / "bin"
            binary.mkdir()
            (binary / "helm").write_text(FAKE_HELM)
            (binary / "helm").chmod(0o755)
            (binary / "kubectl").write_text(
                "#!/bin/sh\necho 'kubectl must not run during Helm dry-run' >&2\nexit 99\n"
            )
            (binary / "kubectl").chmod(0o755)
            chart = directory / "garage"
            chart.mkdir()
            environment = dict(
                os.environ,
                PATH=str(binary) + os.pathsep + os.environ["PATH"],
                TEST_LOG=str(directory / "commands.jsonl"),
                GARAGE_CHART_DIR=str(chart),
            )
            scripts = (
                "grafana/deploy-grafana.sh",
                "harbor/deploy-harbor.sh",
                "openwebui/deploy-openwebui.sh",
                "dashboard/deploy-dashboard.sh",
                "headlamp/deploy-headlamp.sh",
                "garage/deploy-garage.sh",
            )
            for script in scripts:
                with self.subTest(script=script):
                    result = subprocess.run(
                        ["/bin/bash", str(ROOT / script), "--dry-run"],
                        cwd=directory,
                        env=environment,
                        text=True,
                        capture_output=True,
                    )
                    self.assertEqual(
                        result.returncode, 0, result.stdout + result.stderr
                    )
            commands = [
                json.loads(line)
                for line in (directory / "commands.jsonl").read_text().splitlines()
            ]
            self.assertEqual(sum(c[0] == "upgrade" for c in commands), 8)


if __name__ == "__main__":
    unittest.main()
