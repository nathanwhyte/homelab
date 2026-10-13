#!/usr/bin/env python3
"""Unit tests for ov-exporter.py's S3 lock gauges and s3lite's lock parsing.

BUG-1220: the wedge alert must name the wedged lock. Pure logic: stubbed S3,
pinned clock, no network.

    python3 viking/exporters/test_ov_exporter.py
"""

from __future__ import annotations

import datetime
import importlib.util
import os
import sys
import unittest

_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _DIR)
_spec = importlib.util.spec_from_file_location(
    "ov_exporter", os.path.join(_DIR, "ov-exporter.py")
)
exporter = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(exporter)
s3lite = sys.modules["s3lite"]

T0 = 1_800_000_000.0
WEDGED_KEY = "default/user/noot/sessions/mcp-store-e71a7b670fca/.path.ovlock"
WEDGED_BODY = b"9959d01e-e133-4e46-8074-04cc1de18b00:1791829176675880761:E"


def _dt(ts: float) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(ts, datetime.UTC)


class StubS3:
    """`objects` maps key -> mtime (unix); `bodies` key -> bytes or Exception."""

    def __init__(self, objects=None, bodies=None):
        self.objects = dict(objects or {})
        self.bodies = dict(bodies or {})
        self.gets: list[str] = []

    def list_objects(self, bucket):
        return [
            {"Key": k, "LastModified": _dt(m), "Size": 58, "ETag": '"x"'}
            for k, m in self.objects.items()
        ]

    def get_object(self, bucket, key):
        self.gets.append(key)
        body = self.bodies.get(key, b"handle-1:1:T")
        if isinstance(body, Exception):
            raise body
        return body


class TestLockTarget(unittest.TestCase):
    def test_tree_lock_on_directory(self):
        self.assertEqual(
            s3lite.lock_target("default/resources/a/b/.path.ovlock"),
            "viking://resources/a/b",
        )

    def test_the_bug_1220_lock(self):
        self.assertEqual(
            s3lite.lock_target(WEDGED_KEY),
            "viking://user/noot/sessions/mcp-store-e71a7b670fca",
        )

    def test_exact_lock_on_file_drops_hash_keeps_dotted_name(self):
        key = (
            "default/user/noot-pilot/sessions/cc-1/history/archive_008/"
            ".exact.ovlock.memory_diff.json.ca23edf1537c1673"
        )
        self.assertEqual(
            s3lite.lock_target(key),
            "viking://user/noot-pilot/sessions/cc-1/history/archive_008/memory_diff.json",
        )

    def test_exact_lock_without_hash_segment(self):
        self.assertEqual(
            s3lite.lock_target("default/resources/.exact.ovlock.done"),
            "viking://resources/done",
        )

    def test_non_lock_key(self):
        self.assertEqual(s3lite.lock_target("default/resources/a/file.md"), "")


class TestParseLockToken(unittest.TestCase):
    def test_valid_bytes(self):
        self.assertEqual(
            s3lite.parse_lock_token(WEDGED_BODY),
            {
                "owner": "9959d01e-e133-4e46-8074-04cc1de18b00",
                "time_ns": 1791829176675880761,
                "lock_type": "E",
            },
        )

    def test_valid_str_with_whitespace(self):
        token = s3lite.parse_lock_token("  h-1:42:T\n")
        self.assertEqual(token, {"owner": "h-1", "time_ns": 42, "lock_type": "T"})

    def test_malformed(self):
        for body in (
            b"",
            b"garbage",
            b"a:1",
            b"a:1:X",
            b"a:notint:T",
            b":1:T",
            b"a:1:T:z",
        ):
            with self.subTest(body=body):
                self.assertIsNone(s3lite.parse_lock_token(body))


class TestLabelFormatting(unittest.TestCase):
    def test_escape(self):
        self.assertEqual(exporter.escape_label_value('a\\b"c\nd'), 'a\\\\b\\"c\\nd')

    def test_format_labels_order_and_escape(self):
        self.assertEqual(
            exporter.format_labels({"key": 'x"y', "owner": "o"}),
            '{key="x\\"y",owner="o"}',
        )

    def test_format_labels_empty(self):
        self.assertEqual(exporter.format_labels({}), "")

    def test_lock_labels_without_token(self):
        self.assertEqual(
            exporter.lock_labels("default/resources/a/.path.ovlock", None),
            {
                "key": "default/resources/a/.path.ovlock",
                "uri": "viking://resources/a",
                "owner": "unknown",
                "lock_type": "unknown",
            },
        )


class TestSelectLabelCandidates(unittest.TestCase):
    def test_min_age_and_top_n_and_order(self):
        first_seen = {
            "young": T0 - 100,
            "b-old": T0 - 5000,
            "a-old": T0 - 5000,
            "older": T0 - 9000,
            "mid": T0 - 1000,
        }
        got = exporter.select_label_candidates(first_seen, T0, top_n=3, min_age=900)
        self.assertEqual(got, [("older", 9000.0), ("a-old", 5000.0), ("b-old", 5000.0)])

    def test_nothing_past_min_age(self):
        got = exporter.select_label_candidates({"k": T0 - 10}, T0, 3, 900)
        self.assertEqual(got, [])

    def test_top_n_zero_disables(self):
        got = exporter.select_label_candidates({"k": T0 - 9999}, T0, 0, 0)
        self.assertEqual(got, [])


class TestPollS3Locks(unittest.TestCase):
    def setUp(self):
        exporter._s3_first_seen.clear()
        exporter._s3_token_cache.clear()
        exporter._metrics["openviking_s3_ovlock_live_age_seconds"] = []
        self._orig_time = exporter.time.time
        self.now = T0
        exporter.time.time = lambda: self.now

    def tearDown(self):
        exporter.time.time = self._orig_time

    def _poll(self, client, ov_up=True):
        return exporter.poll_s3_locks(
            client, "b", 120.0, 1800.0, ov_up=ov_up, top_n=3, min_age=900.0
        )

    def _series(self):
        return exporter._metrics["openviking_s3_ovlock_live_age_seconds"]

    def test_wedged_lock_is_named_once_past_min_age(self):
        dead = "default/resources/old/.path.ovlock"
        client = StubS3(
            {WEDGED_KEY: self.now - 5, dead: self.now - 7200},
            {WEDGED_KEY: WEDGED_BODY},
        )
        self.assertTrue(self._poll(client))
        self.assertEqual(self._series(), [])  # present 0s: below min age
        self.assertEqual(client.gets, [])  # no GET until it qualifies

        self.now += 3000
        client.objects[WEDGED_KEY] = self.now - 8  # lease refreshed
        self.assertTrue(self._poll(client))
        self.assertEqual(
            self._series(),
            [
                (
                    {
                        "key": WEDGED_KEY,
                        "uri": "viking://user/noot/sessions/mcp-store-e71a7b670fca",
                        "owner": "9959d01e-e133-4e46-8074-04cc1de18b00",
                        "lock_type": "E",
                    },
                    3000.0,
                )
            ],
        )
        self.assertEqual(
            exporter._metrics["openviking_s3_ovlock_oldest_live_age_seconds"], 3000.0
        )
        text = exporter.format_metrics()
        self.assertIn(
            'openviking_s3_ovlock_live_age_seconds{key="'
            + WEDGED_KEY
            + '",uri="viking://user/noot/sessions/mcp-store-e71a7b670fca",'
            'owner="9959d01e-e133-4e46-8074-04cc1de18b00",lock_type="E"} 3000.0',
            text,
        )

    def test_get_failure_keeps_cached_owner(self):
        client = StubS3({WEDGED_KEY: self.now - 5}, {WEDGED_KEY: WEDGED_BODY})
        self._poll(client)
        self.now += 1000
        client.objects[WEDGED_KEY] = self.now - 5
        self._poll(client)
        self.assertEqual(
            self._series()[0][0]["owner"], "9959d01e-e133-4e46-8074-04cc1de18b00"
        )
        self.now += 30
        client.objects[WEDGED_KEY] = self.now - 5
        client.bodies[WEDGED_KEY] = s3lite.S3Error("HTTP 500")
        self._poll(client)
        self.assertEqual(
            self._series()[0][0]["owner"], "9959d01e-e133-4e46-8074-04cc1de18b00"
        )

    def test_unreadable_body_without_cache_is_unknown(self):
        client = StubS3({WEDGED_KEY: self.now - 5}, {WEDGED_KEY: b"garbage"})
        self._poll(client)
        self.now += 1000
        client.objects[WEDGED_KEY] = self.now - 5
        self._poll(client)
        labels = self._series()[0][0]
        self.assertEqual((labels["owner"], labels["lock_type"]), ("unknown", "unknown"))
        self.assertEqual(
            labels["uri"], "viking://user/noot/sessions/mcp-store-e71a7b670fca"
        )

    def test_released_lock_drops_series_and_caches(self):
        client = StubS3({WEDGED_KEY: self.now - 5}, {WEDGED_KEY: WEDGED_BODY})
        self._poll(client)
        self.now += 1000
        client.objects[WEDGED_KEY] = self.now - 5
        self._poll(client)
        self.assertEqual(len(self._series()), 1)
        del client.objects[WEDGED_KEY]
        self.now += 15
        self._poll(client)
        self.assertEqual(self._series(), [])
        self.assertEqual(exporter._s3_first_seen, {})
        self.assertEqual(exporter._s3_token_cache, {})

    def test_ov_down_holds_last_series(self):
        client = StubS3({WEDGED_KEY: self.now - 5}, {WEDGED_KEY: WEDGED_BODY})
        self._poll(client)
        self.now += 1000
        client.objects[WEDGED_KEY] = self.now - 5
        self._poll(client)
        before = list(self._series())
        self.assertFalse(self._poll(client, ov_up=False))
        self.assertEqual(self._series(), before)
        self.assertEqual(exporter._metrics["openviking_s3_ovlock_poll_ok"], 0)

    def test_labelled_series_bounded_by_top_n(self):
        keys = [f"default/resources/r{i}/.path.ovlock" for i in range(6)]
        client = StubS3({k: self.now - 5 for k in keys})
        self._poll(client)
        self.now += 2000
        for k in keys:
            client.objects[k] = self.now - 5
        self._poll(client)
        self.assertEqual(len(self._series()), 3)
        self.assertEqual(len(client.gets), 3)


if __name__ == "__main__":
    unittest.main()
