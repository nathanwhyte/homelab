#!/usr/bin/env python3
"""Unit tests for ovlock-janitor.py (IMPR-1095). Stubbed S3; the only socket
is a loopback listener for fetch_presence_ages' error path. Offline."""

from __future__ import annotations

import argparse
import contextlib
import datetime
import http.client
import importlib.util
import io
import os
import socket
import sys
import threading
import unittest

_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_DIR, "..", "exporters"))
_spec = importlib.util.spec_from_file_location(
    "ovlock_janitor", os.path.join(_DIR, "ovlock-janitor.py")
)
janitor = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(janitor)
s3lite = sys.modules["s3lite"]


def _ts(seconds_ago: float) -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        seconds=seconds_ago
    )


class StubS3(s3lite.S3Client):
    """In-memory stand-in; `listings` is consumed one snapshot per list call.

    get_object_meta returns the key's mtime from the most recent listing
    (i.e. "unchanged since pass 2") unless `reacquired` overrides it or
    `vanished` makes the pre-delete verify fail.
    """

    def __init__(
        self,
        listings,
        bodies=None,
        fail_delete=frozenset(),
        reacquired=None,
        vanished=frozenset(),
    ):
        self.listings = list(listings)
        self.bodies = bodies or {}
        self.fail_delete = fail_delete
        self.reacquired = reacquired or {}
        self.vanished = vanished
        self.deleted: list[str] = []
        self.last_snapshot: dict = {}

    def list_objects(self, bucket):
        self.last_snapshot = self.listings.pop(0)
        return [
            {"Key": k, "LastModified": m, "Size": 58, "ETag": '"x"'}
            for k, m in self.last_snapshot.items()
        ]

    def get_object(self, bucket, key):
        return self.bodies.get(key, b"handle-1:123456789:T")

    def get_object_meta(self, bucket, key):
        if key in self.vanished:
            raise s3lite.S3Error(f"GET {key}: HTTP 404")
        mtime = self.reacquired.get(key, self.last_snapshot.get(key))
        # Header precision is whole seconds
        return self.get_object(bucket, key), mtime.replace(microsecond=0)

    def delete_object(self, bucket, key):
        if key in self.fail_delete:
            raise s3lite.S3Error(f"boom {key}")
        self.deleted.append(key)


def make_args(**overrides) -> argparse.Namespace:
    defaults = dict(
        apply=False,
        bucket="test-bucket",
        health_url="http://ov/health",
        pass_interval=0.0,
        dead_threshold=1800.0,
        max_delete=100,
        exporter_url="http://ov:9210/metrics",
        wedge_threshold=2700.0,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class TestIsLockKey(unittest.TestCase):
    def test_patterns(self):
        self.assertTrue(s3lite.is_lock_key("default/resources/a/.path.ovlock"))
        self.assertTrue(s3lite.is_lock_key("default/resources/.exact.ovlock.name.ab12"))
        self.assertFalse(s3lite.is_lock_key("default/resources/a/file.md"))
        self.assertFalse(s3lite.is_lock_key("default/.path.ovlock.bak"))
        self.assertFalse(s3lite.is_lock_key("default/notovlock/data.json"))


class TestClassify(unittest.TestCase):
    def test_matrix(self):
        now = datetime.datetime.now(datetime.timezone.utc).timestamp()
        pass1 = {
            "advancing": _ts(60),
            "frozen-old": _ts(3600),
            "frozen-young": _ts(300),
            "vanished": _ts(3600),
        }
        pass2 = {
            "advancing": _ts(10),
            "frozen-old": pass1["frozen-old"],
            "frozen-young": pass1["frozen-young"],
            "appeared": _ts(5),
        }
        dead, live, fresh = janitor.classify(pass1, pass2, now, 1800.0)
        self.assertEqual(dead, ["frozen-old"])
        self.assertEqual(live, ["advancing"])
        self.assertEqual(fresh, ["frozen-young"])


class TestRun(unittest.TestCase):
    def _run(self, args, client, healthy=(True, True), presence=None):
        health_iter = iter(healthy)
        original = janitor.ov_healthy
        janitor.ov_healthy = lambda url, timeout=10.0: next(health_iter)
        try:
            return janitor.run(
                args,
                client,
                sleep_fn=lambda s: None,
                presence_fn=lambda url: presence,
            )
        finally:
            janitor.ov_healthy = original

    def test_health_gate_pass1(self):
        client = StubS3([])
        rc = self._run(make_args(), client, healthy=(False,))
        self.assertEqual(rc, 0)
        self.assertEqual(client.listings, [])  # never listed

    def test_health_gate_pass2_deletes_nothing(self):
        snap = {"a/.path.ovlock": _ts(3600)}
        client = StubS3([snap])
        rc = self._run(make_args(apply=True), client, healthy=(True, False))
        self.assertEqual(rc, 0)
        self.assertEqual(client.deleted, [])

    def test_dry_run_never_deletes(self):
        snap = {"a/.path.ovlock": _ts(3600)}
        client = StubS3([snap, dict(snap)])
        rc = self._run(make_args(apply=False), client)
        self.assertEqual(rc, 0)
        self.assertEqual(client.deleted, [])

    def test_apply_deletes_only_dead(self):
        pass1 = {
            "dead/.path.ovlock": _ts(3600),
            "live/.path.ovlock": _ts(60),
            "fresh/.path.ovlock": _ts(600),
        }
        pass2 = dict(pass1, **{"live/.path.ovlock": _ts(1)})
        client = StubS3([pass1, pass2])
        rc = self._run(make_args(apply=True), client)
        self.assertEqual(rc, 0)
        self.assertEqual(client.deleted, ["dead/.path.ovlock"])

    def test_max_delete_cap(self):
        snap = {f"d{i}/.path.ovlock": _ts(3600 + i) for i in range(5)}
        client = StubS3([snap, dict(snap)])
        rc = self._run(make_args(apply=True, max_delete=2), client)
        self.assertEqual(rc, 0)
        self.assertEqual(len(client.deleted), 2)

    def test_delete_failure_exits_nonzero(self):
        snap = {"a/.path.ovlock": _ts(3600), "b/.path.ovlock": _ts(3600)}
        client = StubS3([snap, dict(snap)], fail_delete={"a/.path.ovlock"})
        rc = self._run(make_args(apply=True), client)
        self.assertEqual(rc, 1)
        self.assertEqual(client.deleted, ["b/.path.ovlock"])

    def test_empty_bucket_short_circuits(self):
        client = StubS3([{}])
        rc = self._run(make_args(), client)
        self.assertEqual(rc, 0)
        self.assertEqual(client.listings, [])  # no second pass

    def test_reacquired_between_pass2_and_delete_is_skipped(self):
        snap = {"a/.path.ovlock": _ts(3600), "b/.path.ovlock": _ts(3600)}
        client = StubS3([snap, dict(snap)], reacquired={"a/.path.ovlock": _ts(0)})
        rc = self._run(make_args(apply=True), client)
        self.assertEqual(rc, 0)
        self.assertEqual(client.deleted, ["b/.path.ovlock"])

    def test_vanished_before_delete_is_skipped(self):
        snap = {"a/.path.ovlock": _ts(3600)}
        client = StubS3([snap, dict(snap)], vanished={"a/.path.ovlock"})
        rc = self._run(make_args(apply=True), client)
        self.assertEqual(rc, 0)
        self.assertEqual(client.deleted, [])

    def test_mtime_matches_tolerates_subsecond_only(self):
        base = _ts(1800)
        self.assertTrue(janitor._mtime_matches(base.replace(microsecond=0), base))
        self.assertFalse(
            janitor._mtime_matches(base + datetime.timedelta(seconds=30), base)
        )

    def test_malformed_token_still_classified(self):
        snap = {"a/.path.ovlock": _ts(3600)}
        client = StubS3([snap, dict(snap)], bodies={"a/.path.ovlock": b"garbage"})
        rc = self._run(make_args(apply=True), client)
        self.assertEqual(rc, 0)
        self.assertEqual(client.deleted, ["a/.path.ovlock"])


WEDGED_KEY = "default/user/noot/sessions/mcp-store-e71a7b670fca/.path.ovlock"
WEDGED_BODY = b"9959d01e-e133-4e46-8074-04cc1de18b00:1791829176675880761:E"

EXPORTER_TEXT = (
    "# HELP openviking_s3_ovlock_live_age_seconds Presence age ...\n"
    "# TYPE openviking_s3_ovlock_live_age_seconds gauge\n"
    f'openviking_s3_ovlock_live_age_seconds{{key="{WEDGED_KEY}",'
    'uri="viking://user/noot/sessions/mcp-store-e71a7b670fca",'
    'owner="9959d01e-e133-4e46-8074-04cc1de18b00",lock_type="E"} 252000.0\n'
    'openviking_s3_ovlock_live_age_seconds{key="default/resources/q\\"x/.path.ovlock",'
    'uri="viking://resources/q\\"x",owner="h",lock_type="T"} 1200.5\n'
    "openviking_s3_ovlock_oldest_live_age_seconds 252000.0\n"
    'openviking_queue_pending{queue="total"} 4\n'
)


class TestParsePresenceAges(unittest.TestCase):
    def test_reads_only_the_per_lock_series(self):
        self.assertEqual(
            janitor.parse_presence_ages(EXPORTER_TEXT),
            {WEDGED_KEY: 252000.0, 'default/resources/q"x/.path.ovlock': 1200.5},
        )

    def test_empty_and_malformed(self):
        self.assertEqual(janitor.parse_presence_ages(""), {})
        text = (
            'openviking_s3_ovlock_live_age_seconds{key="a"} notanumber\n'
            'openviking_s3_ovlock_live_age_seconds{owner="o"} 5\n'
            'openviking_s3_ovlock_live_age_seconds{key="b"\n'
        )
        self.assertEqual(janitor.parse_presence_ages(text), {})

    def test_parse_labels_unescapes(self):
        self.assertEqual(
            janitor._parse_labels('a="x\\\\y",b="q\\"r",c="l\\nm"'),
            {"a": "x\\y", "b": 'q"r', "c": "l\nm"},
        )


def _serve_once(payload: bytes) -> tuple[str, threading.Thread]:
    """A loopback listener that answers one request with `payload` raw."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def answer():
        with srv:
            conn, _ = srv.accept()
            with conn:
                conn.recv(4096)
                conn.sendall(payload)

    thread = threading.Thread(target=answer, daemon=True)
    thread.start()
    return f"http://127.0.0.1:{srv.getsockname()[1]}/metrics", thread


class TestFetchPresenceAges(unittest.TestCase):
    """The real fetch path: no response shape may raise out of it."""

    def _fetch(self, payload: bytes):
        url, thread = _serve_once(payload)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            got = janitor.fetch_presence_ages(url, timeout=5.0)
        thread.join(5)
        return got, out.getvalue()

    def test_bad_status_line_is_none(self):
        got, out = self._fetch(b"GARBAGE\r\n\r\n")
        self.assertIsNone(got)
        self.assertIn("BadStatusLine", out)

    def test_truncated_body_is_none(self):
        got, out = self._fetch(b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\nshort")
        self.assertIsNone(got)
        self.assertIn("IncompleteRead", out)

    def test_good_response_parses(self):
        body = EXPORTER_TEXT.encode()
        got, _ = self._fetch(
            b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body) + body
        )
        self.assertEqual(got[WEDGED_KEY], 252000.0)


class TestDescribeLive(unittest.TestCase):
    def test_wedged(self):
        line, wedged = janitor.describe_live(
            WEDGED_KEY, WEDGED_BODY.decode(), 252000.0, 2700.0
        )
        self.assertTrue(wedged)
        self.assertTrue(line.startswith("WEDGED live lock, present >= 2700s"))
        self.assertIn("never deleted", line)
        for part in (
            f": {WEDGED_KEY} ",
            "uri=viking://user/noot/sessions/mcp-store-e71a7b670fca",
            "owner=9959d01e-e133-4e46-8074-04cc1de18b00",
            "lock_type=E",
            "presence_age=252000s",
        ):
            self.assertIn(part, line)

    def test_below_threshold_keeps_legacy_prefix(self):
        line, wedged = janitor.describe_live(WEDGED_KEY, "h:1:T", 1200.0, 2700.0)
        self.assertFalse(wedged)
        self.assertTrue(
            line.startswith(
                f"live-refreshing (heal/operator territory, never deleted): {WEDGED_KEY} "
            )
        )
        self.assertIn("presence_age=1200s", line)

    def test_unknown_age_is_never_wedged(self):
        line, wedged = janitor.describe_live(
            WEDGED_KEY, "(unexpected body: 'x')", None, 2700.0
        )
        self.assertFalse(wedged)
        self.assertIn("owner=unknown lock_type=unknown presence_age=unknown", line)


class TestWedgeReportInRun(unittest.TestCase):
    """The wedge report never changes what is deleted (BUG-1220 invariant)."""

    def _run(self, args, client, presence, presence_fn=None):
        original = janitor.ov_healthy
        janitor.ov_healthy = lambda url, timeout=10.0: True
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                rc = janitor.run(
                    args,
                    client,
                    sleep_fn=lambda s: None,
                    presence_fn=presence_fn or (lambda url: presence),
                )
        finally:
            janitor.ov_healthy = original
        return rc, out.getvalue()

    def _client(self):
        pass1 = {WEDGED_KEY: _ts(20), "dead/.path.ovlock": _ts(3600)}
        pass2 = dict(pass1, **{WEDGED_KEY: _ts(1)})
        return StubS3([pass1, pass2], bodies={WEDGED_KEY: WEDGED_BODY})

    def test_wedged_live_lock_reported_and_never_deleted(self):
        client = self._client()
        rc, out = self._run(make_args(apply=True), client, {WEDGED_KEY: 252000.0})
        self.assertEqual(rc, 0)
        self.assertEqual(client.deleted, ["dead/.path.ovlock"])
        self.assertIn("WEDGED live lock, present >= 2700s", out)
        self.assertIn("owner=9959d01e-e133-4e46-8074-04cc1de18b00", out)
        self.assertIn("wedge report: 1 of 1 live lock(s) present >= 2700s", out)

    def test_exporter_unreachable_degrades_report_only(self):
        client = self._client()
        rc, out = self._run(make_args(apply=True), client, None)
        self.assertEqual(rc, 0)
        self.assertEqual(client.deleted, ["dead/.path.ovlock"])
        self.assertIn("presence_age=unknown", out)
        self.assertIn("wedge report: 0 of 1 live lock(s)", out)

    def test_raising_presence_fn_still_runs_deletion_pass(self):
        client = self._client()

        def boom(url):
            raise http.client.IncompleteRead(b"")

        rc, out = self._run(make_args(apply=True), client, None, presence_fn=boom)
        self.assertEqual(rc, 0)
        self.assertEqual(client.deleted, ["dead/.path.ovlock"])
        self.assertIn("presence_age=unknown", out)

    def test_missing_series_on_reachable_exporter_is_not_exported(self):
        client = self._client()
        rc, out = self._run(make_args(apply=True), client, {})
        self.assertEqual(rc, 0)
        self.assertEqual(client.deleted, ["dead/.path.ovlock"])
        self.assertIn("presence_age=not-exported", out)
        self.assertIn("wedge report: 1 live lock(s) have no ov-exporter series", out)

    def test_no_live_locks_skips_exporter(self):
        snap = {"dead/.path.ovlock": _ts(3600)}
        client = StubS3([snap, dict(snap)])
        calls = []
        original = janitor.ov_healthy
        janitor.ov_healthy = lambda url, timeout=10.0: True
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                janitor.run(
                    make_args(),
                    client,
                    sleep_fn=lambda s: None,
                    presence_fn=lambda url: calls.append(url) or {},
                )
        finally:
            janitor.ov_healthy = original
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
