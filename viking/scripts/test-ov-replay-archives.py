"""Tests for ov-replay-archives.py. Pure (fakes only) — runs anywhere:

    python3 test-ov-replay-archives.py

Every HTTP call goes through the module's single ``_request`` choke point, so these
tests replace that function with a scripted fake keyed by (method, path-regex) instead
of mocking ``urllib``. No test here talks to a real server; the live identity-refusal
check and the self-test round trip against real ov-test are exercised by hand (see the
IMPR-1188 Phase 3 PR description for that evidence).
"""

import json
import os
import re
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_MODULE_PATH = os.path.join(_HERE, "ov-replay-archives.py")
import importlib.util

_SPEC = importlib.util.spec_from_file_location("ov_replay_archives", _MODULE_PATH)
ra = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ra)


OV_TEST_STATUS = {
    "components": {
        "vikingdb": {
            "status": "+--------------+-------------+--------------+--------+\n"
            "|  Collection  | Index Count | Vector Count | Status |\n"
            "+--------------+-------------+--------------+--------+\n"
            "| context_test |      1      |      0       |   OK   |\n"
            "+--------------+-------------+--------------+--------+"
        },
        "queue": {"status": "no queue here"},
    }
}
PROD_STATUS = {
    "components": {
        "vikingdb": {
            "status": "+------------+-------------+--------------+--------+\n"
            "| Collection | Index Count | Vector Count | Status |\n"
            "+------------+-------------+--------------+--------+\n"
            "|  context   |      1      |      0       |   OK   |\n"
            "+------------+-------------+--------------+--------+"
        }
    }
}
IDLE_QUEUE_STATUS = {
    "components": {
        "queue": {
            "status": "+----------------+---------+-------------+\n"
            "|     Queue      | Pending | In Progress |\n"
            "+----------------+---------+-------------+\n"
            "|    Semantic    |    0    |      0      |\n"
            "| Semantic-Nodes |    0    |      0      |\n"
            "+----------------+---------+-------------+"
        }
    }
}
BUSY_QUEUE_STATUS = {
    "components": {
        "queue": {
            "status": "+----------------+---------+-------------+\n"
            "|     Queue      | Pending | In Progress |\n"
            "+----------------+---------+-------------+\n"
            "|    Semantic    |    4    |      1      |\n"
            "| Semantic-Nodes |    0    |      0      |\n"
            "+----------------+---------+-------------+"
        }
    }
}
# Regression fixture (2026-09-24 real sample): Semantic itself is idle, but
# Semantic-Nodes still has pending/in-progress work -- an earlier prod_semantic_queue_idle
# checked only the Semantic row and reported idle=True here, which is wrong.
SEMANTIC_IDLE_NODES_BUSY_STATUS = {
    "components": {
        "queue": {
            "status": "+----------------+---------+-------------+\n"
            "|     Queue      | Pending | In Progress |\n"
            "+----------------+---------+-------------+\n"
            "|    Semantic    |    0    |      0      |\n"
            "| Semantic-Nodes |    9    |      1      |\n"
            "+----------------+---------+-------------+"
        }
    }
}


class FakeRequester:
    """Replaces ``_request``: scripted (method, path-regex) -> handler(body, base_url).

    ``calls`` records ``(base_url, method, path, body)`` so a test can assert which
    host a call went to (the preflight and identity checks share a path but target
    different hosts). A handler that only needs ``body`` (almost every existing rule)
    can ignore the second positional argument -- ``_const`` does.
    """

    def __init__(self, rules):
        self.rules = [(m, re.compile(p), h) for m, p, h in rules]
        self.calls = []

    def __call__(
        self, base_url, method, path, api_key, account, user, body=None, timeout=None
    ):
        self.calls.append((base_url, method, path, body))
        for m, pattern, handler in self.rules:
            if m == method and pattern.match(path):
                return handler(body, base_url)
        raise AssertionError(
            f"unexpected request: {method} {path} (base_url={base_url})"
        )


def _const(value):
    return lambda body, base_url=None: value


def _by_base_url(mapping):
    """A handler that returns a different canned value per target host."""
    return lambda body, base_url=None: mapping[base_url]


class VerifyIdentityTests(unittest.TestCase):
    def test_ov_test_target_is_accepted(self):
        fake = FakeRequester(
            [("GET", r"^/api/v1/observer/system", _const(OV_TEST_STATUS))]
        )
        ra._request = fake
        ra.verify_ov_test_identity("http://ov-test", "key")

    def test_prod_shaped_target_is_refused(self):
        fake = FakeRequester(
            [("GET", r"^/api/v1/observer/system", _const(PROD_STATUS))]
        )
        ra._request = fake
        with self.assertRaises(ra.TargetIdentityError):
            ra.verify_ov_test_identity("http://prod", "key")

    def test_unreachable_marker_is_refused_not_assumed_safe(self):
        fake = FakeRequester(
            [("GET", r"^/api/v1/observer/system", _const({"components": {}}))]
        )
        ra._request = fake
        with self.assertRaises(ra.TargetIdentityError):
            ra.verify_ov_test_identity("http://mystery", "key")


class MessagePayloadTests(unittest.TestCase):
    def test_only_role_and_parts_are_required(self):
        payload = ra._message_payload(
            {"role": "user", "parts": [{"type": "text", "text": "hi"}]}
        )
        self.assertEqual(
            payload, {"role": "user", "parts": [{"type": "text", "text": "hi"}]}
        )

    def test_missing_optional_fields_are_omitted_not_nulled(self):
        # The common shape of a real archived message: no turn_id/message_kind/source_message_ids.
        message = {
            "id": "m1",
            "role": "assistant",
            "parts": [{"type": "text", "text": "ok"}],
            "created_at": "2026-09-24T20:39:14+00:00",
            "peer_id": "p",
            "turn_id": None,
            "message_kind": None,
            "source_message_ids": None,
        }
        payload = ra._message_payload(message)
        self.assertNotIn("turn_id", payload)
        self.assertNotIn("message_kind", payload)
        self.assertNotIn("source_message_ids", payload)
        self.assertEqual(payload["created_at"], "2026-09-24T20:39:14+00:00")
        self.assertEqual(payload["peer_id"], "p")

    def test_present_optional_fields_are_carried(self):
        message = {
            "role": "user",
            "parts": [],
            "turn_id": "t1",
            "message_kind": "user_query",
            "source_message_ids": ["a", "b"],
        }
        payload = ra._message_payload(message)
        self.assertEqual(payload["turn_id"], "t1")
        self.assertEqual(payload["message_kind"], "user_query")
        self.assertEqual(payload["source_message_ids"], ["a", "b"])


class ProdQueueIdleTests(unittest.TestCase):
    def test_idle_queue_reads_true(self):
        fake = FakeRequester(
            [("GET", r"^/api/v1/observer/system", _const(IDLE_QUEUE_STATUS))]
        )
        ra._request = fake
        idle, _ = ra.prod_semantic_queue_idle("http://prod", "key")
        self.assertTrue(idle)

    def test_busy_queue_reads_false(self):
        fake = FakeRequester(
            [("GET", r"^/api/v1/observer/system", _const(BUSY_QUEUE_STATUS))]
        )
        ra._request = fake
        idle, status = ra.prod_semantic_queue_idle("http://prod", "key")
        self.assertFalse(idle)
        self.assertIn("Semantic", status)

    def test_unrecognised_table_shape_is_none_not_a_false_idle(self):
        fake = FakeRequester(
            [
                (
                    "GET",
                    r"^/api/v1/observer/system",
                    _const({"components": {"queue": {"status": ""}}}),
                )
            ]
        )
        ra._request = fake
        idle, _ = ra.prod_semantic_queue_idle("http://prod", "key")
        self.assertIsNone(idle)

    def test_semantic_idle_but_semantic_nodes_busy_is_not_idle(self):
        # Regression: an earlier version checked only the Semantic row and reported
        # idle=True here (live sample, 2026-09-24: Semantic 0/0, Semantic-Nodes 9/1).
        fake = FakeRequester(
            [
                (
                    "GET",
                    r"^/api/v1/observer/system",
                    _const(SEMANTIC_IDLE_NODES_BUSY_STATUS),
                )
            ]
        )
        ra._request = fake
        idle, status = ra.prod_semantic_queue_idle("http://prod", "key")
        self.assertFalse(idle)
        self.assertIn("Semantic-Nodes", status)

    def test_a_missing_semantic_nodes_row_is_unrecognised_not_idle(self):
        only_semantic = {
            "components": {
                "queue": {
                    "status": "+----------+---------+-------------+\n"
                    "|  Queue   | Pending | In Progress |\n"
                    "+----------+---------+-------------+\n"
                    "| Semantic |    0    |      0      |\n"
                    "+----------+---------+-------------+"
                }
            }
        }
        fake = FakeRequester(
            [("GET", r"^/api/v1/observer/system", _const(only_semantic))]
        )
        ra._request = fake
        idle, _ = ra.prod_semantic_queue_idle("http://prod", "key")
        self.assertIsNone(idle)


class PreflightProdQueueTests(unittest.TestCase):
    def test_idle_queue_passes(self):
        fake = FakeRequester(
            [("GET", r"^/api/v1/observer/system", _const(IDLE_QUEUE_STATUS))]
        )
        ra._request = fake
        ra.preflight_prod_queue("http://prod", "key")  # must not raise

    def test_busy_queue_raises_prod_busy_error(self):
        fake = FakeRequester(
            [("GET", r"^/api/v1/observer/system", _const(BUSY_QUEUE_STATUS))]
        )
        ra._request = fake
        with self.assertRaises(ra.ProdBusyError):
            ra.preflight_prod_queue("http://prod", "key")

    def test_semantic_nodes_alone_being_busy_raises(self):
        fake = FakeRequester(
            [
                (
                    "GET",
                    r"^/api/v1/observer/system",
                    _const(SEMANTIC_IDLE_NODES_BUSY_STATUS),
                )
            ]
        )
        ra._request = fake
        with self.assertRaises(ra.ProdBusyError):
            ra.preflight_prod_queue("http://prod", "key")

    def test_a_read_failure_is_treated_as_busy_not_idle(self):
        def fail(body, base_url=None):
            raise ra.ReplayError("connection refused")

        fake = FakeRequester([("GET", r"^/api/v1/observer/system", fail)])
        ra._request = fake
        with self.assertRaises(ra.ProdBusyError):
            ra.preflight_prod_queue("http://prod", "key")

    def test_an_unrecognised_table_shape_is_treated_as_busy_not_idle(self):
        fake = FakeRequester(
            [
                (
                    "GET",
                    r"^/api/v1/observer/system",
                    _const({"components": {"queue": {"status": ""}}}),
                )
            ]
        )
        ra._request = fake
        with self.assertRaises(ra.ProdBusyError):
            ra.preflight_prod_queue("http://prod", "key")


class WaitForTaskTests(unittest.TestCase):
    def test_returns_on_a_done_status(self):
        fake = FakeRequester(
            [("GET", r"^/api/v1/tasks/t1$", _const({"status": "completed"}))]
        )
        ra._request = fake
        result = ra.wait_for_task(
            "http://ov-test", "key", "default", "u", "t1", sleep=lambda s: None
        )
        self.assertEqual(result["status"], "completed")

    def test_raises_on_a_failed_status(self):
        fake = FakeRequester(
            [("GET", r"^/api/v1/tasks/t1$", _const({"status": "failed"}))]
        )
        ra._request = fake
        with self.assertRaises(ra.ReplayError):
            ra.wait_for_task(
                "http://ov-test", "key", "default", "u", "t1", sleep=lambda s: None
            )

    def test_times_out_without_a_terminal_status(self):
        fake = FakeRequester(
            [("GET", r"^/api/v1/tasks/t1$", _const({"status": "running"}))]
        )
        ra._request = fake
        clock = iter([0, 1, 2, 100])  # exceeds a small timeout quickly
        with self.assertRaises(ra.ReplayError):
            ra.wait_for_task(
                "http://ov-test",
                "key",
                "default",
                "u",
                "t1",
                timeout=10,
                sleep=lambda s: None,
                clock=lambda: next(clock),
            )


# The real memory_diff.json shape (self_test run live against ov-test, 2026-09-27,
# identifiers scrubbed). read_memory_diff/_diff_operations nest under "operations".
REAL_EMPTY_DIFF = {
    "archive_uri": "viking://user/u/sessions/s/history/archive_001",
    "trace_id": None,
    "extracted_at": "2026-09-27T19:22:28.030788Z",
    "operations": {"adds": [], "updates": [], "deletes": []},
    "skipped_operations": [],
    "summary": {
        "total_adds": 0,
        "total_updates": 0,
        "total_deletes": 0,
        "total_skipped": 0,
    },
}


class DiffOperationsTests(unittest.TestCase):
    def test_the_real_nested_shape_is_read(self):
        diff = {
            "operations": {
                "adds": [{"uri": "a"}],
                "updates": [{"uri": "b"}],
                "deletes": [],
            }
        }
        adds, updates = ra._diff_operations(diff)
        self.assertEqual((adds, updates), ([{"uri": "a"}], [{"uri": "b"}]))

    def test_the_real_empty_shape_reads_as_no_operations(self):
        self.assertEqual(ra._diff_operations(REAL_EMPTY_DIFF), ([], []))

    def test_a_flat_legacy_shape_is_still_accepted(self):
        diff = {"adds": [{"uri": "a"}], "updates": []}
        self.assertEqual(ra._diff_operations(diff), ([{"uri": "a"}], []))

    def test_none_and_empty_are_no_operations(self):
        self.assertEqual(ra._diff_operations(None), ([], []))
        self.assertEqual(ra._diff_operations({}), ([], []))


OV_TEST_URL = "http://ov-test"
PROD_URL = "http://prod"


class SelfTestTests(unittest.TestCase):
    def _happy_rules(self, diff=None):
        if diff is None:
            diff = {
                "operations": {"adds": [{"uri": "x"}], "updates": [], "deletes": []}
            }
        return [
            (
                "GET",
                r"^/api/v1/observer/system",
                _by_base_url(
                    {OV_TEST_URL: OV_TEST_STATUS, PROD_URL: IDLE_QUEUE_STATUS}
                ),
            ),
            ("POST", r"^/api/v1/sessions$", _const({"session_id": "sess-1"})),
            (
                "POST",
                r"^/api/v1/sessions/sess-1/messages$",
                _const({"message_count": 1}),
            ),
            (
                "POST",
                r"^/api/v1/sessions/sess-1/commit$",
                _const({"task_id": "task-1"}),
            ),
            ("GET", r"^/api/v1/tasks/task-1$", _const({"status": "completed"})),
            (
                "GET",
                r"^/api/v1/content/read",
                _const({"content": json.dumps(diff)}),
            ),
            ("DELETE", r"^/api/v1/sessions/sess-1$", _const({"session_id": "sess-1"})),
        ]

    def test_round_trips_one_synthetic_session(self):
        fake = FakeRequester(self._happy_rules())
        ra._request = fake
        receipt = ra.self_test(OV_TEST_URL, "key", PROD_URL)
        self.assertTrue(receipt["ok"])
        self.assertTrue(receipt["cleaned_up"])
        self.assertEqual(receipt["session_id"], "sess-1")
        # two messages posted before commit
        message_calls = [
            c for c in fake.calls if c[1] == "POST" and c[2].endswith("/messages")
        ]
        self.assertEqual(len(message_calls), 2)
        # cleanup ran last
        self.assertEqual(fake.calls[-1][1], "DELETE")

    def test_refuses_a_non_ov_test_target_after_the_preflight_but_before_creating_anything(
        self,
    ):
        mystery = "http://mystery-target"
        fake = FakeRequester(
            [
                (
                    "GET",
                    r"^/api/v1/observer/system",
                    _by_base_url({PROD_URL: IDLE_QUEUE_STATUS, mystery: PROD_STATUS}),
                )
            ]
        )
        ra._request = fake
        with self.assertRaises(ra.TargetIdentityError):
            ra.self_test(mystery, "key", PROD_URL)
        # preflight (prod), then the failed identity check (mystery) -- nothing else
        self.assertEqual([c[0] for c in fake.calls], [PROD_URL, mystery])
        self.assertFalse(any(c[1] == "POST" for c in fake.calls))

    def test_a_busy_prod_queue_blocks_before_any_session_is_created(self):
        fake = FakeRequester(
            [
                (
                    "GET",
                    r"^/api/v1/observer/system",
                    _by_base_url({PROD_URL: BUSY_QUEUE_STATUS}),
                )
            ]
        )
        ra._request = fake
        with self.assertRaises(ra.ProdBusyError):
            ra.self_test(OV_TEST_URL, "key", PROD_URL)
        self.assertEqual(
            len(fake.calls), 1, "must stop at the preflight, never reach ov-test"
        )
        self.assertFalse(any(c[1] == "POST" for c in fake.calls))

    def test_a_terminal_task_with_no_operations_is_not_success(self):
        # This is the real empty shape captured live (2026-09-27): a genuinely successful
        # round trip whose trivial synthetic exchange extracted nothing. It is not
        # raised as an error -- the receipt is returned normally so main() can still
        # write it and report the failure via the exit code (Codex #173 finding 6).
        fake = FakeRequester(self._happy_rules(diff=REAL_EMPTY_DIFF))
        ra._request = fake
        receipt = ra.self_test(OV_TEST_URL, "key", PROD_URL)
        self.assertFalse(receipt["successful_extraction"])
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["memory_diff"], REAL_EMPTY_DIFF)
        # cleanup still ran
        self.assertTrue(receipt["cleaned_up"])
        self.assertEqual(fake.calls[-1][1], "DELETE")

    def test_fresh_user_per_call(self):
        seen_users = []
        orig_create = ra.create_session

        def spy_create(base_url, api_key, account, user, timeout=ra.DEFAULT_TIMEOUT):
            seen_users.append(user)
            return orig_create(base_url, api_key, account, user, timeout=timeout)

        ra.create_session = spy_create
        try:
            ra._request = FakeRequester(self._happy_rules())
            ra.self_test(OV_TEST_URL, "key", PROD_URL)
            ra._request = FakeRequester(self._happy_rules())
            ra.self_test(OV_TEST_URL, "key", PROD_URL)
        finally:
            ra.create_session = orig_create
        self.assertEqual(len(seen_users), 2)
        self.assertNotEqual(seen_users[0], seen_users[1])


class MainExitCodeTests(unittest.TestCase):
    """Codex #173 finding 6: the receipt is always written, and the exit code reports
    the extraction requirement's success/failure for both modes."""

    def setUp(self):
        self._orig_self_test = ra.self_test
        self._orig_replay_archive = ra.replay_archive

    def tearDown(self):
        ra.self_test = self._orig_self_test
        ra.replay_archive = self._orig_replay_archive

    def _run_main(self, argv):
        with tempfile.TemporaryDirectory() as tmp:
            receipt_path = os.path.join(tmp, "r.json")
            rc = ra.main([*argv, "--receipt", receipt_path])
            written = os.path.exists(receipt_path)
            body = None
            if written:
                with open(receipt_path) as fh:
                    body = json.load(fh)
        return rc, written, body

    def test_self_test_success_exits_zero(self):
        ra.self_test = lambda *a, **kw: {"successful_extraction": True, "ok": True}
        rc, written, body = self._run_main(
            [
                "--self-test",
                "--base-url",
                OV_TEST_URL,
                "--prod-base-url",
                PROD_URL,
                "--api-key",
                "k",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertTrue(written)
        self.assertTrue(body["ok"])

    def test_self_test_failure_still_writes_receipt_and_exits_nonzero(self):
        ra.self_test = lambda *a, **kw: {
            "successful_extraction": False,
            "ok": False,
            "memory_diff": {},
        }
        rc, written, body = self._run_main(
            [
                "--self-test",
                "--base-url",
                OV_TEST_URL,
                "--prod-base-url",
                PROD_URL,
                "--api-key",
                "k",
            ]
        )
        self.assertEqual(rc, 1)
        self.assertTrue(written, "the receipt must be written even on failure")
        self.assertFalse(body["successful_extraction"])

    def test_replay_success_exits_zero(self):
        ra.replay_archive = lambda *a, **kw: {"successful_extraction": True}
        rc, written, body = self._run_main(
            [
                "--replay",
                "--base-url",
                OV_TEST_URL,
                "--prod-base-url",
                PROD_URL,
                "--api-key",
                "k",
                "--session-uri",
                "viking://user/u/sessions/cc-x",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertTrue(written)
        self.assertTrue(body["successful_extraction"])

    def test_replay_failure_still_writes_receipt_and_exits_nonzero(self):
        ra.replay_archive = lambda *a, **kw: {
            "successful_extraction": False,
            "memory_diff": {},
        }
        rc, written, body = self._run_main(
            [
                "--replay",
                "--base-url",
                OV_TEST_URL,
                "--prod-base-url",
                PROD_URL,
                "--api-key",
                "k",
                "--session-uri",
                "viking://user/u/sessions/cc-x",
            ]
        )
        self.assertEqual(rc, 1)
        self.assertTrue(written, "the receipt must be written even on failure")
        self.assertFalse(body["successful_extraction"])


class ArgParsingTests(unittest.TestCase):
    def test_api_key_is_required(self):
        env = os.environ.pop("OPENVIKING_API_KEY", None)
        try:
            with self.assertRaises(SystemExit):
                ra.parse_args(
                    [
                        "--self-test",
                        "--base-url",
                        "http://ov-test",
                        "--prod-base-url",
                        "http://prod",
                    ]
                )
        finally:
            if env is not None:
                os.environ["OPENVIKING_API_KEY"] = env

    def test_prod_base_url_is_required_for_self_test_too(self):
        # The preflight needs it even though self-test never reads prod content.
        with self.assertRaises(SystemExit):
            ra.parse_args(
                ["--self-test", "--base-url", "http://ov-test", "--api-key", "k"]
            )

    def test_replay_requires_session_uri(self):
        with self.assertRaises(SystemExit):
            ra.parse_args(
                [
                    "--replay",
                    "--base-url",
                    "http://ov-test",
                    "--prod-base-url",
                    "http://prod",
                    "--api-key",
                    "k",
                ]
            )

    def test_self_test_needs_base_url_prod_base_url_and_key(self):
        args = ra.parse_args(
            [
                "--self-test",
                "--base-url",
                "http://ov-test",
                "--prod-base-url",
                "http://prod",
                "--api-key",
                "k",
            ]
        )
        self.assertTrue(args.self_test)
        self.assertEqual(args.account, "default")
        self.assertEqual(args.prod_base_url, "http://prod")

    def test_self_test_and_replay_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit):
            ra.parse_args(
                [
                    "--self-test",
                    "--replay",
                    "--base-url",
                    "http://ov-test",
                    "--api-key",
                    "k",
                ]
            )


class ToolResultIdFromRefTests(unittest.TestCase):
    def test_strips_the_trailing_slash_and_takes_the_last_segment(self):
        self.assertEqual(
            ra._tool_result_id_from_ref(
                "viking://user/u/sessions/cc-x/tool-results/tr_abc123/"
            ),
            "tr_abc123",
        )

    def test_no_trailing_slash(self):
        self.assertEqual(
            ra._tool_result_id_from_ref(
                "viking://user/u/sessions/cc-x/tool-results/tr_abc123"
            ),
            "tr_abc123",
        )


class HydrateToolOutputTests(unittest.TestCase):
    """Codex #173 finding 2: hydrate_tool_output goes through the read-only
    tool-results API (not a raw content read of the ref, which names a directory —
    tool_result_store.py:141,240,274), preserves source slices, clears every
    externalization field, and never drops the part on a failed read."""

    def test_a_part_with_no_externalization_fields_passes_through_unresolved(self):
        part = {"type": "tool", "tool_name": "Read"}
        hydrated, unresolved = ra.hydrate_tool_output(
            "http://prod", "key", "default", "u", "cc-x", part
        )
        self.assertEqual(hydrated, part)
        self.assertIsNot(hydrated, part, "a copy, not the source part")
        self.assertFalse(unresolved)

    def test_an_untruncated_ref_with_no_source_ref_is_stripped_but_not_fetched(self):
        # Already-inline output: no read is needed, but the prod pointer must not
        # reach ov-test.
        part = {
            "type": "tool",
            "tool_output": "already inline",
            "tool_output_ref": "viking://user/u/sessions/cc-x/tool-results/tr_1",
        }
        ra._request = FakeRequester([])  # any call would raise "unexpected request"
        hydrated, unresolved = ra.hydrate_tool_output(
            "http://prod", "key", "default", "u", "cc-x", part
        )
        self.assertFalse(unresolved)
        self.assertEqual(hydrated["tool_output"], "already inline")
        self.assertNotIn("tool_output_ref", hydrated)

    def test_a_truncated_part_is_refetched_in_full(self):
        fake = FakeRequester(
            [
                (
                    "GET",
                    r"^/api/v1/sessions/cc-x/tool-results/tr_1",
                    _const({"content": "the full output"}),
                )
            ]
        )
        ra._request = fake
        part = {
            "type": "tool",
            "tool_output": "...truncated...",
            "tool_output_ref": "viking://user/u/sessions/cc-x/tool-results/tr_1",
            "tool_output_truncated": True,
        }
        hydrated, unresolved = ra.hydrate_tool_output(
            "http://prod", "key", "default", "u", "cc-x", part
        )
        self.assertFalse(unresolved)
        self.assertEqual(hydrated["tool_output"], "the full output")
        self.assertNotIn("tool_output_ref", hydrated)
        self.assertNotIn("tool_output_truncated", hydrated)
        # offset=0, limit=-1 for a full re-fetch
        _base_url, _method, path, _body = fake.calls[0]
        self.assertIn("offset=0", path)
        self.assertIn("limit=-1", path)

    def test_a_source_ref_preserves_its_offset_and_limit(self):
        fake = FakeRequester(
            [
                (
                    "GET",
                    r"^/api/v1/sessions/cc-x/tool-results/tr_2",
                    _const({"content": "sliced output"}),
                )
            ]
        )
        ra._request = fake
        part = {
            "type": "tool",
            "tool_output_ref": "viking://user/u/sessions/cc-x/tool-results/tr_2",
            "tool_output_source_ref": "viking://user/u/sessions/cc-x/tool-results/tr_2",
            "tool_output_source_offset": 500,
            "tool_output_source_limit": 200,
        }
        hydrated, unresolved = ra.hydrate_tool_output(
            "http://prod", "key", "default", "u", "cc-x", part
        )
        self.assertFalse(unresolved)
        self.assertEqual(hydrated["tool_output"], "sliced output")
        self.assertNotIn("tool_output_source_ref", hydrated)
        self.assertNotIn("tool_output_source_offset", hydrated)
        self.assertNotIn("tool_output_source_limit", hydrated)
        path = fake.calls[0][2]
        self.assertIn("offset=500", path)
        self.assertIn("limit=200", path)

    def test_a_source_ref_with_no_limit_falls_back_to_original_chars(self):
        fake = FakeRequester(
            [
                (
                    "GET",
                    r"^/api/v1/sessions/cc-x/tool-results/tr_3",
                    _const({"content": "x"}),
                )
            ]
        )
        ra._request = fake
        part = {
            "tool_output_ref": "viking://user/u/sessions/cc-x/tool-results/tr_3",
            "tool_output_source_ref": "viking://user/u/sessions/cc-x/tool-results/tr_3",
            "tool_output_source_offset": 0,
            "tool_output_source_limit": None,
            "tool_output_original_chars": 4000,
        }
        result = ra.read_tool_result
        captured = {}

        def spy(*args, **kwargs):
            captured.update(kwargs)
            return result(*args, **kwargs)

        ra.read_tool_result = spy
        try:
            ra.hydrate_tool_output("http://prod", "key", "default", "u", "cc-x", part)
        finally:
            ra.read_tool_result = result
        self.assertEqual(captured["limit"], 4000)

    def test_a_failed_hydration_keeps_the_part_with_a_marker_and_is_unresolved(self):
        def fail(body, base_url=None):
            raise ra.ReplayError("not found")

        fake = FakeRequester(
            [("GET", r"^/api/v1/sessions/cc-x/tool-results/tr_missing", fail)]
        )
        ra._request = fake
        part = {
            "type": "tool",
            "tool_output_ref": "viking://user/u/sessions/cc-x/tool-results/tr_missing",
            "tool_output_truncated": True,
        }
        hydrated, unresolved = ra.hydrate_tool_output(
            "http://prod", "key", "default", "u", "cc-x", part
        )
        self.assertTrue(unresolved, "a failed read must not silently drop the part")
        self.assertEqual(hydrated["tool_output"], ra.UNRESOLVED_TOOL_OUTPUT_MARKER)
        self.assertNotIn(
            "tool_output_ref", hydrated, "the prod pointer is still cleared"
        )
        self.assertNotIn("tool_output_truncated", hydrated)


SOURCE_SESSION_URI = "viking://user/noot-pilot/sessions/cc-src"


class ReplayArchiveTests(unittest.TestCase):
    """End-to-end replay_archive() coverage, offline (mocked _request)."""

    def _messages_with_a_truncated_tool_part(self):
        return [
            {
                "id": "m0",
                "role": "user",
                "parts": [{"type": "text", "text": "what happened?"}],
            },
            {
                "id": "m1",
                "role": "assistant",
                "parts": [
                    {
                        "type": "tool",
                        "tool_name": "Bash",
                        "tool_output": "...truncated...",
                        "tool_output_ref": "viking://user/noot-pilot/sessions/cc-src/tool-results/tr_1",
                        "tool_output_truncated": True,
                    }
                ],
            },
        ]

    def _base_rules(self, tool_results_rule, diff=None):
        if diff is None:
            diff = {
                "operations": {"adds": [{"uri": "x"}], "updates": [], "deletes": []}
            }
        return [
            (
                "GET",
                r"^/api/v1/observer/system",
                _by_base_url(
                    {PROD_URL: IDLE_QUEUE_STATUS, OV_TEST_URL: OV_TEST_STATUS}
                ),
            ),
            (
                "GET",
                r"^/api/v1/content/read",
                _by_base_url(
                    {
                        PROD_URL: {
                            "content": "\n".join(
                                json.dumps(m)
                                for m in self._messages_with_a_truncated_tool_part()
                            )
                        },
                        OV_TEST_URL: {"content": json.dumps(diff)},
                    }
                ),
            ),
            tool_results_rule,
            ("POST", r"^/api/v1/sessions$", _const({"session_id": "sess-1"})),
            (
                "POST",
                r"^/api/v1/sessions/sess-1/messages$",
                _const({"message_count": 1}),
            ),
            (
                "POST",
                r"^/api/v1/sessions/sess-1/commit$",
                _const({"task_id": "task-1"}),
            ),
            ("GET", r"^/api/v1/tasks/task-1$", _const({"status": "completed"})),
        ]

    def test_a_hydrated_tool_part_is_posted_without_the_prod_pointer(self):
        rule = (
            "GET",
            r"^/api/v1/sessions/cc-src/tool-results/tr_1",
            _const({"content": "the full bash output"}),
        )
        fake = FakeRequester(self._base_rules(rule))
        ra._request = fake
        receipt = ra.replay_archive(
            OV_TEST_URL,
            "key",
            "default",
            PROD_URL,
            "default",
            "noot-pilot",
            SOURCE_SESSION_URI,
            "archive_001",
        )
        self.assertEqual(receipt["unresolved_tool_outputs"], [])
        message_posts = [
            c for c in fake.calls if c[1] == "POST" and c[2].endswith("/messages")
        ]
        self.assertEqual(len(message_posts), 2)
        tool_message_body = message_posts[1][3]
        posted_part = tool_message_body["parts"][0]
        self.assertEqual(posted_part["tool_output"], "the full bash output")
        self.assertNotIn("tool_output_ref", posted_part)
        self.assertNotIn("tool_output_truncated", posted_part)

    def test_an_unresolvable_tool_part_is_still_posted_and_listed_as_unresolved(self):
        def fail(body, base_url=None):
            raise ra.ReplayError("not found")

        rule = ("GET", r"^/api/v1/sessions/cc-src/tool-results/tr_1", fail)
        fake = FakeRequester(self._base_rules(rule))
        ra._request = fake
        receipt = ra.replay_archive(
            OV_TEST_URL,
            "key",
            "default",
            PROD_URL,
            "default",
            "noot-pilot",
            SOURCE_SESSION_URI,
            "archive_001",
        )
        self.assertEqual(len(receipt["unresolved_tool_outputs"]), 1)
        self.assertEqual(receipt["unresolved_tool_outputs"][0]["message_id"], "m1")
        # the tool message was still posted -- 2 posts, not 1
        message_posts = [
            c for c in fake.calls if c[1] == "POST" and c[2].endswith("/messages")
        ]
        self.assertEqual(len(message_posts), 2)
        posted_part = message_posts[1][3]["parts"][0]
        self.assertEqual(posted_part["tool_output"], ra.UNRESOLVED_TOOL_OUTPUT_MARKER)
        self.assertNotIn("tool_output_ref", posted_part)

    def test_a_busy_prod_queue_blocks_before_reading_any_production_messages(self):
        fake = FakeRequester(
            [
                (
                    "GET",
                    r"^/api/v1/observer/system",
                    _by_base_url({PROD_URL: BUSY_QUEUE_STATUS}),
                )
            ]
        )
        ra._request = fake
        with self.assertRaises(ra.ProdBusyError):
            ra.replay_archive(
                OV_TEST_URL,
                "key",
                "default",
                PROD_URL,
                "default",
                "noot-pilot",
                SOURCE_SESSION_URI,
                "archive_001",
            )
        self.assertEqual(len(fake.calls), 1)
        self.assertFalse(any(c[1] == "POST" for c in fake.calls))


if __name__ == "__main__":
    unittest.main()
