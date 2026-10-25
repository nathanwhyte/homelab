"""Tests for ov-test-write-canary.py. Stdlib only — runs anywhere:

    python3 test-ov-test-write-canary.py

No cluster: the pure parts (config parsing, the prod refusal, the 409 retry, the
observer check, the probe record's freshness, the verdict) are tested directly,
with fixtures in the shapes the 2026-10-25 BUG-1268 canary run saw live, and
run()/main() with their collaborators mocked.
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
import signal
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
PROD = {"storage": {"vectordb": {"name": "context"}}}
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
WRITTEN = {"status": "ok", "result": {"vector_status": "complete"}}


def vdb(**fields):
    return {**CONF, "storage": {"vectordb": {**CONF["storage"]["vectordb"], **fields}}}


def passing():
    return {
        "health": 200,
        "observer_names_collection": True,
        "observer_healthy": True,
        "replace": {"http": 200, "body": WRITTEN},
        "read_back_matches": True,
        "probe_vector_fresh": True,
        "vectordb": dict(COUNTS),
    }


def args(**kw):
    base = dict(label="x", replace_attempts=1, replace_wait=0, write_timeout=600,
                no_counts=False)  # fmt: skip
    return wc.argparse.Namespace(**{**base, **kw})


@contextlib.contextmanager
def cluster(http, forward=None, stop=None, records=None):
    """run()'s collaborators: ov-test's conf, prod's, a key, and an importable adapter."""
    forward = forward or mock.Mock(side_effect=[(mock.Mock(), 1), (mock.Mock(), 2)])
    records = records or [{"rows": 0, "updated_at": None},
                          {"rows": 1, "updated_at": "2026-10-25T16:16:51.000Z"}]  # fmt: skip
    with mock.patch.object(wc, "read_ov_conf",
                           side_effect=lambda cm: CONF if cm == wc.TEST_CONFIGMAP else PROD), \
         mock.patch.object(wc, "kubectl", return_value="a2V5"), \
         mock.patch.object(wc, "forward", forward), \
         mock.patch.object(wc, "http", side_effect=http), \
         mock.patch.object(wc, "adapter_class", return_value=mock.Mock()), \
         mock.patch.object(wc, "probe_record", side_effect=records), \
         mock.patch.object(wc, "vectordb_counts", return_value=dict(COUNTS)), \
         mock.patch.object(wc, "stop", stop or mock.Mock()):  # fmt: skip
        yield


class ConfigTests(unittest.TestCase):
    def test_target_comes_from_ov_conf(self):
        self.assertEqual(
            wc.target_from_conf(CONF),
            {"collection": "context_test", "vectordb_svc": "svc/ov-vectordb-test",
             "vectordb_port": 5000, "project": "default", "index": "default",
             "account": "default", "user": "noot"},
        )  # fmt: skip

    def test_project_and_index_come_from_ov_conf_when_set(self):
        # homelab#266 round 4: both were hard-coded to "default".
        target = wc.target_from_conf(vdb(project="p1", index_name="i1"))
        self.assertEqual((target["project"], target["index"]), ("p1", "i1"))

    def test_a_missing_field_or_unusable_url_refuses(self):
        for conf in (
            {**CONF, "storage": {}},
            {k: v for k, v in CONF.items() if k != "default_user"},
            vdb(url="nowhere"),
            vdb(name=""),
        ):
            with self.assertRaises(wc.CanaryError):
                wc.target_from_conf(conf)

    def test_a_templated_name_or_url_refuses(self):
        # homelab#266 round 4: OpenViking expands ${VAR}; json.loads does not.
        for conf in (vdb(name="${OV_COLLECTION}"),
                     vdb(url="http://${VDB_HOST}:5000")):  # fmt: skip
            with self.assertRaises(wc.CanaryError):
                wc.target_from_conf(conf)

    def test_prod_collection_refuses(self):
        wc.refuse_prod("context_test", PROD)
        for test, prod in (("context", PROD), ("context_test", {"storage": {}}),
                           ("context_test", {"storage": {"vectordb": {"name": "${C}"}}})):  # fmt: skip
            with self.assertRaises(wc.CanaryError):
                wc.refuse_prod(test, prod)


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

    def test_the_replace_waits_for_its_vectors(self):
        # homelab#266 round 4: without wait the server only queues the vectors.
        call = mock.Mock(return_value=(200, WRITTEN))
        wc.replace_with_retry(call, "x", attempts=1, wait=0)
        self.assertIs(call.call_args.kwargs["body"]["wait"], True)


class ObserverTests(unittest.TestCase):
    def test_names_the_collection_on_its_own_line(self):
        self.assertEqual(wc.observer_names(OBSERVER, "context_test"), (True, True))
        # A prefix of another collection's name is not a match.
        self.assertEqual(wc.observer_names(OBSERVER, "context")[0], False)
        self.assertEqual(wc.observer_collection(OBSERVER), "context_test")

    def test_an_error_body_is_neither(self):
        self.assertEqual(wc.observer_names("Not Found", "context_test"), (False, False))
        self.assertEqual(
            wc.observer_names({"result": None}, "context_test"), (False, False)
        )
        self.assertIsNone(wc.observer_collection("Not Found"))
        unhealthy = {"result": {**OBSERVER["result"], "is_healthy": False}}
        self.assertEqual(wc.observer_names(unhealthy, "context_test"), (True, False))


class ProbeVectorTests(unittest.TestCase):
    OLD = {"rows": 1, "updated_at": "2026-10-25T16:16:51.000Z"}
    NEW = {"rows": 1, "updated_at": "2026-10-25T17:02:10.000Z"}

    def test_a_newer_record_or_a_new_one_is_fresh(self):
        self.assertTrue(wc.probe_vector_fresh(self.OLD, self.NEW))
        self.assertTrue(
            wc.probe_vector_fresh({"rows": 0, "updated_at": None}, self.NEW)
        )

    def test_an_unchanged_missing_or_unreadable_record_is_not(self):
        # homelab#266 round 4: a broken vector upsert left the old record in place.
        for before, after in (
            (self.OLD, self.OLD),
            (self.NEW, self.OLD),
            (self.OLD, {"rows": 0, "updated_at": None}),
            (self.OLD, {"error": "ERROR HTTPError('404')"}),
            ({"error": "ERROR HTTPError('404')"}, self.NEW),
            (self.OLD, {"rows": 1, "updated_at": "not a time"}),
            ({"rows": 1, "updated_at": None}, self.NEW),
        ):
            self.assertFalse(wc.probe_vector_fresh(before, after), (before, after))

    def test_probe_record_reports_the_latest_stamp_or_the_error(self):
        adapter = mock.Mock()
        adapter.query.return_value = [{"updated_at": "2026-10-25T16:00:00Z"},
                                      {"updated_at": "2026-10-25T17:00:00Z"}]  # fmt: skip
        self.assertEqual(wc.probe_record(adapter),
                         {"rows": 2, "updated_at": "2026-10-25T17:00:00Z"})  # fmt: skip
        self.assertEqual(adapter.query.call_args.kwargs["filter"], wc.PROBE_FILTER)
        adapter.query.side_effect = RuntimeError("boom")
        self.assertIn("error", wc.probe_record(adapter))


class VerdictTests(unittest.TestCase):
    def test_a_clean_run_has_no_failures(self):
        self.assertEqual(wc.failures(passing(), need_vectordb=True), [])

    def test_each_failed_check_is_named(self):
        out = {**passing(), "replace": {"http": 404}, "read_back_matches": False}
        self.assertEqual(wc.failures(out, True), ["replace", "read_back_matches"])

    def test_a_200_replace_with_an_error_body_fails(self):
        out = {**passing(), "replace": {"http": 200, "body": {"status": "error"}}}
        self.assertEqual(wc.failures(out, True), ["replace"])

    def test_vectors_that_were_not_written_fail(self):
        # homelab#266 round 4: a queued vector write passed with failed [].
        for status in ("queued", "failed", "skipped", None):
            body = {"status": "ok", "result": {"vector_status": status}}
            out = {**passing(), "replace": {"http": 200, "body": body}}
            self.assertEqual(wc.failures(out, True),
                             ["replace (vector_status is not complete)"], status)  # fmt: skip
        # --no-counts still checks the server's own outcome.
        self.assertEqual(wc.failures({**out, "vectordb": None}, False),
                         ["replace (vector_status is not complete)"])  # fmt: skip

    def test_a_stale_probe_record_fails(self):
        out = {**passing(), "probe_vector_fresh": False}
        self.assertEqual(wc.failures(out, True),
                         ["probe_vector (no record newer than before the write)"])  # fmt: skip

    def test_counts_must_be_possible_for_any_data(self):
        bad_shape = ["vectordb (a count errored or is not a non-negative integer)"]
        # homelab#266 round 2: -1/-2/-3/0 passed.
        for name, value in (("strict_count", "ERROR HTTPError('404')"),
                            ("count", True), ("count", -1),
                            ("strict_count_account", -3)):  # fmt: skip
            out = passing()
            out["vectordb"][name] = value
            self.assertEqual(wc.failures(out, True), bad_shape, (name, value))
        out = passing()
        del out["vectordb"]["count"]
        self.assertEqual(wc.failures(out, True), bad_shape)
        out = passing()
        out["vectordb"]["strict_count_no_match"] = 3
        self.assertEqual(wc.failures(out, True),
                         ["vectordb (a filter matching nothing counted rows)"])  # fmt: skip
        # homelab#266 round 3: indexing between calls can make the account count
        # exceed the earlier total on a healthy backend, so that is no failure.
        out = passing()
        out["vectordb"].update(count=1, strict_count=1, strict_count_account=2)
        self.assertEqual(wc.failures(out, True), [])

    def test_an_empty_account_after_a_completed_write_fails(self):
        out = passing()
        out["vectordb"]["strict_count_account"] = 0
        self.assertEqual(wc.failures(out, True),
                         ["vectordb (the account holds no record after a completed write)"])  # fmt: skip

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


class RunTests(unittest.TestCase):
    def test_a_clean_run_waits_on_every_write_and_checks_the_record(self):
        sent = []

        def http(base, headers, method, path, params=None, body=None, timeout=120):
            sent.append((method, path, body, timeout))
            if path == "/api/v1/observer/vikingdb":
                return 200, OBSERVER
            if path == "/health":
                return 200, {}
            if method == "POST":
                return 200, WRITTEN
            if len([s for s in sent if s[0] == "POST"]) < 2:
                return 404, "Not Found"
            return 200, {"result": sent[-2][2]["content"]}

        out = {}
        with cluster(http):
            wc.run(args(write_timeout=42), out)
        self.assertEqual(out["failed"], [])
        posts = [s for s in sent if s[0] == "POST"]
        self.assertEqual([p[2]["mode"] for p in posts], ["create", "replace"])
        self.assertTrue(all(p[2]["wait"] is True and p[3] == 42 for p in posts))
        self.assertTrue(out["probe_vector_fresh"])

    def test_an_observer_naming_another_collection_refuses_before_any_write(self):
        other = {"result": {**OBSERVER["result"],
                            "status": STATUS.replace("context_test", "context")}}  # fmt: skip
        sent = []

        def http(base, headers, method, path, params=None, body=None, timeout=120):
            sent.append(method)
            return 200, other

        with cluster(http), self.assertRaises(wc.CanaryError):
            wc.run(args(), {})
        self.assertNotIn("POST", sent)

    def test_an_observer_naming_nothing_is_a_failed_check_not_a_refusal(self):
        # The BUG-1268 `before` run: an unpatched observer errors, and the run writes.
        sent = []

        def http(base, headers, method, path, params=None, body=None, timeout=120):
            sent.append(method)
            if path == "/api/v1/observer/vikingdb":
                return 500, "Internal Server Error"
            return (200, WRITTEN) if method == "POST" else (404, "Not Found")

        out = {}
        with cluster(http):
            wc.run(args(), out)
        self.assertIn("POST", sent)
        self.assertIn("observer_names_collection", out["failed"])

    def test_no_counts_skips_the_vectordb_forward(self):
        forward = mock.Mock(return_value=(mock.Mock(), 1))
        with cluster(lambda *a, **k: (404, "Not Found"), forward=forward):
            out = {}
            wc.run(args(no_counts=True), out)
        self.assertEqual(forward.call_count, 1)
        self.assertEqual(out["vectordb"], "skipped (--no-counts)")


class ForwardTests(unittest.TestCase):
    def setUp(self):
        # No real socket: a sandbox may refuse even a loopback bind.
        patcher = mock.patch.object(wc, "free_port", return_value=40000)
        patcher.start()
        self.addCleanup(patcher.stop)

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


class StopTests(unittest.TestCase):
    def test_an_interrupted_wait_still_kills_the_child(self):
        # homelab#266 round 2: an interrupt in wait() skipped the kill fallback.
        child = mock.Mock()
        child.poll.return_value = None
        child.wait.side_effect = [KeyboardInterrupt, 0]
        with self.assertRaises(KeyboardInterrupt):
            wc.stop(child)
        child.kill.assert_called_once()

    def test_run_stops_every_child_when_one_stop_raises(self):
        # homelab#266 round 2: an interrupt stopping one child skipped the other.
        server, vdb_child = mock.Mock(name="server"), mock.Mock(name="vdb")
        stopped = []

        def stop(p):
            stopped.append(p)
            if p is vdb_child:
                raise KeyboardInterrupt

        forward = mock.Mock(side_effect=[(server, 1), (vdb_child, 2)])
        with cluster(lambda *a, **k: (404, "Not Found"), forward=forward, stop=stop):
            with self.assertRaises(KeyboardInterrupt):
                wc.run(args(), {})
        self.assertEqual(stopped, [vdb_child, server])

    def test_sigterm_during_a_run_stops_every_child(self):
        # homelab#266 round 4: SIGTERM ended the process past the ExitStack.
        server, vdb_child = mock.Mock(name="server"), mock.Mock(name="vdb")
        stopped = []

        def http(base, headers, method, path, params=None, body=None, timeout=120):
            if method == "POST":
                os.kill(os.getpid(), signal.SIGTERM)
            return 404, "Not Found"

        forward = mock.Mock(side_effect=[(server, 1), (vdb_child, 2)])
        buf = io.StringIO()
        with cluster(http, forward=forward, stop=stopped.append), \
             contextlib.redirect_stdout(buf):  # fmt: skip
            code = wc.main(["--label", "x"])
        self.assertEqual(code, wc.EXIT_TERMINATED)
        self.assertEqual(stopped, [vdb_child, server])
        receipt = json.loads(buf.getvalue())
        self.assertEqual(receipt["error"], "terminated")
        self.assertEqual(receipt["target"]["collection"], "context_test")
        self.assertIs(signal.getsignal(signal.SIGTERM), signal.SIG_DFL)


class MainTests(unittest.TestCase):
    def run_main(self, argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            try:
                code = wc.main(argv)
            except SystemExit as e:
                code = e.code
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

    def test_an_interrupt_prints_the_receipt_and_exits_130(self):
        # homelab#266 round 2: KeyboardInterrupt bypassed `except Exception`.
        def interrupt(args, out):
            out["health"] = 200
            raise KeyboardInterrupt

        with mock.patch.object(wc, "run", side_effect=interrupt):
            code, out = self.run_main(["--label", "x"])
        self.assertEqual(code, wc.EXIT_INTERRUPTED)
        self.assertEqual(json.loads(out), {"label": "x", "health": 200,
                                           "error": "interrupted"})  # fmt: skip

    def test_every_receipt_is_one_line(self):
        # homelab#266 round 2: indent=2 printed a refusal over four lines.
        for effect in (wc.CanaryError("is prod's"), lambda a, o: o.update(failed=[])):
            with mock.patch.object(wc, "run", side_effect=effect):
                _, out = self.run_main(["--label", "x"])
            self.assertEqual(len(out.splitlines()), 1, out)

    def test_failed_checks_exit_3(self):
        def finish(failed):
            return lambda args, out: out.update(failed=failed)

        with mock.patch.object(wc, "run", side_effect=finish(["replace"])):
            self.assertEqual(self.run_main(["--label", "x"])[0], wc.EXIT_FAILED)
        with mock.patch.object(wc, "run", side_effect=finish([])):
            self.assertEqual(self.run_main(["--label", "x"])[0], wc.EXIT_OK)

    def test_bad_arguments_exit_1_with_a_receipt_before_any_run(self):
        # homelab#266 round 4: usage errors exited 2 with no receipt, and a
        # negative, NaN or infinite --replace-wait failed only after the writes.
        run = mock.Mock()
        with mock.patch.object(wc, "run", run):
            for argv in ([], ["--label", "x", "--replace-attempts", "0"],
                         ["--label", "x", "--replace-wait", "-1"],
                         ["--label", "x", "--replace-wait", "nan"],
                         ["--label", "x", "--replace-wait", "inf"],
                         ["--label", "x", "--write-timeout", "0"]):  # fmt: skip
                code, out = self.run_main(argv)
                self.assertEqual(code, wc.EXIT_ERROR, argv)
                self.assertEqual(len(out.splitlines()), 1, argv)
                self.assertTrue(json.loads(out)["error"].startswith("bad arguments"))
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
