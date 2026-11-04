#!/usr/bin/env python3
"""Hermetic version-selection and script dry-run safety tests."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_scripts" / "lib"))
from git_env import scrub_environ  # noqa: E402

scrub_environ()

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "helm_deploy", ROOT / "scripts/helm-deploy.py"
)
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)
roll_spec = importlib.util.spec_from_file_location(
    "sts_partition_roll", ROOT / "scripts/sts-partition-roll.py"
)
roll = importlib.util.module_from_spec(roll_spec)
roll_spec.loader.exec_module(roll)


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


class ChartLocalityTests(unittest.TestCase):
    """A chart ref is local only when it says so explicitly (absolute, or
    `./`/`../`-prefixed); everything else is remote regardless of what
    happens to exist on disk at that name."""

    def rows(self, chart="my-chart-1.2.3-rc.1+build.2", status="deployed"):
        return json.dumps(
            [dict(name="release", namespace="ns", chart=chart, status=status)]
        )

    def test_absolute_path_is_local(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertTrue(deploy.is_local_chart_ref(directory))
            with (
                patch.object(
                    deploy,
                    "output",
                    side_effect=["name: my-chart\nversion: 1.2.3\n", "[]"],
                ),
                patch.object(deploy.subprocess, "run") as run,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                deploy.main(["release", "ns", directory, "--dry-run"])
                argv = run.call_args.args[0]
                self.assertIn(directory, argv)
                self.assertEqual(argv[argv.index("--version") + 1], "1.2.3")

    def test_dot_slash_relative_path_is_local(self):
        self.assertTrue(deploy.is_local_chart_ref("./mychart"))
        self.assertTrue(deploy.is_local_chart_ref("../mychart"))
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "mychart").mkdir()
            cwd = os.getcwd()
            os.chdir(directory)
            try:
                with (
                    patch.object(
                        deploy,
                        "output",
                        side_effect=["name: mychart\nversion: 1.2.3\n", "[]"],
                    ),
                    patch.object(deploy.subprocess, "run") as run,
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    deploy.main(["release", "ns", "./mychart", "--dry-run"])
                    argv = run.call_args.args[0]
                    self.assertIn("./mychart", argv)
            finally:
                os.chdir(cwd)

    def test_missing_local_path_errors_before_any_helm_call(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = str(Path(directory) / "does-not-exist")
            with (
                patch.object(deploy, "output") as output,
                patch.object(deploy.subprocess, "run") as run,
            ):
                with self.assertRaisesRegex(ValueError, "does not exist"):
                    deploy.main(["release", "ns", missing])
                output.assert_not_called()
                run.assert_not_called()

    def test_remote_looking_ref_that_exists_as_a_directory_stays_remote(self):
        self.assertFalse(deploy.is_local_chart_ref("repo/my-chart"))
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "repo" / "my-chart").mkdir(parents=True)
            cwd = os.getcwd()
            os.chdir(directory)
            try:
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
                    deploy.main(["release", "ns", "repo/my-chart"])
                    argv = run.call_args.args[0]
                    self.assertIn("repo/my-chart", argv)
            finally:
                os.chdir(cwd)


FAKE_HELM = r"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ['TEST_LOG'], 'a') as log:
    log.write(json.dumps(args)+'\n')
versions = {'kube-prometheus-stack': '87.17.0', 'k8s-monitoring': '3.8.4', 'loki': '7.1.0', 'harbor': '1.19.1', 'open-webui': '15.2.0', 'kubernetes-dashboard': '7.14.0', 'garage': '0.9.2', 'longhorn': '1.12.0', 'gpu-operator': 'v26.3.3'}
# Release name is not always the chart name: the NVIDIA release carries a
# generated suffix (IMPR-1148), and helm-deploy.py matches on chart identity.
charts = {'gpu-operator-1774050554': 'gpu-operator'}
if args[0] == 'list':
    release = args[args.index('--filter')+1].strip('^$').replace('\\-', '-')
    chart = charts.get(release, release)
    print(json.dumps([dict(name=release, namespace=args[args.index('--namespace')+1], status='deployed', chart=chart+'-'+versions[chart])]))
elif args[:2] == ['show', 'chart']:
    name = Path(args[2]).name
    print('name: '+name+'\nversion: '+versions[name])
elif args[0] == 'upgrade':
    assert '--dry-run=server' in args and '--hide-secret' in args, args
    release = args[2]
    assert args[args.index('--version')+1] == versions[charts.get(release, release)], args
else:
    sys.exit('unexpected Helm command: '+str(args))
"""


class ScriptDryRunTests(unittest.TestCase):
    def test_all_seven_scripts_from_unrelated_directory(self):
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
                "garage/deploy-garage.sh",
                "longhorn/deploy-longhorn.sh",
                "gpu/nvidia/deploy-nvidia-gpu.sh",
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
            # grafana runs three Helm upgrades (kube-prometheus-stack, k8s-monitoring,
            # loki); the other six scripts run one each.
            self.assertEqual(sum(c[0] == "upgrade" for c in commands), 9)


class UpgradeChartTests(unittest.TestCase):
    """--upgrade-chart: the only way past a chart version change."""

    def rows(self):
        return json.dumps(
            [
                dict(
                    name="release",
                    namespace="ns",
                    chart="my-chart-1.0.0",
                    status="deployed",
                )
            ]
        )

    def run_main(self, flags, outputs, directory, run_effect=None):
        self.runs_before_hold_check = None

        def hold_check(*args):
            self.runs_before_hold_check = len(run.call_args_list)

        with (
            patch.object(deploy, "output", side_effect=outputs),
            patch.object(deploy.subprocess, "run", side_effect=run_effect) as run,
            patch.object(deploy, "check_rendered_hold", side_effect=hold_check),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            try:
                deploy.main(["release", "ns", directory] + flags)
            finally:
                self.commands = [call.args[0] for call in run.call_args_list]
        return self.commands, stdout.getvalue()

    def test_hold_check_runs_before_any_other_command(self):
        with tempfile.TemporaryDirectory() as directory:
            self.run_main(
                ["--upgrade-chart", "--hold-statefulset", "sts"],
                ["name: my-chart\nversion: 2.0.0\n", self.rows(), "3", "3"],
                directory,
            )
        self.assertEqual(self.runs_before_hold_check, 0)

    def test_diff_mode_runs_both_preflights(self):
        with tempfile.TemporaryDirectory() as directory:
            commands, stdout = self.run_main(
                ["--upgrade-chart", "--hold-statefulset", "sts", "--diff"],
                ["name: my-chart\nversion: 2.0.0\n", self.rows(), "3"],
                directory,
            )
        self.assertIn("--dry-run=server", commands[0])
        self.assertEqual(commands[1][:3], ["helm", "diff", "upgrade"])
        self.assertEqual(len(commands), 2)
        self.assertIn("nothing was changed", stdout)

    def test_failed_upgrade_reports_the_partition(self):
        def effect(command, **kwargs):
            if command[:2] == ["helm", "upgrade"] and "--dry-run=server" not in command:
                raise subprocess.CalledProcessError(1, command)

        with tempfile.TemporaryDirectory() as directory:
            with (
                contextlib.redirect_stderr(io.StringIO()) as stderr,
                self.assertRaises(subprocess.CalledProcessError),
            ):
                self.run_main(
                    ["--upgrade-chart", "--hold-statefulset", "sts"],
                    ["name: my-chart\nversion: 2.0.0\n", self.rows(), "3", "3"],
                    directory,
                    run_effect=effect,
                )
        self.assertIn("helm upgrade failed", stderr.getvalue())
        self.assertIn("partition is 3 (hold 3)", stderr.getvalue())

    def test_without_flag_a_version_change_is_still_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(
                    deploy,
                    "output",
                    side_effect=["name: my-chart\nversion: 2.0.0\n", self.rows()],
                ),
                patch.object(deploy.subprocess, "run") as run,
            ):
                with self.assertRaisesRegex(
                    ValueError, "Chart version mismatch: deployed 1.0.0, supplied 2.0.0"
                ):
                    deploy.main(["release", "ns", directory])
                run.assert_not_called()

    def test_flags_must_come_as_a_pair_and_need_a_local_deployed_chart(self):
        with tempfile.TemporaryDirectory() as directory:
            for argv in (
                [directory, "--upgrade-chart"],
                [directory, "--hold-statefulset", "sts"],
                ["repo/my-chart", "--upgrade-chart", "--hold-statefulset", "sts"],
            ):
                with self.subTest(argv=argv), patch.object(deploy, "output") as out:
                    with self.assertRaises(ValueError):
                        deploy.main(["release", "ns"] + argv)
                    out.assert_not_called()
            with patch.object(
                deploy,
                "output",
                side_effect=["name: my-chart\nversion: 2.0.0\n", "[]"],
            ):
                with self.assertRaisesRegex(ValueError, "nothing to upgrade"):
                    deploy.main(
                        ["release", "ns", directory, "--upgrade-chart"]
                        + ["--hold-statefulset", "sts"]
                    )

    def test_dry_run_issues_no_mutating_command(self):
        with tempfile.TemporaryDirectory() as directory:
            commands, stdout = self.run_main(
                ["--upgrade-chart", "--hold-statefulset=sts", "--dry-run"],
                ["name: my-chart\nversion: 2.0.0\n", self.rows(), "3"],
                directory,
            )
        self.assertEqual(
            [c[:3] for c in commands],
            [["helm", "upgrade", "--install"], ["helm", "diff", "upgrade"]],
        )
        self.assertIn("--dry-run=server", commands[0])
        self.assertIn("--suppress-secrets", commands[1])
        for command in commands:
            self.assertNotIn("patch", command)
            self.assertNotEqual(command[0], "kubectl")
        self.assertIn("hold at 3", stdout)
        self.assertIn("release 2, 1, 0", stdout)
        self.assertIn("nothing was changed", stdout)

    def test_upgrade_preflights_then_holds_then_upgrades(self):
        with tempfile.TemporaryDirectory() as directory:
            commands, _ = self.run_main(
                ["--upgrade-chart", "--hold-statefulset", "sts", "-f", "v.yaml"],
                ["name: my-chart\nversion: 2.0.0\n", self.rows(), "3", "3"],
                directory,
            )
        kinds = [
            "patch"
            if "patch" in c
            else "dry"
            if "--dry-run=server" in c
            else "diff"
            if c[1] == "diff"
            else "upgrade"
            for c in commands
        ]
        self.assertEqual(kinds, ["dry", "diff", "patch", "upgrade"])
        self.assertIn('"partition": 3', commands[2][-1])
        self.assertEqual(commands[3][commands[3].index("--version") + 1], "2.0.0")
        self.assertIn("v.yaml", commands[3])

    def test_lost_hold_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "replaced the partition hold"):
                self.run_main(
                    ["--upgrade-chart", "--hold-statefulset", "sts"],
                    ["name: my-chart\nversion: 2.0.0\n", self.rows(), "3", "0"],
                    directory,
                )


class RenderedHoldTests(unittest.TestCase):
    """check_rendered_hold reads the chart's own render, parsed by kubectl."""

    def check(self, *objects):
        converted = "\n".join(json.dumps(o, indent=4) for o in objects)
        with (
            patch.object(deploy, "output", return_value="rendered: yaml") as out,
            patch.object(
                deploy.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, stdout=converted),
            ) as run,
        ):
            deploy.check_rendered_hold("r", "./c", "ns", "1.0.0", ["-f", "v"], "sts")
        self.assertEqual(out.call_args.args[0][:2], ["helm", "template"])
        self.assertEqual(run.call_args.kwargs["input"], "rendered: yaml")
        self.assertIn("--dry-run=client", run.call_args.args[0])

    def sts(self, strategy=None, name="sts"):
        spec = {} if strategy is None else {"updateStrategy": strategy}
        return {"kind": "StatefulSet", "metadata": {"name": name}, "spec": spec}

    def test_no_strategy_or_plain_rolling_update_passes(self):
        secret = {"kind": "Secret", "metadata": {"name": "s"}}
        self.check(secret, self.sts())
        self.check(self.sts({"type": "RollingUpdate"}))

    def test_rendered_partition_or_on_delete_is_refused(self):
        for strategy in (
            {"type": "RollingUpdate", "rollingUpdate": {"partition": 0}},
            {"rollingUpdate": {"partition": 3}},
            {"type": "OnDelete"},
        ):
            with self.subTest(strategy=strategy):
                with self.assertRaisesRegex(ValueError, "replace the partition hold"):
                    self.check(self.sts(strategy))

    def test_missing_or_duplicate_statefulset_is_refused(self):
        with self.assertRaisesRegex(ValueError, "renders 0 StatefulSet"):
            self.check(self.sts(name="other"))
        with self.assertRaisesRegex(ValueError, "renders 2 StatefulSet"):
            self.check(self.sts(), self.sts())


class FakeCluster:
    """A 3-pod StatefulSet. Lowering the partition makes the released pod linger
    (old UID, still Ready) for `linger` polls, then come back with a new UID."""

    def __init__(
        self, replicas=3, linger=3, healthy_after=0, stuck=None, observed_after=0
    ):
        self.replicas, self.linger, self.stuck = replicas, linger, stuck
        self.partition, self.tick, self.events = replicas, 0, []
        self.pods = {
            n: dict(uid=f"old-{n}", hash="rev-1", ready_at=0) for n in range(replicas)
        }
        # Until this tick the controller has not observed generation 2, and its
        # status still names the old revision as the update revision.
        self.observed_after = observed_after
        self.replace_at = {}
        self.healthy_after = healthy_after
        self.gate_calls = 0

    def advance(self, seconds):
        self.tick += 1
        for ordinal, due in list(self.replace_at.items()):
            if self.tick >= due and ordinal != self.stuck:
                self.pods[ordinal] = dict(
                    uid=f"new-{ordinal}", hash="rev-2", ready_at=self.tick + 1
                )
                self.events.append(f"replaced {ordinal}")
                del self.replace_at[ordinal]

    def kube(self, args, timeout=None):
        if args[:2] == ["get", "statefulset"]:
            observed = self.tick >= self.observed_after
            return json.dumps(
                dict(
                    metadata=dict(generation=2),
                    spec=dict(
                        replicas=self.replicas,
                        updateStrategy=dict(
                            rollingUpdate=dict(partition=self.partition)
                        ),
                    ),
                    status=dict(
                        observedGeneration=2 if observed else 1,
                        updateRevision="rev-2" if observed else "rev-1",
                    ),
                )
            )
        if args[:2] == ["get", "pod"]:
            pod = self.pods[int(args[2].rsplit("-", 1)[1])]
            ready = self.tick >= pod["ready_at"]
            return json.dumps(
                dict(
                    metadata=dict(
                        uid=pod["uid"], labels={"controller-revision-hash": pod["hash"]}
                    ),
                    status=dict(
                        conditions=[
                            dict(type="Ready", status="True" if ready else "False")
                        ]
                    ),
                )
            )
        if args[0] == "patch":
            self.partition = json.loads(args[-1])["spec"]["updateStrategy"][
                "rollingUpdate"
            ]["partition"]
            self.events.append(f"patch {self.partition}")
            self.replace_at[self.partition] = self.tick + self.linger
            return ""
        if args[0] == "exec":
            self.gate_calls += 1
            nodes = 3 if self.gate_calls > self.healthy_after else 2
            return (
                "==== HEALTHY NODES ====\nID Hostname\n"
                + "n h\n" * nodes
                + "\n==== FAILED ====\nx\n"
            )
        raise AssertionError(args)

    def roller(self, timeout=100, gate=True):
        return roll.Roller(
            self.kube,
            "garage",
            ("garage", ["/garage", "status"], 3) if gate else None,
            timeout=timeout,
            interval=1,
            sleep=self.advance,
            clock=lambda: self.tick,
            log=lambda message: None,
        )


class RollTests(unittest.TestCase):
    def test_old_ready_pod_does_not_end_the_wait_early(self):
        cluster = FakeCluster(linger=3)
        cluster.roller().run()
        # Each release happens only after the previous ordinal was replaced.
        self.assertEqual(
            cluster.events,
            ["patch 2", "replaced 2", "patch 1", "replaced 1", "patch 0", "replaced 0"],
        )
        self.assertEqual(cluster.partition, 0)

    def test_health_gate_blocks_the_next_release(self):
        cluster = FakeCluster(linger=1, healthy_after=7)
        cluster.roller().run()
        self.assertGreater(cluster.gate_calls, 3)
        self.assertEqual(cluster.events[0], "patch 2")
        self.assertEqual(cluster.events[-1], "replaced 0")

    def test_lagging_controller_status_does_not_pass_the_old_pod(self):
        # A stale status names rev-1, the old pod's revision; it must not pass.
        cluster = FakeCluster(linger=2, observed_after=4)
        cluster.roller().run()
        self.assertEqual(cluster.events[:2], ["patch 2", "replaced 2"])
        self.assertEqual(cluster.partition, 0)

    def test_pods_already_on_the_update_revision_pass_without_replacement(self):
        cluster = FakeCluster(linger=1000)
        for ordinal in range(3):
            cluster.pods[ordinal]["hash"] = "rev-2"
        cluster.roller(timeout=5).run()
        self.assertEqual(cluster.events, ["patch 2", "patch 1", "patch 0"])

    def test_resume_at_partition_zero_verifies_ordinal_zero(self):
        cluster = FakeCluster()
        cluster.partition = 0
        for ordinal in (1, 2):
            cluster.pods[ordinal] = dict(uid=f"new-{ordinal}", hash="rev-2", ready_at=0)
        with self.assertRaisesRegex(roll.RollTimeout, "garage-0.*revision rev-2"):
            cluster.roller(timeout=5).run()
        self.assertEqual(cluster.events, [])

    def test_hung_kubectl_is_a_timeout(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, directory, True)
        kubectl = directory / "kubectl"
        kubectl.write_text(STUB_HUNG_GATE_KUBECTL)
        kubectl.chmod(0o755)
        path = os.environ["PATH"]
        os.environ["PATH"] = f"{directory}{os.pathsep}{path}"
        self.addCleanup(os.environ.__setitem__, "PATH", path)
        started = time.monotonic()
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            code = roll.main(
                ["garage", "garage", "--timeout", "2", "--gate-container", "garage"]
                + ["--gate-command", "/garage status", "--expect-nodes", "3"]
            )
        self.assertEqual(code, 1)
        self.assertLess(time.monotonic() - started, 15)
        self.assertIn("Resume with", stderr.getvalue())

    def test_timeout_stops_and_leaves_the_partition(self):
        cluster = FakeCluster(stuck=2)
        with self.assertRaisesRegex(roll.RollTimeout, "garage-2.*revision rev-2"):
            cluster.roller(timeout=5).run()
        self.assertEqual(cluster.events, ["patch 2"])
        self.assertEqual(cluster.partition, 2)

    def test_resume_reverifies_the_released_ordinal_then_continues(self):
        cluster = FakeCluster(linger=1)
        cluster.partition = 2
        cluster.pods[2] = dict(uid="new-2", hash="rev-2", ready_at=0)
        cluster.roller().run()
        self.assertEqual(cluster.events[0], "patch 1")
        self.assertEqual(cluster.partition, 0)

    def test_dry_run_changes_nothing(self):
        cluster = FakeCluster()
        cluster.roller().run(dry_run=True)
        self.assertEqual(cluster.events, [])

    def test_healthy_nodes_counts_only_that_section(self):
        text = "==== HEALTHY NODES ====\nID Host\na b\nc d\n\n==== FAILED NODES ====\nID\nx\n"
        self.assertEqual(roll.healthy_nodes(text), 2)
        self.assertEqual(roll.healthy_nodes("==== FAILED NODES ====\nID\nx\n"), 0)


STUB_HUNG_GATE_KUBECTL = r"""#!/usr/bin/env python3
import json, sys, time
args = sys.argv[3:]  # after `-n <namespace>`
if args[:2] == ['get', 'statefulset']:
    print(json.dumps(dict(
        metadata=dict(generation=1),
        spec=dict(replicas=3, updateStrategy=dict(rollingUpdate=dict(partition=1))),
        status=dict(observedGeneration=1, updateRevision='rev-2'))))
elif args[:2] == ['get', 'pod']:
    print(json.dumps(dict(
        metadata=dict(uid='u', labels={'controller-revision-hash': 'rev-2'}),
        status=dict(conditions=[dict(type='Ready', status='True')]))))
elif args[0] == 'exec':
    time.sleep(60)
else:
    sys.exit('unexpected kubectl command: ' + str(args))
"""

STUB_GIT = r"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ['TEST_LOG'], 'a') as log:
    log.write(json.dumps(['git'] + args) + '\n')
if args[0] == 'clone':
    chart = Path(args[-1]) / 'script/helm/garage'
    chart.mkdir(parents=True)
    Path(args[-1], 'TAG').write_text(args[args.index('--branch') + 1])
elif args[:1] == ['-C'] and args[2] == 'describe':
    tag = Path(args[1], 'TAG')
    if not tag.exists():
        sys.exit(128)
    print(tag.read_text())
else:
    sys.exit('unexpected git command: ' + str(args))
"""

STUB_UPGRADE_HELM = r"""#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ['TEST_LOG'], 'a') as log:
    log.write(json.dumps(['helm'] + args) + '\n')
if args[0] == 'list':
    print(json.dumps([dict(name='garage', namespace='garage', status='deployed', chart='garage-' + os.environ['DEPLOYED'])]))
elif args[:2] == ['show', 'chart']:
    print('name: garage\nversion: 0.10.4')
elif args[0] == 'template':
    print('kind: StatefulSet')
elif args[:2] == ['diff', 'upgrade'] or (args[0] == 'upgrade' and '--dry-run=server' in args):
    pass
elif os.environ.get('HELM_UPGRADE_FAILS') and args[0] == 'upgrade':
    sys.exit('upgrade failed')
else:
    sys.exit('unexpected Helm command: ' + str(args))
"""

STUB_KUBECTL = r"""#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ['TEST_LOG'], 'a') as log:
    log.write(json.dumps(['kubectl'] + args) + '\n')
if 'get' in args and 'statefulset' in args and any(a.startswith('-o') or a == 'jsonpath={.spec.replicas}' for a in args):
    print('3')
elif args[:2] == ['create', '--dry-run=client']:
    sys.stdin.read()
    print(json.dumps(dict(kind='StatefulSet', metadata=dict(name='garage'), spec={})))
elif os.environ.get('KUBECTL_ALLOW_PATCH') and 'patch' in args:
    pass
elif 'cluster-info' in args:
    pass
else:
    sys.exit('kubectl must not mutate: ' + str(args))
"""


class GarageScriptTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.directory, True)
        binary = self.directory / "bin"
        binary.mkdir()
        for name, body in (
            ("git", STUB_GIT),
            ("helm", STUB_UPGRADE_HELM),
            ("kubectl", STUB_KUBECTL),
        ):
            (binary / name).write_text(body)
            (binary / name).chmod(0o755)
        self.garage_dir = self.directory / "garage-dir"
        self.garage_dir.mkdir()
        self.log = self.directory / "commands.jsonl"
        self.environment = dict(
            os.environ,
            PATH=str(binary) + os.pathsep + os.environ["PATH"],
            TEST_LOG=str(self.log),
            GARAGE_DIR=str(self.garage_dir),
            DEPLOYED="0.10.4",
        )
        self.environment.pop("GARAGE_CHART_DIR", None)

    def run_script(self, *args):
        return subprocess.run(
            ["/bin/bash", str(ROOT / "garage/deploy-garage.sh"), *args],
            cwd=self.directory,
            env=self.environment,
            text=True,
            capture_output=True,
        )

    def commands(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def pinned_ref(self):
        text = (ROOT / "garage/deploy-garage.sh").read_text()
        return __import__("re").search(r'^GARAGE_CHART_REF="([^"]+)"', text, 8)[1]

    def test_missing_clone_is_cloned_at_the_pinned_ref(self):
        result = self.run_script("--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        clone = next(c for c in self.commands() if c[:2] == ["git", "clone"])
        self.assertEqual(clone[clone.index("--branch") + 1], self.pinned_ref())
        self.assertEqual(clone[-1], str(self.garage_dir / "garage"))

    def test_clone_on_another_tag_is_refused_naming_both_refs(self):
        clone = self.garage_dir / "garage"
        (clone / "script/helm/garage").mkdir(parents=True)
        (clone / "TAG").write_text("v2.2.0")
        for args in (("--dry-run",), ()):
            with self.subTest(args=args):
                result = self.run_script(*args)
                self.assertEqual(result.returncode, 1)
                self.assertIn("v2.2.0", result.stderr)
                self.assertIn(self.pinned_ref(), result.stderr)
                self.assertIn("checkout " + self.pinned_ref(), result.stderr)
        self.assertFalse([c for c in self.commands() if c[0] != "git" or c[1] != "-C"])

    def test_chart_dir_override_skips_the_pin(self):
        chart = self.directory / "mine"
        chart.mkdir()
        self.environment["GARAGE_CHART_DIR"] = str(chart)
        result = self.run_script("--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse([c for c in self.commands() if c[0] == "git"])

    def test_upgrade_dry_run_mutates_nothing(self):
        self.environment["DEPLOYED"] = "0.9.2"
        result = self.run_script("--upgrade-chart", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("release 2, 1, 0", result.stdout)
        commands = self.commands()
        for command in commands:
            if command[0] == "kubectl":
                self.assertTrue(
                    command[1:5] == ["-n", "garage", "get", "statefulset"]
                    or command[1:3] == ["create", "--dry-run=client"],
                    command,
                )
            if command[0] == "helm":
                self.assertTrue(
                    command[0:2]
                    in (
                        ["helm", "list"],
                        ["helm", "show"],
                        ["helm", "diff"],
                        ["helm", "template"],
                    )
                    or "--dry-run=server" in command,
                    command,
                )
        self.assertTrue([c for c in commands if c[:3] == ["helm", "diff", "upgrade"]])

    def test_upgrade_never_applies_the_storageclass(self):
        # A failing upgrade, then a successful one up to the roll: neither path
        # may reach `kubectl apply` (the stub kubectl exits on any apply).
        self.environment.update(
            DEPLOYED="0.9.2", HELM_UPGRADE_FAILS="1", KUBECTL_ALLOW_PATCH="1"
        )
        for name in ("garage-values.yaml", "storageclass-longhorn-garage-data.yaml"):
            (self.garage_dir / name).write_text("{}\n")
        result = self.run_script("--upgrade-chart")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("helm upgrade failed", result.stderr)
        applies = [c for c in self.commands() if c[0] == "kubectl" and "apply" in c]
        self.assertEqual(applies, [])

    def test_upgrade_without_the_flag_is_refused(self):
        self.environment["DEPLOYED"] = "0.9.2"
        result = self.run_script("--dry-run")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Chart version mismatch", result.stderr)


if __name__ == "__main__":
    unittest.main()
