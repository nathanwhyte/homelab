#!/usr/bin/env python3
"""Hermetic backup freshness tests; never access credentials or R2."""

import http.client
import importlib.util
from pathlib import Path
import subprocess
import threading
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "exporter", Path(__file__).with_name("backup-freshness-exporter.py")
)
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)


class FreshnessTests(unittest.TestCase):
    def test_dump_required_not_newer_sidecar_or_empty_dump(self):
        objects = [
            {
                "Path": "2026-09-01/postgres/coach/old.pgdump",
                "Size": 10,
                "ModTime": "2026-09-01T00:00:00Z",
            },
            {
                "Path": "2026-09-06/postgres/coach/new.pgdump.sha256",
                "Size": 10,
                "ModTime": "2026-09-06T00:00:00Z",
            },
            {
                "Path": "2026-09-06/postgres/coach/new.pgdump",
                "Size": 0,
                "ModTime": "2026-09-06T00:00:00Z",
            },
        ]
        self.assertEqual(
            exporter.newest_modtime(objects, "postgres/coach/"),
            exporter._parse_rfc3339("2026-09-01T00:00:00Z"),
        )
        self.assertIsNone(exporter.newest_modtime(objects, "postgres/absent-app/"))

    def test_declared_database_without_any_job_or_object_is_infinite(self):
        with patch.object(exporter, "list_backup_tree", return_value=[]):
            body = exporter.render(*exporter.collect())
        for entry in exporter.DECLARED:
            self.assertIn(
                f'backup_freshness_seconds{{app="{entry["app"]}"}} +Inf', body
            )
        self.assertIn("backup_freshness_up 1", body)

    def test_listing_failure_withholds_database_series(self):
        with patch.object(
            exporter,
            "list_backup_tree",
            side_effect=RuntimeError("credential-placeholder"),
        ):
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
            with (
                self.subTest(stdout=stdout),
                patch.object(
                    exporter.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0, stdout=stdout),
                ),
            ):
                self.assertIsNone(exporter.collect()[0])
        with patch.object(
            exporter.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired("rclone", 120),
        ):
            self.assertIsNone(exporter.collect()[0])


class HandlerTests(unittest.TestCase):
    def _start_server(self, state, lock):
        server = exporter.ThreadingHTTPServer(
            ("127.0.0.1", 0), exporter._make_handler(state, lock)
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def test_metrics_matches_render_of_the_refreshed_sample(self):
        state = [None, 0.0]
        lock = threading.Lock()
        sample = ({entry["app"]: 42.0 for entry in exporter.DECLARED}, 99.0)
        with patch.object(exporter, "collect", return_value=sample):
            exporter._refresh_once(state, lock)
        server = self._start_server(state, lock)
        conn = http.client.HTTPConnection(*server.server_address, timeout=5)
        try:
            # render()'s default `now` is time.time() at call time, so pin it
            # for both the handler's render and this assertion's expectation.
            with patch.object(exporter.time, "time", return_value=123.0):
                conn.request("GET", "/metrics")
                response = conn.getresponse()
                body = response.read()
                expected = exporter.render(*state)
            self.assertEqual(response.status, 200)
            self.assertEqual(
                response.getheader("Content-Type"), "text/plain; version=0.0.4"
            )
            self.assertEqual(body.decode(), expected)
        finally:
            conn.close()

    def test_healthz_is_ok_and_unknown_path_is_404(self):
        server = self._start_server([None, 0.0], threading.Lock())
        conn = http.client.HTTPConnection(*server.server_address, timeout=5)
        try:
            conn.request("GET", "/healthz")
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"ok\n")

            conn.request("GET", "/nope")
            response = conn.getresponse()
            response.read()
            self.assertEqual(response.status, 404)
        finally:
            conn.close()


class RefreshTests(unittest.TestCase):
    def test_last_run_advances_on_success_and_failure(self):
        state = [None, 0.0]
        lock = threading.Lock()
        good = ({entry["app"]: 1.0 for entry in exporter.DECLARED}, 10.0)
        with patch.object(exporter, "collect", side_effect=[good, (None, 20.0)]):
            exporter._refresh_once(state, lock)
            self.assertEqual(state, list(good))
            exporter._refresh_once(state, lock)
        self.assertEqual(state, [None, 20.0])

    def test_failed_listing_sets_up_zero_and_omits_per_app_series(self):
        state = [None, 0.0]
        lock = threading.Lock()
        with patch.object(exporter, "collect", return_value=(None, 5.0)):
            exporter._refresh_once(state, lock)
        body = exporter.render(*state)
        self.assertIn("backup_freshness_up 0", body)
        self.assertNotIn("backup_freshness_seconds{", body)

    def test_reader_never_observes_a_torn_state(self):
        # Each sample pairs `newest` values with a matching `last_run`, so a
        # reader that raced the lock and saw one sample's dict alongside
        # another's timestamp would be caught below.
        state = [None, 0.0]
        lock = threading.Lock()
        samples = [
            ({entry["app"]: float(i) for entry in exporter.DECLARED}, float(i))
            for i in range(1, 30)
        ]
        torn_reads = []
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                with lock:
                    newest, last_run = state[0], state[1]
                if newest is not None and any(v != last_run for v in newest.values()):
                    torn_reads.append((newest, last_run))

        reader_thread = threading.Thread(target=reader)
        reader_thread.start()
        try:
            with patch.object(exporter, "collect", side_effect=samples):
                for _ in samples:
                    exporter._refresh_once(state, lock)
        finally:
            stop.set()
            reader_thread.join()
        self.assertEqual(torn_reads, [])


if __name__ == "__main__":
    unittest.main()
