"""Offline checks for the capped EmbeddingGemma 2 probe: manifest, cap, gates, restore."""

import copy
import json
import os
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import benchmark_1080_slots as bench
import probe_eg2_1080 as probe

REPO = Path(__file__).resolve().parents[2]
SERVICES = ("embedder-eg2-service.yaml", "embedder-qwen-service.yaml")


def sample():
    rows = []
    for target, n in ((300, 12), (1000, 10), (3000, 10), (6000, 6), (7600, 6)):
        rows += [{"target": target, "tokens": target, "text": f"t{target}"}] * n
    return rows


def passing_records():
    log = "llama_context: Flash Attention enabled\n"
    pairs = [
        {"length": n, "ms": 100.0 + n / 100}
        for a, b in probe.PAIRS
        for n in (a, b)
        for _ in range(3)
    ]
    buckets = [
        {"target": t, "ms": ms}
        for t, ms in ((300, 40.0), (1000, 120.0), (3000, 400.0))
        for _ in range(3)
    ]
    return {"startup_log": log, "pairs": pairs, "buckets": buckets, "guard": None}


class ManifestTests(unittest.TestCase):
    def test_probe_pod_shape(self):
        pod = probe.manifest()
        container = pod["spec"]["containers"][0]
        self.assertEqual(container["image"], probe.IMAGE)
        self.assertIn("sha256:c4348afb", container["image"])
        self.assertEqual(container["resources"]["limits"]["memory"], "6Gi")
        self.assertEqual(container["resources"]["limits"]["nvidia.com/gpu"], "1")
        claims = [
            v["persistentVolumeClaim"]["claimName"]
            for v in pod["spec"]["volumes"]
            if "persistentVolumeClaim" in v
        ]
        self.assertEqual(claims, ["embedder-eg2-model-cache"])
        args = container["args"]
        self.assertEqual(args[args.index("--flash-attn") + 1], "auto")
        # Gate 1 reads INFO and WARN lines, so verbosity must stay at 3 or above.
        self.assertGreaterEqual(int(args[args.index("--verbosity") + 1]), 3)
        self.assertNotIn("-lv", args)
        self.assertEqual(args[args.index("--pooling") + 1], "mean")

    def test_no_service_selects_the_probe(self):
        label = probe.manifest()["metadata"]["labels"]["app"]
        for name in SERVICES:
            text = (REPO / "viking" / "manifests" / name).read_text()
            self.assertIn("app: embedder-", text)
            self.assertNotIn(f"app: {label}", text)

    def test_probe_args_extend_the_deployment_args(self):
        text = (
            REPO / "viking" / "manifests" / "embedder-eg2-cuda-deployment.yaml"
        ).read_text()
        block = text.split("args:\n", 2)[2].split("ports:")[0]
        deployed = [
            line.strip()[2:].strip('"')
            for line in block.splitlines()
            if line.strip().startswith("- ")
        ]
        self.assertEqual(deployed, probe.EG2_ARGS)

    def test_prepull_requests_no_gpu(self):
        pod = probe.prepull_manifest()
        self.assertNotIn("runtimeClassName", pod["spec"])
        container = pod["spec"]["containers"][0]
        self.assertEqual(container["image"], probe.IMAGE)
        self.assertEqual(container["command"], ["/app/llama-server", "--version"])
        for kind in ("requests", "limits"):
            self.assertNotIn("nvidia.com/gpu", container["resources"][kind])


class CapTests(unittest.TestCase):
    def test_select_inputs_keeps_only_capped_targets(self):
        kept = probe.select_inputs(sample())
        self.assertEqual(len(kept), 32)
        self.assertEqual({r["target"] for r in kept}, {300, 1000, 3000})
        self.assertTrue(all(r["qwen_tokens"] <= probe.CAP for r in kept))

    def test_cap_inputs_drops_texts_over_the_eg2_cap(self):
        texts = [
            {"index": 0, "target": 3000, "text": "a"},
            {"index": 1, "target": 3000, "text": "b"},
            {"index": 2, "target": 3000, "text": "c"},
        ]
        counts = {"a": 2900, "b": probe.CAP - probe.PREFIX_MARGIN + 1, "c": 3500}
        kept, dropped = probe.cap_inputs(texts, counts.__getitem__)
        self.assertEqual([r["index"] for r in kept], [0])
        self.assertEqual([r["index"] for r in dropped], [1, 2])


class EvaluateTests(unittest.TestCase):
    def test_all_measured_gates_pass_and_gate4_pends(self):
        result = probe.evaluate(passing_records())
        self.assertEqual(result["verdict"], "PENDING")
        result = probe.evaluate(passing_records(), {"min": 0.9995, "median": 0.9999})
        self.assertEqual(result["verdict"], "PASS")

    def test_cpu_assignment_line_fails_gate1(self):
        records = passing_records()
        records["startup_log"] += (
            "llama_context: layer 3 is assigned to device CUDA0 but Flash Attention"
            " is assigned to device CPU (usually due to missing support)\n"
        )
        result = probe.evaluate(records)
        self.assertFalse(result["gate1_attention_on_gpu"]["pass"])
        self.assertEqual(result["verdict"], "FAIL")

    def test_gate1_needs_the_enabled_line(self):
        records = passing_records()
        records["startup_log"] = "no resolution lines at this verbosity\n"
        self.assertFalse(probe.evaluate(records)["gate1_attention_on_gpu"]["pass"])
        records["startup_log"] = "Flash Attention not supported, set to disabled\n"
        self.assertFalse(probe.evaluate(records)["gate1_attention_on_gpu"]["pass"])

    def test_pair_ratio_over_limit_fails_gate2_in_either_direction(self):
        for slow in (2049, 2048):
            records = passing_records()
            for row in records["pairs"]:
                if row["length"] == slow:
                    row["ms"] = 160.0
                elif row["length"] in (2048, 2049):
                    row["ms"] = 100.0
            gate = probe.evaluate(records)["gate2_no_length_cliff"]
            self.assertFalse(gate["pass"])
            self.assertEqual(gate["pairs"]["2048/2049"]["ratio"], 1.6)

    def test_slow_3k_bucket_fails_gate3(self):
        records = passing_records()
        for row in records["buckets"]:
            if row["target"] == 3000:
                row["ms"] = 3700.0
        gate = probe.evaluate(records)["gate3_faster_than_qwen"]
        self.assertFalse(gate["pass"])
        self.assertTrue(gate["buckets"]["1000"]["pass"])

    def test_partial_records_fail_unmeasured_gates(self):
        records = passing_records()
        records["buckets"] = [r for r in records["buckets"] if r["target"] == 300]
        records["guard"] = "rss guard"
        result = probe.evaluate(records)
        self.assertTrue(result["gate2_no_length_cliff"]["pass"])
        self.assertFalse(result["gate3_faster_than_qwen"]["buckets"]["3000"]["pass"])
        self.assertEqual(result["guard"], "rss guard")
        self.assertEqual(result["verdict"], "FAIL")

    def test_low_cosine_fails_gate4(self):
        result = probe.evaluate(passing_records(), {"min": 0.998})
        self.assertFalse(result["gate4_reference_vectors"]["pass"])


SNAPSHOT = {
    "metadata": {"uid": "u", "resourceVersion": "1"},
    "spec": {"template": {"image": "qwen"}, "replicas": 1},
}


class FakeDeployment:
    """embedder-qwen-cuda as bench.obj / bench.kube see it, honoring --resource-version."""

    def __init__(self, image="qwen"):
        self.state = copy.deepcopy(SNAPSHOT)
        self.state["spec"]["template"]["image"] = image
        self.scales = []
        self.lost_responses = []  # exceptions raised after a scale lands
        self.unlanded = []  # exceptions raised before a scale lands

    def obj(self, *args):
        return copy.deepcopy(self.state)

    def kube(self, *args, **kwargs):
        if args[0] != "scale":
            return SimpleNamespace(stdout="")
        self.scales.append(args)
        if self.unlanded:
            raise self.unlanded.pop(0)
        version = args[3].split("=", 1)[1]
        if version != self.state["metadata"]["resourceVersion"]:
            raise subprocess.CalledProcessError(1, "kubectl scale", "conflict")
        self.state["spec"]["replicas"] = int(args[2].split("=", 1)[1])
        self.state["metadata"]["resourceVersion"] = str(int(version) + 1)
        if self.lost_responses:
            raise self.lost_responses.pop(0)
        return SimpleNamespace(stdout="")


class WindowTests(unittest.TestCase):
    def run_window(
        self,
        body=lambda: None,
        budget_s=60,
        kube_effect=None,
        during_restore=None,
        deployment=None,
    ):
        abort = threading.Event()
        deployment = deployment or FakeDeployment()
        start_pod = MagicMock()

        def probe_kube(*args, **kwargs):
            if kube_effect:
                kube_effect(*args, **kwargs)
            return SimpleNamespace(stdout="")

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(bench, "obj", side_effect=deployment.obj),
            patch.object(bench, "kube", side_effect=deployment.kube),
            patch.object(bench, "RETRY_PAUSE_S", 0),
            patch.object(probe, "kube", side_effect=probe_kube) as kube,
            patch.object(
                probe, "cleanup_restore", side_effect=during_restore
            ) as restore,
        ):
            out = Path(directory)
            error = None
            try:
                probe.run_window(out, SNAPSHOT, budget_s, start_pod, body, abort)
            except Exception as exc:  # noqa: BLE001 — each test asserts the type
                error = exc
            window = json.loads((out / "window.json").read_text())
            return SimpleNamespace(
                error=error,
                window=window,
                kube=kube,
                restore=restore,
                start_pod=start_pod,
                deployment=deployment,
                out=out,
                abort=abort,
            )

    def test_failure_in_measurements_restores_once(self):
        def body():
            raise RuntimeError("probe broke")

        run = self.run_window(body)
        self.assertEqual(str(run.error), "probe broke")
        run.restore.assert_called_once()
        args, kwargs = run.restore.call_args
        self.assertEqual(args, (SNAPSHOT, run.out))
        self.assertEqual(kwargs["pod"], probe.POD)
        self.assertIn("deadline", kwargs)
        self.assertEqual(run.window["stopped_by"], ["RuntimeError: probe broke"])
        self.assertIn("restored_at", run.window)
        self.assertIn("--resource-version=1", run.deployment.scales[0])
        run.start_pod.assert_called_once()

    def test_restore_deadline_is_twenty_minutes_after_scale_down(self):
        before = time.monotonic()
        run = self.run_window()
        deadline = run.restore.call_args.kwargs["deadline"]
        self.assertGreaterEqual(deadline - before, probe.DEADLINE_MINUTES * 60 - 1)
        self.assertLessEqual(deadline - before, probe.DEADLINE_MINUTES * 60 + 1)

    def test_deadline_stops_measurements_and_restores(self):
        run = self.run_window(lambda: time.sleep(5), 1)
        self.assertEqual(str(run.error), "deadline reached")
        run.restore.assert_called_once()
        self.assertTrue(run.abort.is_set())
        self.assertIn("restored_at", run.window)
        self.assertLess(run.window["outage_seconds"], 5)

    def test_success_restores_once(self):
        run = self.run_window()
        self.assertIsNone(run.error)
        run.restore.assert_called_once()
        self.assertNotIn("stopped_by", run.window)
        self.assertFalse(run.window["overran_deadline"])

    def test_template_changed_before_scale_down_leaves_production_alone(self):
        run = self.run_window(deployment=FakeDeployment(image="new"))
        self.assertIsInstance(run.error, bench.ProductionChanged)
        self.assertEqual(run.deployment.scales, [])
        run.restore.assert_not_called()
        run.start_pod.assert_not_called()
        self.assertTrue(run.window["production_untouched"])

    def test_scale_down_that_landed_before_its_timeout_is_not_repeated(self):
        deployment = FakeDeployment()
        deployment.lost_responses.append(subprocess.TimeoutExpired("kubectl scale", 60))
        run = self.run_window(deployment=deployment)
        self.assertIsNone(run.error)
        self.assertEqual(len(run.deployment.scales), 1)
        run.start_pod.assert_called_once()
        run.restore.assert_called_once()

    def test_scale_down_write_that_never_lands_still_restores(self):
        deployment = FakeDeployment()
        deployment.unlanded = [
            subprocess.TimeoutExpired("kubectl scale", 60)
        ] * bench.RESTORE_ATTEMPTS
        run = self.run_window(deployment=deployment)
        self.assertIsInstance(run.error, subprocess.TimeoutExpired)
        self.assertEqual(run.deployment.state["spec"]["replicas"], 1)
        run.start_pod.assert_not_called()
        run.restore.assert_called_once()

    def test_production_pod_drain_timeout_still_restores(self):
        def kube_effect(*args, **kwargs):
            if args[0] == "wait":
                raise subprocess.TimeoutExpired("kubectl wait", 190)

        run = self.run_window(kube_effect=kube_effect)
        self.assertIsInstance(run.error, subprocess.TimeoutExpired)
        run.start_pod.assert_not_called()
        run.restore.assert_called_once()

    def test_slow_setup_never_starts_the_probe_past_the_cutoff(self):
        def kube_effect(*args, **kwargs):
            if args[0] == "wait":
                time.sleep(1.2)

        run = self.run_window(budget_s=1, kube_effect=kube_effect)
        self.assertEqual(str(run.error), "deadline reached")
        wait = next(c for c in run.kube.call_args_list if c.args[0] == "wait")
        self.assertIn("--timeout=1s", wait.args)
        run.start_pod.assert_not_called()
        run.restore.assert_called_once()

    def test_probe_delete_runs_even_when_the_restore_fails(self):
        def during_restore(*args, **kwargs):
            raise RuntimeError("rollout failed")

        run = self.run_window(during_restore=during_restore)
        self.assertEqual(str(run.error), "rollout failed")
        deletes = [
            c for c in run.kube.call_args_list if c.args[:2] == ("delete", "pod")
        ]
        self.assertEqual(len(deletes), 1)
        self.assertNotIn("restored_at", run.window)

    def test_signal_before_create_prevents_the_probe(self):
        def kube_effect(*args, **kwargs):
            if args[0] == "wait":
                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(0.2)

        run = self.run_window(kube_effect=kube_effect)
        self.assertEqual(str(run.error), f"interrupted: signal {signal.SIGTERM}")
        run.start_pod.assert_not_called()
        run.restore.assert_called_once()

    def test_signals_never_unwind_past_the_restore(self):
        def body():
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(5)

        def during_restore(*args, **kwargs):
            # A second Ctrl-C mid-restore only records a reason.
            os.kill(os.getpid(), signal.SIGINT)
            time.sleep(0.2)

        run = self.run_window(body, during_restore=during_restore)
        run.restore.assert_called_once()
        self.assertIn("restored_at", run.window)
        self.assertEqual(str(run.error), f"interrupted: signal {signal.SIGTERM}")
        self.assertEqual(len(run.window["stopped_by"]), 2)


class RestoreTests(unittest.TestCase):
    def deployment(self, replicas, version, image="qwen"):
        return {
            "metadata": {"uid": "u", "resourceVersion": version},
            "spec": {"template": {"image": image}, "replicas": replicas},
        }

    def restore(self, gets, kube_effect=None, deadline=None):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(bench, "obj", side_effect=gets) as obj,
            patch.object(bench, "kube", side_effect=kube_effect) as kube,
            patch.object(bench, "Forward"),
            patch.object(
                bench, "http", return_value={"data": [{"embedding": [1.0] * 2560}]}
            ),
            patch.object(bench, "RETRY_PAUSE_S", 0),
        ):
            error = None
            try:
                bench.restore(SNAPSHOT, Path(directory), deadline)
            except Exception as exc:  # noqa: BLE001 — each test asserts the type
                error = exc
            restored = (Path(directory) / "restored.json").exists()
            return error, obj, kube, restored

    def scales(self, kube):
        return [c for c in kube.call_args_list if c.args[0] == "scale"]

    def test_scale_is_conditional_on_the_checked_version(self):
        gets = [self.deployment(0, "7"), self.deployment(1, "8")]
        error, _, kube, restored = self.restore(gets)
        self.assertIsNone(error)
        self.assertTrue(restored)
        self.assertIn("--resource-version=7", self.scales(kube)[0].args)

    def test_transient_read_timeout_is_retried(self):
        gets = [
            subprocess.TimeoutExpired("kubectl get", 60),
            self.deployment(0, "7"),
            self.deployment(1, "8"),
        ]
        error, _, kube, restored = self.restore(gets)
        self.assertIsNone(error)
        self.assertTrue(restored)
        self.assertEqual(len(self.scales(kube)), 1)

    def test_edit_between_check_and_scale_is_refused(self):
        def kube_effect(*args, **kwargs):
            if args[0] == "scale":
                raise subprocess.CalledProcessError(1, "kubectl scale", "conflict")

        gets = [self.deployment(0, "7"), self.deployment(0, "9", image="new")]
        error, _, kube, restored = self.restore(gets, kube_effect)
        self.assertRegex(str(error), "changed concurrently")
        self.assertEqual(len(self.scales(kube)), 1)
        self.assertFalse(restored)

    def test_scale_that_landed_before_its_timeout_is_not_repeated(self):
        def kube_effect(*args, **kwargs):
            if args[0] == "scale":
                raise subprocess.TimeoutExpired("kubectl scale", 60)

        gets = [
            self.deployment(0, "7"),
            self.deployment(1, "8"),
            self.deployment(1, "8"),
        ]
        error, _, kube, restored = self.restore(gets, kube_effect)
        self.assertIsNone(error)
        self.assertTrue(restored)
        self.assertEqual(len(self.scales(kube)), 1)

    def test_no_retry_starts_after_the_deadline(self):
        gets = [
            subprocess.TimeoutExpired("kubectl get", 60),
            self.deployment(0, "7"),
        ]
        error, obj, kube, restored = self.restore(gets, deadline=time.monotonic() - 1)
        self.assertIsInstance(error, subprocess.TimeoutExpired)
        self.assertEqual(obj.call_count, 1)
        self.assertEqual(self.scales(kube), [])
        self.assertFalse(restored)

    def test_no_retry_starts_when_the_pause_crosses_the_deadline(self):
        gets = [
            subprocess.TimeoutExpired("kubectl get", 60),
            self.deployment(0, "7"),
        ]
        with (
            patch.object(bench, "RETRY_PAUSE_S", 0.2),
            tempfile.TemporaryDirectory() as directory,
            patch.object(bench, "obj", side_effect=gets) as obj,
            patch.object(bench, "kube") as kube,
            self.assertRaises(subprocess.TimeoutExpired),
        ):
            bench.restore(SNAPSHOT, Path(directory), time.monotonic() + 0.1)
        self.assertEqual(obj.call_count, 1)
        kube.assert_not_called()

    def test_probe_delete_is_retried_before_the_restore(self):
        attempts = [subprocess.TimeoutExpired("kubectl delete", 70), None]

        def kube_effect(*args, **kwargs):
            effect = attempts.pop(0)
            if effect:
                raise effect

        with (
            patch.object(bench, "kube", side_effect=kube_effect) as kube,
            patch.object(bench, "RETRY_PAUSE_S", 0),
            patch.object(bench, "restore") as restore,
        ):
            bench.cleanup_restore(SNAPSHOT, Path("unused"), probe.POD)
        self.assertEqual(kube.call_count, 2)
        restore.assert_called_once()

    def test_retries_continue_before_the_deadline(self):
        gets = [
            subprocess.TimeoutExpired("kubectl get", 60),
            self.deployment(0, "7"),
            self.deployment(1, "8"),
        ]
        error, _, _, restored = self.restore(gets, deadline=time.monotonic() + 600)
        self.assertIsNone(error)
        self.assertTrue(restored)

    def test_template_changed_during_rollout_is_reported(self):
        gets = [self.deployment(0, "7"), self.deployment(1, "9", image="new")]
        error, _, _, restored = self.restore(gets)
        self.assertRegex(str(error), "changed concurrently")
        self.assertFalse(restored)


if __name__ == "__main__":
    unittest.main()
