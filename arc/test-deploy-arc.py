#!/usr/bin/env python3
"""Hermetic tests for the ARC CPU trial's shell deployment boundary."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_scripts" / "lib"))
from git_env import scrub_environ  # noqa: E402

scrub_environ()

import json
import copy
import importlib.util
import os
import re
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
if tool == "kubectl" and args[:2] == ["get", "autoscalingrunnerset"]:
    counts = {"f:minRunners": {}, "f:maxRunners": {}}
    owners = [dict(manager="helm", operation="Apply", apiVersion="actions.github.com/v1alpha1",
                   fieldsType="FieldsV1", fieldsV1={"f:spec": counts})]
    if os.environ.get("ARC_TEST_LEGACY_OWNER"):
        owners.append({**owners[0], "manager": "kubectl-patch", "operation": "Update"})
    print(json.dumps(dict(apiVersion="actions.github.com/v1alpha1", kind="AutoscalingRunnerSet",
        metadata=dict(name="homelab-arc-compendium", namespace="arc-runners", uid="test", resourceVersion="1",
            annotations={"meta.helm.sh/release-name": "compendium", "meta.helm.sh/release-namespace": "arc-runners"},
            managedFields=owners),
        spec=dict(minRunners=int(os.environ.get("ARC_TEST_COUNT", "0")), maxRunners=0))))
    sys.exit(0)
if tool == "kubectl" and args[:2] == ["get", "pods,ephemeralrunners,ephemeralrunnersets"]:
    print(json.dumps({"items": [{"kind": "Pod"}] if os.environ.get("ARC_TEST_BUSY") else []}))
    sys.exit(0)
if tool == "helm" and args[:1] == ["list"]:
    namespace = args[args.index("--namespace") + 1]
    controller = namespace == "arc-systems"
    release = "arc" if controller else "compendium"
    expected = ["list", "--namespace", namespace, "--deployed", "--filter",
                "^" + release + "$", "--output", "json"]
    if args != expected:
        print("Unsupported helm list arguments: " + repr(args), file=sys.stderr)
        sys.exit(97)
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
if tool == "helm" and (args[:1] == ["rollback"] or args[:2] == ["diff", "rollback"]):
    sys.exit(int(os.environ.get("ARC_TEST_HELM_EXIT", "0")))
print("Unexpected command: " + repr([tool, *args]), file=sys.stderr)
sys.exit(97)
"""


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
        for call in calls:
            if call[0] == "kubectl":
                self.assertTrue(call[1:] == ["cluster-info"] or call[1] == "get")
                if call[1] == "get":
                    self.assertEqual(call[call.index("-n") + 1], "arc-runners")
        for call in self.upgrades(calls):
            index = call.index("upgrade")
            self.assertEqual(call[index + 1], "compendium")
            self.assertNotIn("--install", call)
            self.assertNotIn("--allow-unreleased", call)
            self.assertEqual(call[call.index("--namespace") + 1], "arc-runners")
            self.assertEqual(call[call.index("--version") + 1], "0.15.7")
            if "--reuse-values" in call:
                self.assertNotIn("-f", call)
                self.assertNotIn("--reset-values", call)
                self.assertEqual(
                    call[call.index("--set") + 1], "minRunners=0,maxRunners=0"
                )
            else:
                self.assertIn("--reset-values", call)
                self.assertEqual(
                    call[call.index("-f") + 1],
                    str(ROOT / "runner-scale-set-compendium-values.yaml"),
                )
            self.assertNotIn("--force-conflicts", call)

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

    def test_release_lookup_uses_helm_three_and_four_deployed_filter(self):
        result, calls = self.run_script("--runner-cpu-trial", "--diff")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            [call for call in calls if call[:2] == ["helm", "list"]],
            [
                [
                    "helm",
                    "list",
                    "--namespace",
                    namespace,
                    "--deployed",
                    "--filter",
                    f"^{release}$",
                    "--output",
                    "json",
                ]
                for namespace, release in (
                    ("arc-systems", "arc"),
                    ("arc-runners", "compendium"),
                )
            ],
        )

    def test_diff_and_apply_use_identical_chart_and_values(self):
        for mode in (
            "--runner-cpu-trial",
            "--runner-cpu-baseline",
            "--runner-cpu-drain",
        ):
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
                    {"ARC_TEST_STATUS_" + key: "pending-rollback"},
                    {"ARC_TEST_CHART_" + key: "wrong-chart"},
                ]
            )
        for env in cases:
            for mode in (
                "--runner-cpu-trial",
                "--runner-cpu-baseline",
                "--runner-cpu-drain",
            ):
                for flags in ([], ["--diff"]):
                    with self.subTest(env=env, mode=mode, flags=flags):
                        result, calls = self.run_script(mode, *flags, env=env)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertEqual(self.upgrades(calls), [])
                        self.assert_scope(calls)

    def test_conflicting_or_duplicate_modes_fail_before_any_command(self):
        for second in (
            "--runner-cpu-trial",
            "--runner-cpu-baseline",
            "--runner-cpu-drain",
        ):
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

    def test_activation_requires_empty_fenced_scale_set(self):
        for mode in ("--runner-cpu-trial", "--runner-cpu-baseline"):
            for env in (
                {"ARC_TEST_BUSY": "1"},
                {"ARC_TEST_COUNT": "4"},
                {"ARC_TEST_LEGACY_OWNER": "1"},
            ):
                with self.subTest(mode=mode, env=env):
                    result, calls = self.run_script(mode, env=env)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(self.upgrades(calls), [])

    def test_drain_keeps_template_and_allows_existing_jobs_to_finish(self):
        result, calls = self.run_script(
            "--runner-cpu-drain", env={"ARC_TEST_BUSY": "1", "ARC_TEST_COUNT": "4"}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_scope(calls)
        self.assertEqual(len(self.upgrades(calls)), 1)
        result, calls = self.run_script(
            "--runner-cpu-drain", env={"ARC_TEST_LEGACY_OWNER": "1"}
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.upgrades(calls), [])

    def test_missing_jq_fails_the_suite_instead_of_skipping(self):
        environment = {**os.environ, "PATH": str(self.path)}
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve())],
            env=environment,
            text=True,
            capture_output=True,
            timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("jq is required", result.stderr)
        self.assertNotIn("skipped=", result.stderr)

    def test_documented_drain_gate_requires_no_old_runners(self):
        match = re.search(
            r"jq -e '(all\(\.items\[\];.*?)'", (ROOT / "arc.md").read_text(), re.S
        )
        self.assertIsNotNone(match)
        drained = {
            "kind": "EphemeralRunnerSet",
            "spec": {},  # ARC serializes zero desired replicas with omitempty.
            "status": {"currentReplicas": 0},
        }
        cases = [
            ([], True),
            ([drained], True),
            ([{**drained, "spec": {"replicas": 0}}], True),
            ([{**drained, "spec": {"replicas": 1}}], False),
            ([{**drained, "status": {"currentReplicas": 1}}], False),
            ([{**drained, "status": {}}], False),
            ([drained, {"kind": "Pod"}], False),
            ([drained, {"kind": "EphemeralRunner"}], False),
            ([drained, {**drained, "status": {"currentReplicas": 1}}], False),
        ]
        for items, accepted in cases:
            with self.subTest(items=items):
                result = subprocess.run(
                    ["jq", "-e", match.group(1)],
                    input=json.dumps({"items": items}),
                    text=True,
                    capture_output=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 0 if accepted else 1, result.stderr)

    def test_documented_capture_requires_positive_deployed_revision(self):
        match = re.search(
            r"jq -er '(if length == 1.*?)'", (ROOT / "arc.md").read_text(), re.S
        )
        self.assertIsNotNone(match)
        healthy = {"revision": "8", "status": "deployed"}
        for rows, accepted in (
            ([healthy], True),
            ([], False),
            ([healthy, healthy], False),
            ([{**healthy, "status": "failed"}], False),
            ([{**healthy, "status": "pending-upgrade"}], False),
            ([{**healthy, "status": "pending-rollback"}], False),
            ([{**healthy, "revision": "0"}], False),
        ):
            with self.subTest(rows=rows):
                result = subprocess.run(
                    ["jq", "-er", match.group(1)],
                    input=json.dumps(rows),
                    text=True,
                    capture_output=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode == 0, accepted, result.stderr)

    def test_documented_recovery_uses_explicit_revision_and_supported_flags(self):
        commands = re.findall(
            r"^helm (?:diff )?rollback compendium .+$",
            (ROOT / "arc.md").read_text(),
            re.M,
        )
        self.assertEqual(len(commands), 2)
        for command, expected in zip(
            commands,
            (
                [
                    "helm",
                    "diff",
                    "rollback",
                    "compendium",
                    "8",
                    "-n",
                    "arc-runners",
                    "--suppress-secrets",
                ],
                [
                    "helm",
                    "rollback",
                    "compendium",
                    "8",
                    "-n",
                    "arc-runners",
                    "--wait",
                    "--timeout",
                    "5m",
                ],
            ),
        ):
            for revision in ("8", ""):
                with self.subTest(command=command, revision=revision):
                    self.calls_path.unlink(missing_ok=True)
                    result = subprocess.run(
                        ["bash", "-c", command],
                        env={
                            **os.environ,
                            "PATH": f"{self.path}{os.pathsep}{os.environ['PATH']}",
                            "ARC_TEST_CALLS": str(self.calls_path),
                            "arc_cpu_drain_revision": revision,
                        },
                        text=True,
                        capture_output=True,
                        timeout=10,
                    )
                    if revision:
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(
                            json.loads(self.calls_path.read_text()), expected
                        )
                    else:
                        self.assertNotEqual(result.returncode, 0)
                        self.assertFalse(self.calls_path.exists())


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "ownership", ROOT / "runner-count-ownership.py"
        )
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        counts = {"f:minRunners": {}, "f:maxRunners": {}}
        helm = dict(
            manager="helm",
            operation="Apply",
            apiVersion=self.module.API,
            fieldsType="FieldsV1",
            fieldsV1={"f:spec": counts},
        )
        legacy = {
            **copy.deepcopy(helm),
            "manager": "kubectl-patch",
            "operation": "Update",
        }
        self.resource = dict(
            apiVersion=self.module.API,
            kind="AutoscalingRunnerSet",
            metadata=dict(
                name=self.module.NAME,
                namespace=self.module.NAMESPACE,
                uid="identity",
                resourceVersion="7",
                annotations={
                    "meta.helm.sh/release-name": "compendium",
                    "meta.helm.sh/release-namespace": "arc-runners",
                },
                managedFields=[helm, legacy],
            ),
            spec=dict(minRunners=4, maxRunners=8),
        )

    def test_handoff_only_removes_known_coowner_and_guards_identity_and_version(self):
        before = copy.deepcopy(self.resource)
        self.assertEqual(
            self.module.handoff_patch(self.resource),
            [
                {"op": "test", "path": "/metadata/uid", "value": "identity"},
                {"op": "test", "path": "/metadata/resourceVersion", "value": "7"},
                {"op": "remove", "path": "/metadata/managedFields/1"},
            ],
        )
        self.assertEqual(self.resource, before)
        self.resource["metadata"]["managedFields"].pop()
        self.assertEqual(self.module.handoff_patch(self.resource), [])

    def test_unexpected_ownership_or_identity_fails_closed(self):
        cases = []
        for key, value in (
            ("manager", "another-operator"),
            ("operation", "Apply"),
            ("apiVersion", "wrong"),
        ):
            resource = copy.deepcopy(self.resource)
            resource["metadata"]["managedFields"][1][key] = value
            cases.append(resource)
        resource = copy.deepcopy(self.resource)
        resource["metadata"]["managedFields"][1]["fieldsV1"]["f:spec"][
            "f:template"
        ] = {}
        cases.append(resource)
        resource = copy.deepcopy(self.resource)
        resource["metadata"]["managedFields"].pop(0)
        cases.append(resource)
        resource = copy.deepcopy(self.resource)
        resource["metadata"]["name"] = "other-scale-set"
        cases.append(resource)
        for resource in cases:
            with self.subTest(resource=resource):
                with self.assertRaises(ValueError):
                    self.module.handoff_patch(resource)


if __name__ == "__main__":
    if not shutil.which("jq"):
        raise SystemExit(
            "ERROR: jq is required to run the ARC deployment safety tests."
        )
    unittest.main()
