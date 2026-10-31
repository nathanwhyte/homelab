#!/usr/bin/env python3
"""Unit tests for ov-exporter.py's S3 lock gauges, s3lite's lock parsing, and
the /client-telemetry push validation.

BUG-1220: the wedge alert must name the wedged lock. Pure logic: stubbed S3,
pinned clock, no network.

    python3 viking/exporters/test_ov_exporter.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_scripts" / "lib"))
from git_env import scrub_environ  # noqa: E402

scrub_environ()

import datetime
import importlib.util
import json
import os
import socket
import threading
import time
import unittest
import urllib.request
from unittest import mock

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

    def test_locks_lists_every_live_lock_past_the_label_cap(self):
        # BUG-1282: /metrics names only the top N; the sync needs them all.
        keys = [f"default/resources/compendium/d{n}/.path.ovlock" for n in range(5)]
        dead = "default/resources/old/.path.ovlock"
        client = StubS3({**{k: self.now - 7 for k in keys}, dead: self.now - 7200})
        self.assertTrue(self._poll(client))
        self.now += 15
        for k in keys:
            client.objects[k] = self.now - 4
        self.assertTrue(self._poll(client))

        body = exporter.locks_payload()
        self.assertTrue(body["ok"])
        self.assertEqual(body["polled_at"], self.now)
        self.assertEqual(body["live_window_seconds"], 120.0)
        self.assertEqual([lock["key"] for lock in body["locks"]], keys)
        first = body["locks"][0]
        self.assertEqual(first["uri"], "viking://resources/compendium/d0")
        self.assertEqual(first["first_seen"], T0)
        self.assertEqual(first["last_modified"], self.now - 4)
        self.assertEqual(first["refresh_age_seconds"], 4.0)

    def test_locks_makes_no_s3_call(self):
        client = StubS3({WEDGED_KEY: self.now - 5}, {WEDGED_KEY: WEDGED_BODY})
        self.assertTrue(self._poll(client))
        gets = list(client.gets)
        exporter.locks_payload()
        self.assertEqual(client.gets, gets)

    def test_locks_names_an_owner_only_once_labelled(self):
        client = StubS3({WEDGED_KEY: self.now - 5}, {WEDGED_KEY: WEDGED_BODY})
        self.assertTrue(self._poll(client))
        self.assertNotIn("owner", exporter.locks_payload()["locks"][0])
        self.now += 3000
        client.objects[WEDGED_KEY] = self.now - 5
        self.assertTrue(self._poll(client))
        lock = exporter.locks_payload()["locks"][0]
        self.assertEqual(lock["owner"], "9959d01e-e133-4e46-8074-04cc1de18b00")
        self.assertEqual(lock["lock_type"], "E")
        self.assertIs(lock["owner_read_ok"], True)

    def test_locks_says_when_the_owner_is_a_cached_value(self):
        # Codex R1: a failed GET keeps the label, so a reader must see the
        # owner was not read on this poll.
        client = StubS3({WEDGED_KEY: self.now - 5}, {WEDGED_KEY: WEDGED_BODY})
        self.assertTrue(self._poll(client))
        self.now += 3000
        client.objects[WEDGED_KEY] = self.now - 5
        self.assertTrue(self._poll(client))
        self.now += 15
        client.objects[WEDGED_KEY] = self.now - 5
        client.bodies[WEDGED_KEY] = OSError("reset")
        self.assertTrue(self._poll(client))
        lock = exporter.locks_payload()["locks"][0]
        self.assertEqual(lock["owner"], "9959d01e-e133-4e46-8074-04cc1de18b00")
        self.assertIs(lock["owner_read_ok"], False)

    def test_locks_generation_counts_successful_polls_only(self):
        client = StubS3({WEDGED_KEY: self.now - 5})
        self.assertTrue(self._poll(client))
        first = exporter.locks_payload()["generation"]
        self.assertTrue(self._poll(client))
        self.assertEqual(exporter.locks_payload()["generation"], first + 1)
        self.assertFalse(self._poll(client, ov_up=False))
        self.assertEqual(exporter.locks_payload()["generation"], first + 1)

    def test_locks_snapshot_is_replaced_whole(self):
        # Codex R1: lock data and its poll marker must publish together.
        client = StubS3({WEDGED_KEY: self.now - 5})
        self.assertTrue(self._poll(client))
        before = exporter.locks_payload()
        self.now += 15
        client.objects.clear()
        self.assertTrue(self._poll(client))
        after = exporter.locks_payload()
        self.assertEqual(len(before["locks"]), 1)
        self.assertEqual(after["locks"], [])
        self.assertEqual(after["generation"], before["generation"] + 1)
        self.assertEqual(after["polled_at"], self.now)

    def test_locks_first_seen_restarts_with_the_holder(self):
        client = StubS3({WEDGED_KEY: self.now - 5}, {WEDGED_KEY: WEDGED_BODY})
        self.assertTrue(self._poll(client))
        self.now += 3000
        client.objects[WEDGED_KEY] = self.now - 5
        self.assertTrue(self._poll(client))
        self.now += 15
        client.objects[WEDGED_KEY] = self.now - 5
        client.bodies[WEDGED_KEY] = b"new-owner:1:E"
        self.assertTrue(self._poll(client))
        self.assertEqual(exporter.locks_payload()["locks"][0]["first_seen"], self.now)

    def test_locks_says_when_it_is_not_current(self):
        client = StubS3({WEDGED_KEY: self.now - 5})
        self.assertTrue(self._poll(client))
        polled = self.now
        self.now += 15
        self.assertFalse(self._poll(client, ov_up=False))
        body = exporter.locks_payload()
        self.assertFalse(body["ok"])
        self.assertEqual(body["polled_at"], polled)

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

    def test_holder_change_on_present_key_restarts_presence(self):
        # Review finding: released and re-acquired between two polls, the key
        # never leaves the bucket. The new holder must not inherit the old
        # holder's age (misattribution, and a fresh alert fingerprint that
        # bypasses repeat_interval).
        client = StubS3({WEDGED_KEY: self.now - 5}, {WEDGED_KEY: b"owner-1:1:T"})
        self._poll(client)
        self.now += 3000
        client.objects[WEDGED_KEY] = self.now - 5
        self._poll(client)
        self.assertEqual(
            [(s[0]["owner"], s[1]) for s in self._series()], [("owner-1", 3000.0)]
        )

        self.now += 15
        client.objects[WEDGED_KEY] = self.now - 2
        client.bodies[WEDGED_KEY] = b"owner-2:2:T"
        self._poll(client)
        self.assertEqual(self._series(), [])
        self.assertEqual(exporter._s3_first_seen[WEDGED_KEY], self.now)
        self.assertEqual(
            exporter._metrics["openviking_s3_ovlock_oldest_live_age_seconds"], 0.0
        )

        self.now += 1000
        client.objects[WEDGED_KEY] = self.now - 5
        self._poll(client)
        self.assertEqual(
            [(s[0]["owner"], s[1]) for s in self._series()], [("owner-2", 1000.0)]
        )

    def test_lock_type_change_is_a_holder_change(self):
        self.assertTrue(
            exporter.holder_changed(
                {"owner": "h", "lock_type": "T"}, {"owner": "h", "lock_type": "E"}
            )
        )
        self.assertFalse(
            exporter.holder_changed(
                {"owner": "h", "lock_type": "T"}, {"owner": "h", "lock_type": "T"}
            )
        )
        self.assertFalse(exporter.holder_changed(None, {"owner": "h"}))
        self.assertFalse(exporter.holder_changed({"owner": "h"}, None))

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


class TestClientPush(unittest.TestCase):
    KEY = "root-key"
    AUTH = "Bearer root-key"

    def setUp(self):
        exporter._client_totals.clear()

    def _push(self, doc, auth=AUTH, now=T0):
        body = doc if isinstance(doc, bytes) else json.dumps(doc).encode()
        return exporter.apply_client_push(body, auth, self.KEY, now)

    def _doc(self, machine="pop", **totals):
        return {
            "machine": machine,
            "harness": "claude-code",
            "totals": totals or {"recalls": 3, "recall_tokens": 900},
        }

    def test_valid_push_is_exported(self):
        self.assertEqual(self._push(self._doc()), (204, ""))
        text = exporter.format_metrics()
        self.assertIn(
            'openviking_client_recalls_total{machine="pop",harness="claude-code"} 3',
            text,
        )
        self.assertIn(
            'openviking_client_recall_tokens_total{machine="pop",harness="claude-code"} 900',
            text,
        )
        self.assertIn(
            f'openviking_client_last_push_unix{{machine="pop",harness="claude-code"}} {T0}',
            text,
        )

    def test_resend_replaces_instead_of_adding(self):
        self._push(self._doc(recalls=3))
        self._push(self._doc(recalls=5), now=T0 + 300)
        self.assertEqual(
            exporter._client_totals[("pop", "claude-code")],
            {"totals": {"recalls": 5}, "received": T0 + 300},
        )

    def test_rejects_wrong_or_missing_key(self):
        self.assertEqual(self._push(self._doc(), auth="Bearer nope")[0], 401)
        self.assertEqual(self._push(self._doc(), auth=None)[0], 401)
        self.assertEqual(exporter.apply_client_push(b"{}", "Bearer ", "", T0)[0], 401)
        self.assertEqual(exporter._client_totals, {})

    def test_rejects_bad_bodies(self):
        bad = [
            b"not json",
            [1, 2],
            self._doc(machine="Pop!"),
            {**self._doc(), "totals": {}},
            self._doc(bogus=1),
            self._doc(recalls=-1),
            self._doc(recalls=1.5),
            self._doc(recalls=True),
        ]
        for doc in bad:
            with self.subTest(doc=doc):
                self.assertEqual(self._push(doc)[0], 400)
        self.assertEqual(exporter._client_totals, {})

    def test_series_cap(self):
        for i in range(exporter.CLIENT_MAX_SERIES):
            self.assertEqual(self._push(self._doc(machine=f"m{i}"))[0], 204)
        self.assertEqual(self._push(self._doc(machine="one-more"))[0], 429)
        # An existing series can still update at the cap.
        self.assertEqual(self._push(self._doc(machine="m0"))[0], 204)


class TestClientPushHardening(unittest.TestCase):
    KEY = "root-key"
    AUTH = "Bearer root-key"

    def setUp(self):
        exporter._client_totals.clear()

    def _post(self, headers, body=b"", reads=None):
        def read(n):
            if reads is not None:
                reads.append(n)
            return body[:n]

        return exporter.client_post_response(headers, read, self.KEY)

    def _body(self, **totals):
        return json.dumps(
            {"machine": "pop", "harness": "codex", "totals": totals or {"recalls": 1}}
        ).encode()

    def test_huge_total_rejected_and_scrape_stays_valid(self):
        doc = {"machine": "pop", "harness": "codex", "totals": {"recalls": 10**400}}
        status, _ = exporter.apply_client_push(
            json.dumps(doc).encode(), self.AUTH, self.KEY, T0
        )
        self.assertEqual(status, 400)
        self.assertEqual(
            exporter.apply_client_push(
                self._body(recalls=2**53 + 1), self.AUTH, self.KEY, T0
            )[0],
            400,
        )
        self.assertEqual(
            exporter.apply_client_push(
                self._body(recalls=2**53), self.AUTH, self.KEY, T0
            )[0],
            204,
        )
        for line in exporter.format_metrics().splitlines():
            if line and not line.startswith("#"):
                float(line.rsplit(" ", 1)[1])  # every sample value parses

    def test_label_with_trailing_newline_rejected(self):
        doc = {"machine": "pop\n", "harness": "codex", "totals": {"recalls": 1}}
        status, _ = exporter.apply_client_push(
            json.dumps(doc).encode(), self.AUTH, self.KEY, T0
        )
        self.assertEqual(status, 400)

    def test_non_ascii_auth_is_401_not_an_exception(self):
        self.assertFalse(exporter.client_auth_ok("Bearer \xff€", self.KEY))
        self.assertTrue(exporter.client_auth_ok(self.AUTH, self.KEY))
        self.assertFalse(exporter.client_auth_ok(self.AUTH, ""))

    def test_unauthorised_post_reads_no_body(self):
        reads = []
        status, _ = self._post({"Content-Length": "4096"}, reads=reads)
        self.assertEqual((status, reads), (401, []))

    def test_bad_content_length(self):
        for value in ("wat", "", "-1", "0", "4097"):
            with self.subTest(value=value):
                reads = []
                status, _ = self._post(
                    {"Authorization": self.AUTH, "Content-Length": value}, reads=reads
                )
                self.assertIn(status, (400, 413))
                self.assertEqual(reads, [])

    def test_short_body_rejected(self):
        body = self._body()
        status, _ = self._post(
            {"Authorization": self.AUTH, "Content-Length": str(len(body) + 5)}, body
        )
        self.assertEqual(status, 400)
        self.assertEqual(exporter._client_totals, {})

    def test_inflight_cap(self):
        for _ in range(exporter.CLIENT_MAX_INFLIGHT):
            exporter._client_inflight.acquire()
        try:
            body = self._body()
            status, _ = self._post(
                {"Authorization": self.AUTH, "Content-Length": str(len(body))}, body
            )
            self.assertEqual(status, 503)
        finally:
            for _ in range(exporter.CLIENT_MAX_INFLIGHT):
                exporter._client_inflight.release()

    def test_session_start_runs_is_accepted(self):
        body = self._body(session_start_runs=7)
        status, _ = self._post(
            {"Authorization": self.AUTH, "Content-Length": str(len(body))}, body
        )
        self.assertEqual(status, 204)
        self.assertIn(
            'openviking_client_session_start_runs_total{machine="pop",harness="codex"} 7',
            exporter.format_metrics(),
        )

    def test_valid_post_stores(self):
        body = self._body(recall_errors=2)
        status, _ = self._post(
            {"Authorization": self.AUTH, "Content-Length": str(len(body))}, body
        )
        self.assertEqual(status, 204)
        self.assertIn(
            'openviking_client_recall_errors_total{machine="pop",harness="codex"} 2',
            exporter.format_metrics(),
        )


class TestConnectionDeadline(unittest.TestCase):
    """A client trickling bytes resets the socket timeout but not the deadline."""

    def setUp(self):
        handler = type("H", (exporter.MetricsHandler,), {"deadline": 0.5, "timeout": 5})
        self.server = exporter.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.env = mock.patch.dict(os.environ, {"OPENVIKING_KEY": "k"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_locks_route_serves_json(self):
        url = "http://%s:%d/locks" % self.server.server_address
        with urllib.request.urlopen(url, timeout=2) as resp:
            self.assertEqual(resp.headers["Content-Type"], "application/json")
            self.assertEqual(
                set(json.load(resp)),
                {"ok", "generation", "polled_at", "live_window_seconds", "locks"},
            )

    def trickle(self, head: bytes) -> float:
        """Send `head`, then a byte every 0.1 s; seconds until the server closes."""
        sock = socket.create_connection(self.server.server_address)
        sock.settimeout(0.1)
        start = time.monotonic()
        sock.sendall(head)
        try:
            while time.monotonic() - start < 3:
                try:
                    if sock.recv(4096) == b"":
                        break
                except TimeoutError:
                    sock.sendall(b"x")
        except OSError:
            pass  # reset by the server's shutdown
        finally:
            sock.close()
        return time.monotonic() - start

    def test_trickled_headers_are_cut_off(self):
        self.assertLess(self.trickle(b"GET /metr"), 1.5)

    def test_trickled_body_is_cut_off_and_frees_its_slot(self):
        head = b"POST /client-telemetry HTTP/1.1\r\nAuthorization: Bearer k\r\nContent-Length: 4000\r\n\r\n"
        self.assertLess(self.trickle(head), 1.5)
        # The in-flight slot was released: all four can still be taken.
        for _ in range(exporter.CLIENT_MAX_INFLIGHT):
            self.assertTrue(exporter._client_inflight.acquire(timeout=1))
        for _ in range(exporter.CLIENT_MAX_INFLIGHT):
            exporter._client_inflight.release()


if __name__ == "__main__":
    unittest.main()
