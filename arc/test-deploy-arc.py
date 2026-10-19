#!/usr/bin/env python3
"""Hermetic tests for the ARC CPU trial's shell deployment boundary."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent
MOCK = r"""#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

tool = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["ARC_TEST_CALLS"], "a") as stream:
    stream.write(json.dumps([tool, *args]) + "\n")
if tool == "kubectl" and args == ["cluster-info"]:
    sys.exit(0)
if tool == "helm" and args[:1] == ["list"]:
    namespace = args[args.index("--namespace") + 1]
    controller = namespace == "arc-systems"
    release = "arc" if controller else "compendium"
    key = "CONTROLLER" if controller else "RUNNER"
    if os.environ.get("ARC_TEST_FAIL_LOOKUP") == key:
        sys.exit(9)
    if os.environ.get("ARC_TEST_MISSING") == key:
        print("[]")
    else:
        chart = "gha-runner-scale-set" + ("-controller" if controller else "")
        chart = os.environ.get("ARC_TEST_CHART_" + key, chart)
        version = os.environ.get("ARC_TEST_VERSION_" + key, "0.15.7")
        status = os.environ.get("ARC_TEST_STATUS_" + key, "deployed")
        print(json.dumps([dict(name=release, namespace=namespace,
                               chart=chart + "-" + version, status=status)]))
    sys.exit(0)
if tool == "helm" and args == ["plugin", "list"]:
    print("NAME VERSION DESCRIPTION")
    if not os.environ.get("ARC_TEST_NO_DIFF"):
        print("diff 3.14.0 Diff plugin")
    sys.exit(0)
if tool == "helm" and (args[:1] == ["upgrade"] or args[:2] == ["diff", "upgrade"]):
    sys.exit(int(os.environ.get("ARC_TEST_HELM_EXIT", "0")))
print("Unexpected command: " + repr([tool, *args]), file=sys.stderr)
sys.exit(97)
"""


@unittest.skipUnless(shutil.which("jq"), "jq is required by the deployment script")
class RunnerCpuTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        mock = self.path / "mock.py"
        mock.write_text(MOCK)
        mock.chmod(0o755)
        for name in ("kubectl", "helm"):
            (self.path / name).symlink_to(mock)
        self.calls_path = self.path / "calls.jsonl"

    def run_script(self, *args, env=None):
        self.calls_path.unlink(missing_ok=True)
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("ARC_TEST_") and key != "ARC_CHART_VERSION"
        }
        environment.update(
            PATH=f"{self.path}{os.pathsep}{os.environ['PATH']}",
            ARC_TEST_CALLS=str(self.calls_path),
        )
        environment.update(env or {})
        result = subprocess.run(
            ["bash", str(ROOT / "deploy-arc.sh"), *args],
            env=environment,
            text=True,
            capture_output=True,
            timeout=10,
        )
        calls = (
            [json.loads(line) for line in self.calls_path.read_text().splitlines()]
            if self.calls_path.exists()
            else []
        )
        return result, calls

    def upgrades(self, calls):
        return [call for call in calls if "upgrade" in call]

    def assert_scope(self, calls):
        self.assertEqual(
            [call for call in calls if call[0] == "kubectl"],
            [["kubectl", "cluster-info"]],
        )
        for call in self.upgrades(calls):
            index = call.index("upgrade")
            self.assertEqual(call[index + 1], "compendium")
            self.assertNotIn("--install", call)
            self.assertNotIn("--allow-unreleased", call)
            self.assertEqual(call[call.index("--namespace") + 1], "arc-runners")
            self.assertEqual(call[call.index("--version") + 1], "0.15.7")
            self.assertIn("--reset-values", call)
            self.assertEqual(
                call[call.index("-f") + 1],
                str(ROOT / "runner-scale-set-compendium-values.yaml"),
            )

    def test_trial_changes_only_expected_scale_set_values(self):
        result, calls = self.run_script("--runner-cpu-trial")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_scope(calls)
        [upgrade] = self.upgrades(calls)
        self.assertEqual(
            upgrade[upgrade.index("--set") + 1],
            "minRunners=4,maxRunners=4,"
            "template.spec.containers[0].resources.requests.cpu=1000m,"
            "template.spec.containers[0].resources.limits.cpu=2",
        )

    def test_baseline_resets_values_without_trial_overrides(self):
        result, calls = self.run_script("--runner-cpu-baseline")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_scope(calls)
        [upgrade] = self.upgrades(calls)
        self.assertNotIn("--set", upgrade)

    def test_diff_and_apply_use_identical_chart_and_values(self):
        for mode in ("--runner-cpu-trial", "--runner-cpu-baseline"):
            with self.subTest(mode=mode):
                result, calls = self.run_script(mode, "--diff")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assert_scope(calls)
                [diff] = self.upgrades(calls)
                self.assertEqual(diff[:3], ["helm", "diff", "upgrade"])
                result, calls = self.run_script(mode)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assert_scope(calls)
                [apply] = self.upgrades(calls)
                self.assertEqual(diff[3:], apply[2 : apply.index("--wait")])

    def test_failed_preconditions_never_reach_diff_or_apply(self):
        cases = [
            {"ARC_TEST_VERSION_CONTROLLER": "0.14.2"},
            {"ARC_CHART_VERSION": "9.0.0"},
        ]
        for key in ("CONTROLLER", "RUNNER"):
            cases.extend(
                [
                    {"ARC_TEST_MISSING": key},
                    {"ARC_TEST_FAIL_LOOKUP": key},
                    {"ARC_TEST_STATUS_" + key: "failed"},
                    {"ARC_TEST_STATUS_" + key: "pending-upgrade"},
                    {"ARC_TEST_CHART_" + key: "wrong-chart"},
                ]
            )
        for env in cases:
            for flags in ([], ["--diff"]):
                with self.subTest(env=env, flags=flags):
                    result, calls = self.run_script(
                        "--runner-cpu-trial", *flags, env=env
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(self.upgrades(calls), [])
                    self.assert_scope(calls)

    def test_conflicting_or_duplicate_modes_fail_before_any_command(self):
        for second in ("--runner-cpu-trial", "--runner-cpu-baseline"):
            result, calls = self.run_script("--runner-cpu-trial", second)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(calls, [])

    def test_missing_diff_plugin_never_applies(self):
        result, calls = self.run_script(
            "--runner-cpu-trial", "--diff", env={"ARC_TEST_NO_DIFF": "1"}
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.upgrades(calls), [])

    def test_helm_failure_is_not_hidden(self):
        for flags in ([], ["--diff"]):
            result, _ = self.run_script(
                "--runner-cpu-trial", *flags, env={"ARC_TEST_HELM_EXIT": "8"}
            )
            self.assertEqual(result.returncode, 8)


if __name__ == "__main__":
    unittest.main()
