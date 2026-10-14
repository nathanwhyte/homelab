"""Offline checks for the capped EmbeddingGemma 2 probe: manifest, cap, gates, restore."""

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

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


class WindowTests(unittest.TestCase):
    def run_window(self, body, budget_s=60):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(probe, "kube") as kube,
            patch.object(probe, "cleanup_restore") as restore,
        ):
            out = Path(directory)
            error = None
            try:
                probe.run_window(out, {"snapshot": True}, budget_s, body)
            except RuntimeError as exc:
                error = exc
            window = json.loads((out / "window.json").read_text())
            return error, window, kube, restore, out

    def test_failure_in_measurements_restores_once(self):
        def body():
            raise RuntimeError("probe broke")

        error, window, kube, restore, out = self.run_window(body)
        self.assertEqual(str(error), "probe broke")
        restore.assert_called_once_with({"snapshot": True}, out, pod=probe.POD)
        self.assertEqual(window["error"]["error"], "probe broke")
        self.assertIn("restored_at", window)
        kube.assert_any_call("scale", "deployment/" + probe.DEPLOY, "--replicas=0")

    def test_deadline_stops_measurements_and_restores(self):
        error, window, _, restore, _ = self.run_window(lambda: time.sleep(5), 1)
        self.assertEqual(str(error), "deadline reached")
        restore.assert_called_once()
        self.assertIn("restored_at", window)

    def test_success_restores_once(self):
        error, window, _, restore, _ = self.run_window(lambda: None)
        self.assertIsNone(error)
        restore.assert_called_once()
        self.assertNotIn("error", window)


if __name__ == "__main__":
    unittest.main()
