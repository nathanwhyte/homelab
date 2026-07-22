#!/usr/bin/env python3
"""Unit tests for ovlock-janitor.py (IMPR-1095). Pure logic — stubbed S3."""

from __future__ import annotations

import argparse
import datetime
import importlib.util
import os
import sys
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
    def _run(self, args, client, healthy=(True, True)):
        health_iter = iter(healthy)
        original = janitor.ov_healthy
        janitor.ov_healthy = lambda url, timeout=10.0: next(health_iter)
        try:
            return janitor.run(args, client, sleep_fn=lambda s: None)
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
        client = StubS3(
            [snap, dict(snap)], reacquired={"a/.path.ovlock": _ts(0)}
        )
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


if __name__ == "__main__":
    unittest.main()
