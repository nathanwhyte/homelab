"""Offline checks for the experiment's production restoration boundary."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import benchmark_1080_slots as bench


class RestorationTests(unittest.TestCase):
    def test_restore_attempted_when_probe_delete_fails(self):
        with (
            patch.object(bench, "kube", side_effect=TimeoutError("delete timeout")),
            patch.object(bench, "restore") as restore,
        ):
            with self.assertRaises(TimeoutError):
                bench.cleanup_restore({"snapshot": True}, Path("unused"))
            restore.assert_called_once_with({"snapshot": True}, Path("unused"))

    def test_concurrent_template_change_is_not_overwritten(self):
        snapshot = {
            "metadata": {"uid": "original"},
            "spec": {"template": {"image": "original"}, "replicas": 1},
        }
        changed = {
            "metadata": {"uid": "original"},
            "spec": {"template": {"image": "new"}, "replicas": 0},
        }
        with (
            patch.object(bench, "obj", return_value=changed),
            patch.object(bench, "kube") as kube,
        ):
            with self.assertRaisesRegex(RuntimeError, "changed concurrently"):
                bench.restore(snapshot, Path("unused"))
            kube.assert_not_called()

    def test_restoration_requires_valid_production_vector(self):
        snapshot = {
            "metadata": {"uid": "original"},
            "spec": {"template": {}, "replicas": 1},
        }
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(bench, "obj", return_value=snapshot),
            patch.object(bench, "kube"),
            patch.object(bench, "Forward") as forward,
            patch.object(
                bench, "http", return_value={"data": [{"embedding": [1.0] * 1024}]}
            ),
        ):
            with self.assertRaises(ValueError):
                bench.restore(snapshot, Path(directory))
            self.assertFalse((Path(directory) / "restored.json").exists())
            forward.return_value.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
