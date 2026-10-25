"""Tests for ov-test-write-canary.py. Stdlib only — runs anywhere:

    python3 test-ov-test-write-canary.py

No cluster: the pure parts (config parsing, the prod refusal, the 409 retry, the
observer check, the verdict) are tested directly, with fixtures in the shapes the
2026-10-25 BUG-1268 canary run saw live.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_scripts" / "lib"))
from git_env import scrub_environ  # noqa: E402

scrub_environ()

import contextlib
import importlib.util
import io
import json
import os
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_SPEC = importlib.util.spec_from_file_location(
    "ov_test_write_canary", os.path.join(_HERE, "ov-test-write-canary.py")
)
wc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(wc)

CONF = {
    "default_account": "default",
    "default_user": "noot",
    "storage": {
        "vectordb": {
            "backend": "http",
            "name": "context_test",
            "dimension": 2560,
            "url": "http://ov-vectordb-test.viking.svc.cluster.local:5000",
        }
    },
}
STATUS = (
    "Account: default\nBackend: http\nCollection: context_test\nIndex: default\n"
    "Dimension: 2560\nVector count: 5"
)
OBSERVER = {
    "status": "ok",
    "result": {"name": "vikingdb", "is_healthy": True, "status": STATUS},
}
COUNTS = {
    "count": 5,
    "strict_count": 5,
    "strict_count_account": 5,
    "strict_count_no_match": 0,
}


def passing():
    return {
        "health": 200,
        "observer_names_collection": True,
        "observer_healthy": True,
        "replace": {"http": 200},
        "read_back_matches": True,
        "vectordb": dict(COUNTS),
    }


class ConfigTests(unittest.TestCase):
    def test_target_comes_from_ov_conf(self):
        self.assertEqual(
            wc.target_from_conf(CONF),
            {"collection": "context_test", "vectordb_svc": "svc/ov-vectordb-test",
             "vectordb_port": 5000, "account": "default", "user": "noot"},
        )  # fmt: skip

    def test_a_missing_field_or_unusable_url_refuses(self):
        for conf in (
            {**CONF, "storage": {}},
            {k: v for k, v in CONF.items() if k != "default_user"},
            {
                **CONF,
                "storage": {
                    "vectordb": {**CONF["storage"]["vectordb"], "url": "nowhere"}
                },
            },
            {
                **CONF,
                "storage": {"vectordb": {**CONF["storage"]["vectordb"], "name": ""}},
            },
        ):
            with self.assertRaises(wc.CanaryError):
                wc.target_from_conf(conf)

    def test_prod_collection_refuses(self):
        prod = {"storage": {"vectordb": {"name": "context"}}}
        wc.refuse_prod("context_test", prod)
        with self.assertRaises(wc.CanaryError):
            wc.refuse_prod("context", prod)
        with self.assertRaises(wc.CanaryError):
            wc.refuse_prod("context_test", {"storage": {}})


class ReplaceRetryTests(unittest.TestCase):
    def test_retries_on_409_until_the_path_frees(self):
        replies = iter([(409, "busy"), (409, "busy"), (200, {"status": "ok"})])
        sleep = mock.Mock()
        status, body, tries = wc.replace_with_retry(
            lambda *a, **k: next(replies), "x", attempts=5, wait=10, sleep=sleep
        )
        self.assertEqual((status, tries), (200, 3))
        self.assertEqual(sleep.call_count, 2)

    def test_gives_up_with_the_last_409(self):
        sleep = mock.Mock()
        status, _, tries = wc.replace_with_retry(
            lambda *a, **k: (409, "busy"), "x", attempts=2, wait=10, sleep=sleep
        )
        self.assertEqual((status, tries, sleep.call_count), (409, 2, 1))

    def test_other_errors_are_not_retried(self):
        sleep = mock.Mock()
        status, _, tries = wc.replace_with_retry(
            lambda *a, **k: (500, "boom"), "x", attempts=5, wait=10, sleep=sleep
        )
        self.assertEqual((status, tries, sleep.call_count), (500, 1, 0))


class ObserverTests(unittest.TestCase):
    def test_names_the_collection_on_its_own_line(self):
        self.assertEqual(wc.observer_names(OBSERVER, "context_test"), (True, True))
        # A prefix of another collection's name is not a match.
        self.assertEqual(wc.observer_names(OBSERVER, "context")[0], False)

    def test_an_error_body_is_neither(self):
        self.assertEqual(wc.observer_names("Not Found", "context_test"), (False, False))
        self.assertEqual(
            wc.observer_names({"result": None}, "context_test"), (False, False)
        )
        unhealthy = {"result": {**OBSERVER["result"], "is_healthy": False}}
        self.assertEqual(wc.observer_names(unhealthy, "context_test"), (True, False))


class VerdictTests(unittest.TestCase):
    def test_a_clean_run_has_no_failures(self):
        self.assertEqual(wc.failures(passing(), need_counts=True), [])

    def test_each_failed_check_is_named(self):
        out = {**passing(), "replace": {"http": 404}, "read_back_matches": False}
        self.assertEqual(wc.failures(out, True), ["replace", "read_back_matches"])

    def test_count_errors_and_a_nonzero_no_match_fail(self):
        out = passing()
        out["vectordb"]["strict_count"] = "ERROR HTTPError('404')"
        self.assertEqual(wc.failures(out, True), ["vectordb"])
        out = passing()
        out["vectordb"]["strict_count_no_match"] = 3
        self.assertIn("vectordb (a filter matching nothing counted rows)",
                      wc.failures(out, True))  # fmt: skip
        out = passing()
        out["vectordb"]["count"] = True
        self.assertEqual(wc.failures(out, True), ["vectordb"])

    def test_missing_counts_fail_unless_skipped(self):
        out = {**passing(), "vectordb": None}
        self.assertEqual(len(wc.failures(out, True)), 1)
        self.assertEqual(wc.failures(out, False), [])


class ReadBackTests(unittest.TestCase):
    def test_only_the_exact_replacement_matches(self):
        expected = "# canary\n\nreplaced 2026-10-25T00:00:00Z abc (after)\n"
        self.assertTrue(wc.read_back_matches(200, {"result": expected}, expected))
        # homelab#266 round 1: a truncated body that kept the marker passed.
        for body in ({"result": "replaced 2026-10-25T00:00:00Z abc"}, {"result": None},
                     "Not Found"):  # fmt: skip
            self.assertFalse(wc.read_back_matches(200, body, expected))
        self.assertFalse(wc.read_back_matches(404, {"result": expected}, expected))


class ForwardTests(unittest.TestCase):
    def test_an_interrupt_during_startup_stops_the_child(self):
        # homelab#266 round 1: the child outlived a KeyboardInterrupt here.
        child = mock.Mock()
        child.poll.return_value = None
        with mock.patch.object(wc.subprocess, "Popen", return_value=child), \
             mock.patch.object(wc.socket, "create_connection",
                               side_effect=KeyboardInterrupt):  # fmt: skip
            with self.assertRaises(KeyboardInterrupt):
                wc.forward("svc/openviking-test", 1933)
        child.terminate.assert_called_once()
        child.wait.assert_called()

    def test_a_child_that_exits_is_reported_and_reaped(self):
        child = mock.Mock(returncode=1)
        child.poll.return_value = 1
        with mock.patch.object(wc.subprocess, "Popen", return_value=child):
            with self.assertRaises(wc.CanaryError):
                wc.forward("svc/openviking-test", 1933)
        child.wait.assert_called()


class MainTests(unittest.TestCase):
    def run_main(self, argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = wc.main(argv)
        return code, buf.getvalue()

    def test_a_refusal_exits_1_with_one_json_receipt(self):
        with mock.patch.object(wc, "run", side_effect=wc.CanaryError("is prod's")):
            code, out = self.run_main(["--label", "after"])
        self.assertEqual(code, wc.EXIT_ERROR)
        self.assertEqual(json.loads(out), {"label": "after", "error": "is prod's"})

    def test_a_transport_error_still_prints_the_receipt_so_far(self):
        # homelab#266 round 1: URLError and ValueError escaped with no receipt.
        def fail(args, out):
            out["health"] = 200
            raise wc.urllib.error.URLError("connection reset")

        with mock.patch.object(wc, "run", side_effect=fail):
            code, out = self.run_main(["--label", "after"])
        receipt = json.loads(out)
        self.assertEqual(code, wc.EXIT_ERROR)
        self.assertEqual(receipt["health"], 200)
        self.assertTrue(receipt["error"].startswith("URLError: "))

    def test_failed_checks_exit_3(self):
        def finish(failed):
            return lambda args, out: out.update(failed=failed)

        with mock.patch.object(wc, "run", side_effect=finish(["replace"])):
            self.assertEqual(self.run_main(["--label", "x"])[0], wc.EXIT_FAILED)
        with mock.patch.object(wc, "run", side_effect=finish([])):
            self.assertEqual(self.run_main(["--label", "x"])[0], wc.EXIT_OK)

    def test_replace_attempts_must_be_positive(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            wc.main(["--label", "x", "--replace-attempts", "0"])


if __name__ == "__main__":
    unittest.main()
