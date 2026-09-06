#!/usr/bin/env python3
"""Hermetic backup freshness tests; never access credentials or R2."""
import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("exporter", Path(__file__).with_name("backup-freshness-exporter.py"))
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)


class FreshnessTests(unittest.TestCase):
    def test_dump_required_not_newer_sidecar_or_empty_dump(self):
        objects = [
            {"Path": "2026-09-01/postgres/coach/old.pgdump", "Size": 10, "ModTime": "2026-09-01T00:00:00Z"},
            {"Path": "2026-09-06/postgres/coach/new.pgdump.sha256", "Size": 10, "ModTime": "2026-09-06T00:00:00Z"},
            {"Path": "2026-09-06/postgres/coach/new.pgdump", "Size": 0, "ModTime": "2026-09-06T00:00:00Z"},
        ]
        self.assertEqual(exporter.newest_modtime(objects, "postgres/coach/"), exporter._parse_rfc3339("2026-09-01T00:00:00Z"))
        self.assertIsNone(exporter.newest_modtime(objects, "postgres/glossary/"))

    def test_declared_database_without_any_job_or_object_is_infinite(self):
        with patch.object(exporter, "list_backup_tree", return_value=[]):
            body = exporter.render(*exporter.collect())
        for entry in exporter.DECLARED:
            self.assertIn(f'backup_freshness_seconds{{app="{entry["app"]}"}} +Inf', body)
        self.assertIn("backup_freshness_up 1", body)

    def test_listing_failure_withholds_database_series(self):
        with patch.object(exporter, "list_backup_tree", side_effect=RuntimeError("credential-placeholder")):
            body = exporter.render(*exporter.collect())
        self.assertIn("backup_freshness_up 0", body)
        self.assertNotIn("backup_freshness_seconds{", body)
        self.assertNotIn("credential-placeholder", body)

    def test_age_advances_while_last_run_stays_frozen(self):
        newest = {entry["app"]: 10 for entry in exporter.DECLARED}
        body = exporter.render(newest, 20, now=100)
        self.assertIn('backup_freshness_seconds{app="coach"} 90.000', body)
        self.assertIn("backup_freshness_last_run_timestamp_seconds 20.000", body)

    def test_malformed_or_timeout_is_unhealthy(self):
        for stdout in ("{}", "null", "", '[{"Path":"x"}]'):
            with self.subTest(stdout=stdout), patch.object(exporter.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout=stdout)):
                self.assertIsNone(exporter.collect()[0])
        with patch.object(exporter.subprocess, "run", side_effect=subprocess.TimeoutExpired("rclone", 120)):
            self.assertIsNone(exporter.collect()[0])


if __name__ == "__main__":
    unittest.main()
