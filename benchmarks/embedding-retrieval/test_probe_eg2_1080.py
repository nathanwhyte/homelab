"""Offline checks for the capped EmbeddingGemma 2 probe: manifest, cap, gates, restore."""

import copy
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
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
        for t, ms in (
            (300, 40.0),
            (1000, 120.0),
            (3000, 400.0),
            (6000, 900.0),
            (7600, 1300.0),
        )
        for _ in range(3)
    ]
    return {"startup_log": log, "pairs": pairs, "buckets": buckets, "guard": None}


class ManifestTests(unittest.TestCase):
    def test_probe_pod_shape(self):
        pod = probe.manifest()
        container = pod["spec"]["containers"][0]
        self.assertEqual(container["image"], probe.IMAGE)
        self.assertIn("sha256:b0baccbe", container["image"])
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
        # Gate 1 reads libllama INFO lines, which log at TRACE (4).
        self.assertGreaterEqual(int(args[args.index("--verbosity") + 1]), 4)
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
        self.assertEqual(len(kept), 44)
        self.assertEqual({r["target"] for r in kept}, {300, 1000, 3000, 6000, 7600})
        self.assertTrue(all(r["qwen_tokens"] <= probe.CAP for r in kept))

    def test_cap_inputs_drops_texts_over_the_eg2_cap(self):
        texts = [
            {"index": 0, "target": 7600, "text": "a"},
            {"index": 1, "target": 7600, "text": "b"},
            {"index": 2, "target": 7600, "text": "c"},
        ]
        counts = {"a": 7900, "b": probe.CAP - probe.PREFIX_MARGIN + 1, "c": 8500}
        kept, dropped = probe.cap_inputs(texts, counts.__getitem__)
        self.assertEqual([r["index"] for r in kept], [0])
        self.assertEqual([r["index"] for r in dropped], [1, 2])

    def test_select_inputs_stops_at_the_cap(self):
        edge = probe.CAP - probe.PREFIX_MARGIN
        rows = [
            {"target": 7600, "tokens": edge, "text": "fits"},
            {"target": 7600, "tokens": edge + 1, "text": "over"},
        ]
        self.assertEqual([r["text"] for r in probe.select_inputs(rows)], ["fits"])


class ShortInputTests(unittest.TestCase):
    def test_query_file_in_the_repo_loads(self):
        queries = probe.load_queries(
            Path(__file__).parent / "task1218-eg2-short-queries.json"
        )
        self.assertEqual(len(queries), 78)

    def test_load_queries_rejects_duplicate_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "q.json"
            path.write_text(
                json.dumps(
                    {"queries": [{"id": "a", "text": "x"}, {"id": "a", "text": "y"}]}
                )
            )
            with self.assertRaises(ValueError):
                probe.load_queries(path)

    def run_queries(self, source, queries, text_tokens=len):
        """measure() in --queries mode against a fake server; returns (records, sent)."""
        sent = []

        def fake_http(base, path, body=None, timeout=None):
            if path == "/tokenize":
                text = body["content"]
                bos = [2, 1] if body["add_special"] else []
                if text == source:
                    return {"tokens": bos + list(range(10, 10 + len(text)))}
                return {"tokens": bos + [7] * text_tokens(text)}
            value = body["input"][0]
            sent.append(value)
            n = len(value) if isinstance(value, list) else text_tokens(value)
            return {
                "data": [{"embedding": [1.0] * probe.DIMENSION}],
                "usage": {"prompt_tokens": n},
            }

        records = {"pairs": [], "buckets": [], "ordering": [], "short": []}
        error = None
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(probe, "http", fake_http),
        ):
            try:
                probe.measure(
                    "http://x",
                    MagicMock(),
                    threading.Event(),
                    [],
                    source,
                    Path(tmp),
                    records,
                    lambda: None,
                    queries=queries,
                )
            except RuntimeError as exc:
                error = exc
        return records, sent, error

    def test_queries_mode_sends_exactly_the_planned_inputs(self):
        records, sent, error = self.run_queries(
            "s" * 400, [{"id": "q1", "text": "what is BUG-1"}]
        )
        self.assertIsNone(error)
        # Independent of probe.SHORT_LENGTHS.
        expected = ["probe warm-up request"]
        for n in (8, 16, 32, 64, 128, 255, 256, 257):
            fragment = list(range(10, 10 + n))
            expected += [fragment, [2, 1] + fragment]
        expected.append("what is BUG-1")
        self.assertEqual(sent, expected)
        self.assertEqual(records["pairs"], [])
        self.assertEqual(records["buckets"], [])
        self.assertEqual(records["bos"], [2, 1])

    def test_short_fragment_source_is_refused_before_sending(self):
        _, sent, error = self.run_queries("s" * 100, [{"id": "q1", "text": "q"}])
        self.assertIn("fragment source has 100 tokens", str(error))
        self.assertEqual(sent, ["probe warm-up request"])

    def test_query_over_the_cap_is_never_sent(self):
        def tokens(text):
            return probe.CAP + 1 if text == "long" else 5

        _, sent, error = self.run_queries(
            "s" * 400,
            [{"id": "ok", "text": "short"}, {"id": "big", "text": "long"}],
            tokens,
        )
        self.assertIn("query big", str(error))
        self.assertIn("short", sent)
        self.assertNotIn("long", sent)

    def test_queries_plan_counts_the_queries_workload(self):
        queries = [{"id": "a", "text": "x"}, {"id": "b", "text": "y"}]
        planned = probe.plan(sample(), probe.select_inputs(sample()), queries)
        self.assertEqual(planned["mode"], "queries")
        self.assertEqual(planned["requests"], 1 + 2 * 8 + 2)


class EvaluateTests(unittest.TestCase):
    def test_gate2_scores_the_planned_pairs(self):
        # Independent of probe.PAIRS, which passing_records() derives from.
        expected = [
            "1024/1025",
            "2048/2049",
            "2816/2817",
            "4096/4097",
            "6144/6145",
            "7680/7681",
        ]
        pairs = probe.evaluate(passing_records())["gate2_no_length_cliff"]["pairs"]
        self.assertEqual(list(pairs), expected)

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

    def test_unmeasured_gates_read_incomplete_not_fail(self):
        records = passing_records()
        records["buckets"] = [r for r in records["buckets"] if r["target"] == 300]
        records["guard"] = "rss guard"
        result = probe.evaluate(records)
        self.assertTrue(result["gate2_no_length_cliff"]["pass"])
        gate3 = result["gate3_faster_than_qwen"]
        self.assertIsNone(gate3["buckets"]["3000"]["pass"])
        self.assertTrue(gate3["buckets"]["300"]["pass"])
        self.assertIsNone(gate3["pass"])
        self.assertEqual(result["guard"], "rss guard")
        self.assertEqual(result["verdict"], "INCOMPLETE")
        # Even with gate 4 measured, missing buckets keep it incomplete.
        self.assertEqual(probe.evaluate(records, {"min": 1.0})["verdict"], "INCOMPLETE")

    def test_a_measured_failure_wins_over_missing_data(self):
        records = passing_records()
        records["buckets"] = [
            {"target": 300, "ms": 400.0}
        ]  # slower than Qwen's 326 ms; 1k and 3k never ran
        result = probe.evaluate(records)
        self.assertIsNone(result["gate3_faster_than_qwen"]["buckets"]["1000"]["pass"])
        self.assertFalse(result["gate3_faster_than_qwen"]["pass"])
        self.assertEqual(result["verdict"], "FAIL")

    def test_no_startup_log_is_unmeasured(self):
        records = passing_records()
        records["startup_log"] = None
        result = probe.evaluate(records)
        self.assertIsNone(result["gate1_attention_on_gpu"]["pass"])
        self.assertEqual(result["verdict"], "INCOMPLETE")

    def test_nothing_measured_is_incomplete(self):
        self.assertEqual(probe.evaluate({})["verdict"], "INCOMPLETE")

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
        if args[0] == "annotate":
            versions = [a.split("=", 1)[1] for a in args if "--resource-version=" in a]
            if versions and versions[0] != self.state["metadata"]["resourceVersion"]:
                raise subprocess.CalledProcessError(1, "kubectl annotate", "conflict")
            version = int(self.state["metadata"]["resourceVersion"])
            self.state["metadata"]["resourceVersion"] = str(version + 1)
            return SimpleNamespace(stdout="")
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
        note=None,
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
                probe.run_window(out, SNAPSHOT, budget_s, start_pod, body, abort, note)
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

    def test_window_json_records_the_rehearsal_note(self):
        run = self.run_window(note={"rehearsal": "skipped (--skip-rehearsal)"})
        self.assertEqual(run.window["rehearsal"], "skipped (--skip-rehearsal)")

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

    def test_production_already_at_zero_is_refused_before_any_write(self):
        deployment = FakeDeployment()
        deployment.state["spec"]["replicas"] = 0
        run = self.run_window(deployment=deployment)
        self.assertIsInstance(run.error, bench.ProductionChanged)
        self.assertEqual(run.deployment.scales, [])
        run.start_pod.assert_not_called()
        run.restore.assert_not_called()
        self.assertTrue(run.window["production_untouched"])

    def test_outage_ends_at_the_restore_not_the_final_delete(self):
        def kube_effect(*args, **kwargs):
            if args[:2] == ("delete", "pod"):
                time.sleep(1.2)

        run = self.run_window(kube_effect=kube_effect)
        self.assertIsNone(run.error)
        self.assertEqual(run.window["outage_seconds"], 0)

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

    def test_probe_create_is_bounded_by_the_time_left(self):
        run = self.run_window(budget_s=30)
        (timeout,) = run.start_pod.call_args.args
        self.assertLessEqual(timeout, 30)
        self.assertGreaterEqual(timeout, 1)
        run = self.run_window(budget_s=600)
        self.assertEqual(run.start_pod.call_args.args, (60,))

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

    def test_hangup_and_quit_after_scale_down_still_restore(self):
        # In a child process: without a handler, these signals' default action
        # kills the process outright, and that must not take the test runner with it.
        for sig in (signal.SIGHUP, signal.SIGQUIT):
            with self.subTest(signal=sig.name):
                script = textwrap.dedent(
                    f"""
                    import json, os, signal, time
                    import test_probe_eg2_1080 as t

                    def body():
                        os.kill(os.getpid(), signal.{sig.name})
                        time.sleep(5)

                    run = t.WindowTests().run_window(body)
                    print(json.dumps(
                        {{"restores": run.restore.call_count, "error": str(run.error)}}
                    ))
                    """
                )
                child = subprocess.run(
                    [sys.executable, "-c", script],
                    cwd=Path(__file__).parent,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,  # the return code is the assertion
                )
                self.assertEqual(child.returncode, 0, child.stderr)
                result = json.loads(child.stdout.strip().splitlines()[-1])
                self.assertEqual(result["restores"], 1)
                self.assertEqual(result["error"], f"interrupted: signal {sig}")


class GuardTests(unittest.TestCase):
    def test_rss_guard_latches_before_startup_returns(self):
        telemetry = "512, 8192, 50, 90, 80\n5767168 5767168\n60000\n0.5 0.4 0.3 1/2 3\n"
        calls = []

        def kube(*args, **kwargs):
            calls.append(args[0])
            return SimpleNamespace(stdout=telemetry if args[0] == "exec" else "")

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(bench, "kube", side_effect=kube),
            patch.object(bench, "http") as http,
        ):
            monitor = bench.Monitor(
                Path(directory) / "t.jsonl",
                "http://unused",
                pod=probe.POD,
                rss_limit_kib=probe.RSS_LIMIT_KIB,
            )
            with self.assertRaisesRegex(RuntimeError, "rss guard"):
                monitor.start()
            monitor.close()
        self.assertEqual(calls, ["exec", "delete"])
        http.assert_not_called()


class StartupLogTests(unittest.TestCase):
    def capture(self, results):
        records = {}
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(probe, "kube", side_effect=results),
            patch.object(probe.time, "sleep"),
        ):
            probe.capture_startup_log(records, Path(directory))
            saved = (Path(directory) / "startup.log").exists()
        return records, saved

    def test_api_error_leaves_gate1_unmeasured(self):
        error = SimpleNamespace(returncode=1, stdout="", stderr="Error from server")
        records, saved = self.capture([error] * 3)
        self.assertIsNone(records["startup_log"])
        self.assertEqual(records["startup_log_error"], "Error from server")
        self.assertFalse(saved)
        self.assertIsNone(probe.evaluate(records)["gate1_attention_on_gpu"]["pass"])

    def test_retry_captures_the_log_without_kubectl_stderr(self):
        error = SimpleNamespace(returncode=1, stdout="", stderr="Error from server")
        ok = SimpleNamespace(
            returncode=0,
            stdout="llama_context: Flash Attention enabled\n",
            stderr="kubectl warning",
        )
        records, saved = self.capture([error, ok])
        self.assertTrue(saved)
        self.assertNotIn("kubectl warning", records["startup_log"])
        self.assertNotIn("startup_log_error", records)
        self.assertTrue(probe.evaluate(records)["gate1_attention_on_gpu"]["pass"])


class RestoreTests(unittest.TestCase):
    def deployment(self, replicas, version, image="qwen"):
        return {
            "metadata": {"uid": "u", "resourceVersion": version},
            "spec": {"template": {"image": image}, "replicas": replicas},
        }

    def restore(self, gets, kube_effect=None, deadline=None):
        # The fence reads the Deployment before the scale step's own read.
        first = next(i for i, g in enumerate(gets) if isinstance(g, dict))
        gets = [*gets[: first + 1], copy.deepcopy(gets[first]), *gets[first + 1 :]]
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

    def test_closed_stdout_never_stops_a_restore(self):
        gets = [
            subprocess.TimeoutExpired("kubectl get", 60),
            self.deployment(0, "7"),
            self.deployment(1, "8"),
        ]
        with patch("builtins.print", side_effect=BrokenPipeError("stdout closed")):
            error, _, kube, restored = self.restore(gets)
        self.assertIsNone(error)
        self.assertTrue(restored)
        self.assertEqual(len(self.scales(kube)), 1)

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

    def restore_fake(self, deployment):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(bench, "obj", side_effect=deployment.obj),
            patch.object(bench, "kube", side_effect=deployment.kube),
            patch.object(bench, "Forward"),
            patch.object(
                bench, "http", return_value={"data": [{"embedding": [1.0] * 2560}]}
            ),
            patch.object(bench, "RETRY_PAUSE_S", 0),
        ):
            bench.restore(SNAPSHOT, Path(directory))
            return (Path(directory) / "restored.json").exists()

    def test_a_timed_out_scale_down_cannot_commit_after_the_restore(self):
        # The scale-down never landed, so production still reads 1 replica at its
        # original version; the stale write is still pending on the API server.
        deployment = FakeDeployment()
        stale = ("scale", "deployment/" + bench.DEPLOY, "--replicas=0")
        stale += (
            "--resource-version=" + deployment.state["metadata"]["resourceVersion"],
        )
        self.assertTrue(self.restore_fake(deployment))
        with self.assertRaises(subprocess.CalledProcessError):
            deployment.kube(*stale)
        self.assertEqual(deployment.state["spec"]["replicas"], 1)

    def test_replica_count_is_verified_after_the_rollout(self):
        gets = [self.deployment(0, "7"), self.deployment(0, "9")]
        error, _, _, restored = self.restore(gets)
        self.assertRegex(str(error), "at 0 after restore")
        self.assertFalse(restored)

    def test_template_changed_during_rollout_is_reported(self):
        gets = [self.deployment(0, "7"), self.deployment(1, "9", image="new")]
        error, _, _, restored = self.restore(gets)
        self.assertRegex(str(error), "changed concurrently")
        self.assertFalse(restored)


def rehearsal_sample(long_3000=False):
    """44 rows; `long_3000` makes the 3,000-target texts tokenize past the EG2 cap."""
    rows = []
    for target, n in ((300, 12), (1000, 10), (3000, 10), (6000, 6), (7600, 6)):
        chars = 9000 if long_3000 and target == 3000 else max(target, 7700)
        rows += [{"target": target, "tokens": target, "text": "a" * chars}] * n
    return rows


class FakeServer:
    """A stand-in for http(): one token per character, plus [2, 1] specials."""

    def __init__(self, over_cap_array=False):
        self.over_cap_array = over_cap_array
        self.sent = []

    def __call__(self, base, path, body=None, timeout=None):
        if path == "/tokenize":
            n = len(body["content"])
            specials = [2, 1] if body["add_special"] else []
            return {"tokens": specials + [10 + i % 900 for i in range(n)]}
        value = body["input"][0]
        self.sent.append(value)
        time.sleep(0.001)  # a measured request never takes 0.0 ms
        tokens = len(value) if isinstance(value, list) else len(value) + 2
        if self.over_cap_array and isinstance(value, list) and tokens >= 2000:
            tokens = probe.CAP + 1
        return {
            "data": [{"embedding": [1.0] * probe.DIMENSION}],
            "usage": {"prompt_tokens": tokens},
        }


GOOD_LOG = "llama_context: Flash Attention enabled\n"


class RehearsalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.name = f"rehearse-test-{os.getpid()}-{id(self)}"
        self.addCleanup(
            shutil.rmtree, Path(probe.__file__).parent / "scratch" / self.name, True
        )
        patcher = patch.object(probe.latency_local, "GAP_S", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def args(self, rows, log=GOOD_LOG, queries=None):
        sample_path = self.root / "sample.json"
        sample_path.write_text(json.dumps(rows))
        log_path = self.root / "server.log"
        log_path.write_text(log)
        query_path = None
        if queries:
            query_path = self.root / "queries.json"
            query_path.write_text(json.dumps({"queries": queries}))
        return SimpleNamespace(
            sample=sample_path,
            queries=query_path,
            query_list=queries,
            output=self.root / self.name,
            server_log=log_path,
            base_url="http://127.0.0.1:1",
        )

    def rehearse(self, rows, server=None, **kwargs):
        args = self.args(rows, **kwargs)
        server = server or FakeServer()
        kept = probe.select_inputs(rows)
        with (
            patch.object(probe, "http", server),
            patch.object(probe, "kube", side_effect=AssertionError("kubectl")),
        ):
            passed = probe.rehearse(args, rows, kept)
        record = json.loads((args.output / "rehearsal.json").read_text())
        failed = {c["name"] for c in record["checks"] if not c["ok"]}
        return passed, record, failed, server, args

    def test_good_fixture_passes_and_records_the_plan_hash(self):
        rows = rehearsal_sample()
        passed, record, failed, server, args = self.rehearse(rows)
        self.assertTrue(passed, record["checks"])
        self.assertEqual(failed, set())
        self.assertEqual(
            record["plan_hash"],
            probe.plan_hash(args, rows, probe.select_inputs(rows)),
        )
        # Every token array that is not special-free starts with the BOS id.
        arrays = [v for v in server.sent if isinstance(v, list)]
        self.assertTrue(arrays and all(v[0] == 2 and v[-1] == 1 for v in arrays))

    def test_missing_fa_line_fails(self):
        passed, _, failed, _, _ = self.rehearse(
            rehearsal_sample(), log="no resolution lines at this verbosity\n"
        )
        self.assertFalse(passed)
        self.assertEqual(failed, {"fa_signal_in_server_log"})

    def test_empty_server_log_leaves_gate_1_unmeasured(self):
        passed, _, failed, _, _ = self.rehearse(rehearsal_sample(), log="")
        self.assertFalse(passed)
        self.assertEqual(failed, {"gates_1_3_measured", "fa_signal_in_server_log"})

    def test_fallback_line_counts_as_the_signal(self):
        passed, _, _, _, _ = self.rehearse(
            rehearsal_sample(), log="Flash Attention not supported, set to disabled\n"
        )
        self.assertTrue(passed)

    def test_empty_bucket_fails(self):
        passed, record, failed, _, _ = self.rehearse(rehearsal_sample(long_3000=True))
        self.assertFalse(passed)
        self.assertIn("every_bucket_has_requests", failed)
        detail = next(
            c for c in record["checks"] if c["name"] == "every_bucket_has_requests"
        )
        self.assertIn("3000", detail["detail"])

    def test_failed_ordering_requests_fail(self):
        server = FakeServer()
        original = server.__call__
        texts = []

        def failing(base, path, body=None, timeout=None):
            if path != "/tokenize" and isinstance(body["input"][0], str):
                texts.append(1)
                # The warm-up and 44 texts x 3 repeats come first; the rest is ordering.
                if len(texts) > 1 + 44 * probe.REPEATS:
                    raise OSError("connection reset")
            return original(base, path, body, timeout)

        passed, _, failed, _, _ = self.rehearse(rehearsal_sample(), server=failing)
        self.assertFalse(passed)
        self.assertEqual(failed, {"ordering_ran_clean"})

    def test_over_cap_request_fails(self):
        passed, _, failed, _, _ = self.rehearse(
            rehearsal_sample(), server=FakeServer(over_cap_array=True)
        )
        self.assertFalse(passed)
        self.assertLessEqual({"no_request_over_cap", "plan_ran_to_the_end"}, failed)

    def test_special_free_array_fails_unless_marked(self):
        def row(key, head, tail, **flags):
            return {
                "key": key,
                "tokens": 16,
                "array": True,
                "head": head,
                "tail": tail,
                "special_free": False,
                "prefix_only": False,
                **flags,
            }

        records = {
            "specials": [2, 1],
            "requests": [
                row("calibrate", 10, 11),
                row("frag-8", 10, 11, special_free=True),
                row("bosfrag-8", 2, 11, prefix_only=True),
                row("first", None, None, array=False),
            ],
        }

        def check():
            checks = {c["name"]: c for c in probe.rehearsal_checks(records, True)}
            return checks["token_arrays_carry_specials"]

        result = check()
        self.assertFalse(result["ok"])
        self.assertIn("calibrate", result["detail"])
        self.assertNotIn("frag-8", result["detail"])
        self.assertNotIn("bosfrag-8", result["detail"])
        records["requests"][0].update(head=2)  # BOS without EOS still fails
        self.assertFalse(check()["ok"])
        records["requests"][0].update(tail=1)
        self.assertTrue(check()["ok"])

    def test_unresolved_specials_fail_instead_of_skipping_the_audit(self):
        records = {
            "specials": [],
            "requests": [
                {
                    "key": "calibrate",
                    "tokens": 16,
                    "array": True,
                    "head": 10,
                    "tail": 11,
                    "special_free": False,
                    "prefix_only": False,
                }
            ],
        }
        checks = {c["name"]: c for c in probe.rehearsal_checks(records, True)}
        self.assertFalse(checks["model_specials_resolved"]["ok"])
        self.assertFalse(checks["token_arrays_carry_specials"]["ok"])

    def test_queries_mode_keeps_its_special_free_fragments_labeled(self):
        rows = rehearsal_sample()
        passed, record, _, server, _ = self.rehearse(
            rows, queries=[{"id": "q1", "text": "what is BUG-1"}]
        )
        self.assertTrue(passed, record["checks"])
        self.assertEqual(record["mode"], "queries")
        sent = [v for v in server.sent if isinstance(v, list)]
        self.assertEqual(len([v for v in sent if v[0] == 2]), 8)
        self.assertEqual(len([v for v in sent if v[0] != 2]), 8)

    def test_verbosity_below_trace_fails_the_check(self):
        args = list(probe.PROBE_ARGS)
        args[args.index("--verbosity") + 1] = "3"
        with patch.object(probe, "PROBE_ARGS", args):
            checks = probe.rehearsal_checks({"requests": []}, True)
        self.assertFalse(
            next(c for c in checks if c["name"] == "probe_verbosity_logs_fa")["ok"]
        )


class RehearsalGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.rows = rehearsal_sample()
        self.sample_path = self.root / "sample.json"
        self.sample_path.write_text(json.dumps(self.rows))
        self.kept = probe.select_inputs(self.rows)
        self.name = f"execute-test-{os.getpid()}-{id(self)}"
        self.addCleanup(
            shutil.rmtree, Path(probe.__file__).parent / "scratch" / self.name, True
        )

    def args(self, **kwargs):
        base = {
            "sample": self.sample_path,
            "queries": None,
            "query_list": None,
            "output": self.root / self.name,
            "rehearsal": None,
            "skip_rehearsal": False,
        }
        return SimpleNamespace(**{**base, **kwargs})

    def rehearsal_file(self, **fields):
        record = {
            "passed": True,
            "plan_hash": probe.plan_hash(self.args(), self.rows, self.kept),
            "checks": [{"name": "fa_signal_in_server_log", "ok": False}],
        }
        path = self.root / "rehearsal.json"
        path.write_text(json.dumps({**record, **fields}))
        return path

    def execute(self, args):
        with (
            patch.object(probe, "preflight", side_effect=RuntimeError("stop")),
            self.assertRaisesRegex(Exception, "stop|rehearsal") as ctx,
        ):
            probe.execute(args, self.rows, self.kept)
        return ctx.exception

    def test_execute_refuses_without_a_rehearsal(self):
        error = self.execute(self.args())
        self.assertIsInstance(error, probe.RehearsalRefused)
        self.assertFalse((self.root / self.name).exists())

    def test_execute_refuses_a_failed_rehearsal(self):
        path = self.rehearsal_file(passed=False)
        error = self.execute(self.args(rehearsal=path))
        self.assertIsInstance(error, probe.RehearsalRefused)
        self.assertIn("fa_signal_in_server_log", str(error))

    def test_execute_refuses_a_mismatched_plan_hash(self):
        path = self.rehearsal_file(plan_hash="0" * 64)
        error = self.execute(self.args(rehearsal=path))
        self.assertIsInstance(error, probe.RehearsalRefused)
        self.assertIn("plan hash", str(error))

    def test_a_changed_request_setting_invalidates_the_rehearsal(self):
        path = self.rehearsal_file()
        with patch.object(probe, "PREFIX_MARGIN", 500):
            error = self.execute(self.args(rehearsal=path))
        self.assertIsInstance(error, probe.RehearsalRefused)

    def test_a_changed_sample_invalidates_the_rehearsal(self):
        path = self.rehearsal_file()
        self.sample_path.write_text(json.dumps(self.rows) + " ")
        error = self.execute(self.args(rehearsal=path))
        self.assertIsInstance(error, probe.RehearsalRefused)

    def test_matching_rehearsal_proceeds_to_preflight(self):
        path = self.rehearsal_file()
        error = self.execute(self.args(rehearsal=path))
        self.assertNotIsInstance(error, probe.RehearsalRefused)
        provenance = json.loads((self.root / self.name / "provenance.json").read_text())
        self.assertEqual(provenance["rehearsal"], str(path))

    def test_skip_rehearsal_proceeds_and_is_recorded(self):
        error = self.execute(self.args(skip_rehearsal=True))
        self.assertNotIsInstance(error, probe.RehearsalRefused)
        provenance = json.loads((self.root / self.name / "provenance.json").read_text())
        self.assertIn("skipped", provenance["rehearsal"])
        self.assertIn(
            "skipped",
            probe.require_rehearsal(
                self.args(skip_rehearsal=True), self.rows, self.kept
            )["rehearsal"],
        )


if __name__ == "__main__":
    unittest.main()
