"""Tests for ov-test-write-canary.py. Stdlib only — runs anywhere:

    python3 test-ov-test-write-canary.py

No cluster: the pure parts (config parsing, the refusals, the 409 retry, the
observer check, the probe record's md5, the verdict) are tested directly, with
fixtures in the shapes the 2026-10-25 BUG-1268 canary run saw live, and
run()/main() with their collaborators mocked.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_scripts" / "lib"))
from git_env import scrub_environ  # noqa: E402

scrub_environ()

import base64
import contextlib
import hashlib
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
PROD = {
    "storage": {
        "vectordb": {
            "name": "context",
            "url": "http://ov-vectordb.viking.svc.cluster.local:5000",
        }
    }
}
TARGET = {"collection": "context_test", "vectordb_svc": "svc/ov-vectordb-test"}
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


def md5(text):
    return hashlib.md5(text.encode()).hexdigest()


def passing():
    return {
        "health": 200,
        "observer_names_collection": True,
        "observer_healthy": True,
        "replace": {"http": 200, "body": WRITTEN},
        "read_back_matches": True,
        "probe_vector_written": True,
        "vectordb": dict(COUNTS),
    }


def args(**kw):
    base = dict(label="x", replace_attempts=1, replace_wait=0, write_timeout=600,
                no_vectordb=False)  # fmt: skip
    return wc.argparse.Namespace(**{**base, **kw})


def fake_forward(*children):
    """A forward() that registers each given child on the caller's stack, as the real one does."""
    queue = list(children)

    def forward(svc, remote, stack):
        p = queue.pop(0) if queue else mock.Mock()
        stack.callback(wc.stop, p)
        return 40000 + len(queue)

    return mock.Mock(side_effect=forward)


class Server:
    """An HTTP double for ov-test that logs every request and stores the probe."""

    def __init__(self, observer=(200, OBSERVER), write=(200, WRITTEN)):
        self.observer, self.write, self.sent, self.content = observer, write, [], None

    def __call__(
        self, base, headers, method, path, params=None, body=None, timeout=120
    ):
        self.sent.append({"method": method, "path": path, "body": body,
                          "timeout": timeout})  # fmt: skip
        if path == "/api/v1/observer/vikingdb":
            return self.observer
        if path == "/health":
            return 200, {}
        if method == "POST":
            if self.write[0] == 200:
                self.content = body["content"]
            return self.write
        if self.content is None:
            return 404, "Not Found"
        return 200, {"result": self.content}

    def posts(self):
        return [s for s in self.sent if s["method"] == "POST"]


@contextlib.contextmanager
def cluster(http, forward=None, stop=None, prod=PROD, adapter=None):
    """run()'s collaborators: ov-test's conf, prod's, a key, and an importable adapter."""
    adapter = adapter or mock.Mock(**{"query.return_value": []})
    with mock.patch.object(wc, "read_ov_conf",
                           side_effect=lambda cm: CONF if cm == wc.TEST_CONFIGMAP else prod), \
         mock.patch.object(wc, "kubectl", return_value=base64.b64encode(b"key").decode()), \
         mock.patch.object(wc, "forward", forward or fake_forward()), \
         mock.patch.object(wc, "http", side_effect=http), \
         mock.patch.object(wc, "adapter_class", return_value=mock.Mock(return_value=adapter)), \
         mock.patch.object(wc, "vectordb_counts", return_value=dict(COUNTS)), \
         mock.patch.object(wc, "stop", stop or mock.Mock()):  # fmt: skip
        yield adapter


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
            vdb(url="http://ov-vectordb-test.viking.svc"),
            vdb(name=""),
        ):
            with self.assertRaises(wc.CanaryError):
                wc.target_from_conf(conf)

    def test_anything_templated_refuses(self):
        # homelab#266 rounds 4-5: OpenViking expands ${VAR} over the whole file;
        # json.loads does not, so no value this run uses may hold a `$`.
        for conf in (vdb(name="${OV_COLLECTION}"),
                     vdb(url="http://${VDB_HOST}:5000"),
                     vdb(project="${OV_PROJECT}"), vdb(index_name="$OV_INDEX"),
                     {**CONF, "default_account": "${OV_ACCOUNT}"},
                     {**CONF, "default_user": "$USER"}):  # fmt: skip
            with self.assertRaises(wc.CanaryError, msg=conf):
                wc.target_from_conf(conf)

    def test_a_vectordb_outside_the_namespace_refuses(self):
        # homelab#266 round 5: other-ns was dropped, landing on viking's service.
        for url in ("http://ov-vectordb.other-ns.svc:5000", "http://10.0.0.5:5000"):
            with self.assertRaises(wc.CanaryError, msg=url):
                wc.target_from_conf(vdb(url=url))
        self.assertEqual(wc.vectordb_service("http://ov-vectordb-test:5000"),
                         ("svc/ov-vectordb-test", 5000))  # fmt: skip

    def test_prods_collection_or_vectordb_refuses(self):
        wc.refuse_prod(TARGET, PROD)
        for target, prod in (
            ({**TARGET, "collection": "context"}, PROD),
            # homelab#266 round 5: a test collection on prod's vectordb passed.
            ({**TARGET, "vectordb_svc": "svc/ov-vectordb"}, PROD),
            (TARGET, {"storage": {}}),
            (TARGET, {"storage": {"vectordb": None}}),
            (TARGET, {"storage": {"vectordb": {**PROD["storage"]["vectordb"], "name": "${C}"}}}),
            (TARGET, {"storage": {"vectordb": {"name": "context"}}}),
        ):  # fmt: skip
            with self.assertRaises(wc.CanaryError, msg=(target, prod)):
                wc.refuse_prod(target, prod)


class KeyTests(unittest.TestCase):
    def test_the_key_is_stripped(self):
        with mock.patch.object(wc, "kubectl",
                               return_value=base64.b64encode(b"s3cr3t\n").decode()):  # fmt: skip
            self.assertEqual(wc.read_key(), "s3cr3t")

    def test_a_key_that_cannot_be_a_header_refuses_without_naming_it(self):
        # homelab#266 round 5: http.client's ValueError put the key in the receipt.
        for raw in (b"", b"s3cr3t\nmore", b"s3cr3t more", b"\xff\xfe"):
            with mock.patch.object(wc, "kubectl",
                                   return_value=base64.b64encode(raw).decode()), \
                 self.assertRaises(wc.CanaryError) as cm:  # fmt: skip
                wc.read_key()
            self.assertNotIn("s3cr3t", str(cm.exception))


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
    CONTENT = "# canary\n\nreplaced 2026-10-25T00:00:00Z abc (after)\n"

    def test_only_a_record_of_this_runs_content_counts(self):
        # homelab#266 rounds 4-5: an old record, the create's record, or an
        # errored read (which the adapter returns as no rows) passed.
        mine = md5(self.CONTENT)
        self.assertTrue(wc.probe_vector_written({"rows": 2, "md5s": ["a", mine]},
                                                self.CONTENT))  # fmt: skip
        for record in ({"rows": 1, "md5s": [md5("# canary\n\ncreated x\n")]},
                       {"rows": 0, "md5s": []}, {"rows": 1, "md5s": []},
                       {"error": "ERROR RuntimeError('boom')"}, None):  # fmt: skip
            self.assertFalse(wc.probe_vector_written(record, self.CONTENT), record)

    def test_probe_record_reports_the_md5s_or_the_error(self):
        adapter = mock.Mock()
        adapter.query.return_value = [{"md5": "b"}, {"md5": "a"}, {"md5": None}, {}]
        self.assertEqual(wc.probe_record(adapter), {"rows": 4, "md5s": ["a", "b"]})
        self.assertEqual(adapter.query.call_args.kwargs["filter"], wc.PROBE_FILTER)
        self.assertIn("md5", adapter.query.call_args.kwargs["output_fields"])
        adapter.query.side_effect = RuntimeError("boom")
        self.assertIn("error", wc.probe_record(adapter))

    def test_a_termination_is_not_swallowed_as_a_result(self):
        # homelab#266 round 5: with Terminated an Exception, these would report it.
        adapter = mock.Mock()
        adapter.query.side_effect = wc.Terminated
        adapter.count.side_effect = wc.Terminated
        with self.assertRaises(wc.Terminated):
            wc.probe_record(adapter)
        with self.assertRaises(wc.Terminated):
            wc.vectordb_counts(adapter, "default")


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
        # --no-vectordb still checks the server's own outcome.
        self.assertEqual(wc.failures({**out, "vectordb": None}, False),
                         ["replace (vector_status is not complete)"])  # fmt: skip

    def test_a_record_without_this_runs_md5_fails(self):
        out = {**passing(), "probe_vector_written": False}
        self.assertEqual(wc.failures(out, True),
                         ["probe_vector (no record carries this run's content md5)"])  # fmt: skip

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
    def test_a_clean_run_writes_only_the_probe_and_checks_its_record(self):
        server = Server()
        forward = fake_forward()
        out = {}
        with cluster(server, forward=forward) as adapter:
            adapter.query.side_effect = lambda **k: [{"md5": md5(server.content)}]
            wc.run(args(write_timeout=42), out)
        self.assertEqual(out["failed"], [])
        posts = server.posts()
        self.assertEqual([p["body"]["mode"] for p in posts], ["create", "replace"])
        # homelab#266 round 5: a POST to another URI or service passed the tests.
        for p in posts:
            self.assertEqual((p["path"], p["body"]["uri"], p["body"]["wait"], p["timeout"]),
                             ("/api/v1/content/write", wc.PROBE_URI, True, 42))  # fmt: skip
        self.assertEqual([c.args[:2] for c in forward.call_args_list],
                         [(wc.SERVER_SVC, wc.SERVER_PORT), ("svc/ov-vectordb-test", 5000)])  # fmt: skip
        self.assertTrue(out["probe_vector_written"])

    def test_a_record_left_by_the_create_fails(self):
        # homelab#266 round 5: a run that created the probe passed on the create's
        # record even when the replace's vectors were never written.
        server = Server()
        out = {}
        with cluster(server) as adapter:
            adapter.query.side_effect = lambda **k: [
                {"md5": md5(server.posts()[0]["body"]["content"])}
            ]
            wc.run(args(), out)
        self.assertEqual(server.posts()[0]["body"]["mode"], "create")
        self.assertIn(
            "probe_vector (no record carries this run's content md5)", out["failed"]
        )

    def test_prods_collection_refuses_before_any_forward_or_request(self):
        # homelab#266 round 5: removing or moving refuse_prod passed the tests.
        server = Server()
        forward = fake_forward()
        same = {
            "storage": {
                "vectordb": {**PROD["storage"]["vectordb"], "name": "context_test"}
            }
        }
        with (
            cluster(server, forward=forward, prod=same),
            self.assertRaises(wc.CanaryError),
        ):
            wc.run(args(), {})
        self.assertEqual((forward.call_count, server.sent), (0, []))

    def test_an_observer_naming_another_collection_refuses_before_any_write(self):
        other = {"result": {**OBSERVER["result"],
                            "status": STATUS.replace("context_test", "context")}}  # fmt: skip
        server = Server(observer=(200, other))
        with cluster(server), self.assertRaises(wc.CanaryError):
            wc.run(args(), {})
        self.assertEqual(server.posts(), [])

    def test_an_observer_naming_nothing_is_a_failed_check_not_a_refusal(self):
        # The BUG-1268 `before` run: an unpatched observer errors, and the run writes.
        server = Server(observer=(500, "Internal Server Error"))
        out = {}
        with cluster(server):
            wc.run(args(), out)
        self.assertEqual(len(server.posts()), 2)
        self.assertIn("observer_names_collection", out["failed"])

    def test_no_vectordb_skips_the_vectordb_forward(self):
        forward = fake_forward()
        out = {}
        with cluster(Server(), forward=forward):
            wc.run(args(no_vectordb=True), out)
        self.assertEqual(forward.call_count, 1)
        self.assertEqual(out["vectordb"], "skipped (--no-vectordb)")
        self.assertEqual(out["failed"], [])


class ForwardTests(unittest.TestCase):
    def setUp(self):
        # No real socket: a sandbox may refuse even a loopback bind.
        patcher = mock.patch.object(wc, "free_port", return_value=40000)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_child_is_on_the_stack_before_any_signal_can_land(self):
        # homelab#266 round 5: a signal between forward() returning and the
        # caller's callback left the child running.
        child = mock.Mock()
        child.poll.return_value = None
        masks = []

        def popen(*a, **k):
            masks.append(signal.pthread_sigmask(signal.SIG_BLOCK, set()))
            return child

        stack = mock.Mock()
        with mock.patch.object(wc.subprocess, "Popen", side_effect=popen), \
             mock.patch.object(wc.socket, "create_connection"):  # fmt: skip
            self.assertEqual(wc.forward("svc/openviking-test", 1933, stack), 40000)
        self.assertTrue(wc.HELD <= masks[0])
        stack.callback.assert_called_once_with(wc.stop, child)
        self.assertFalse(wc.HELD & signal.pthread_sigmask(signal.SIG_BLOCK, set()))

    def test_an_interrupt_during_startup_stops_the_child(self):
        # homelab#266 round 1: the child outlived a KeyboardInterrupt here.
        child = mock.Mock()
        child.poll.return_value = None
        with mock.patch.object(wc.subprocess, "Popen", return_value=child), \
             mock.patch.object(wc.socket, "create_connection",
                               side_effect=KeyboardInterrupt):  # fmt: skip
            with self.assertRaises(KeyboardInterrupt):
                with contextlib.ExitStack() as stack:
                    wc.forward("svc/openviking-test", 1933, stack)
        child.terminate.assert_called_once()
        child.wait.assert_called()

    def test_a_child_that_exits_is_reported_and_reaped(self):
        child = mock.Mock(returncode=1)
        child.poll.return_value = 1
        with mock.patch.object(wc.subprocess, "Popen", return_value=child):
            with self.assertRaises(wc.CanaryError):
                with contextlib.ExitStack() as stack:
                    wc.forward("svc/openviking-test", 1933, stack)
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

        with cluster(Server(), forward=fake_forward(server, vdb_child), stop=stop):
            with self.assertRaises(KeyboardInterrupt):
                wc.run(args(), {})
        self.assertEqual(stopped, [vdb_child, server])

    def test_sigterm_during_a_run_stops_every_child(self):
        # homelab#266 round 4: SIGTERM ended the process past the ExitStack.
        server, vdb_child = mock.Mock(name="server"), mock.Mock(name="vdb")
        stopped = []

        def adapter_query(**k):
            os.kill(os.getpid(), signal.SIGTERM)
            return []

        buf = io.StringIO()
        with cluster(Server(), forward=fake_forward(server, vdb_child),
                     stop=stopped.append) as adapter, \
             contextlib.redirect_stdout(buf):  # fmt: skip
            adapter.query.side_effect = adapter_query
            code = wc.main(["--label", "x"])
        self.assertEqual(code, wc.EXIT_TERMINATED)
        self.assertEqual(stopped, [vdb_child, server])
        receipt = json.loads(buf.getvalue())
        self.assertEqual(receipt["error"], "terminated")
        self.assertEqual(receipt["target"]["collection"], "context_test")
        self.assertIs(signal.getsignal(signal.SIGTERM), signal.SIG_DFL)
        self.assertFalse(wc.HELD & signal.pthread_sigmask(signal.SIG_BLOCK, set()))

    def test_signals_wait_while_the_receipt_is_printed(self):
        # homelab#266 round 5: a SIGTERM during the final emit printed nothing.
        masks = []
        real = wc.emit

        def emit(out):
            masks.append(signal.pthread_sigmask(signal.SIG_BLOCK, set()))
            real(out)

        with mock.patch.object(wc, "run", side_effect=lambda a, o: o.update(failed=[])), \
             mock.patch.object(wc, "emit", side_effect=emit), \
             contextlib.redirect_stdout(io.StringIO()):  # fmt: skip
            wc.main(["--label", "x"])
        self.assertTrue(wc.HELD <= masks[0])
        self.assertFalse(wc.HELD & signal.pthread_sigmask(signal.SIG_BLOCK, set()))


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
        # homelab#266 rounds 4-5: usage errors exited 2 with no receipt, and a
        # --replace-wait that sleep() rejects (negative, NaN, infinite, or past
        # its range) failed only after the writes.
        run = mock.Mock()
        with mock.patch.object(wc, "run", run):
            for argv in ([], ["--label", "x", "--replace-attempts", "0"],
                         ["--label", "x", "--replace-wait", "-1"],
                         ["--label", "x", "--replace-wait", "nan"],
                         ["--label", "x", "--replace-wait", "inf"],
                         ["--label", "x", "--replace-wait", "1e10"],
                         ["--label", "x", "--write-timeout", "0"],
                         ["--label", "x", "--write-timeout", "1e10"]):  # fmt: skip
                code, out = self.run_main(argv)
                self.assertEqual(code, wc.EXIT_ERROR, argv)
                self.assertEqual(len(out.splitlines()), 1, argv)
                self.assertTrue(json.loads(out)["error"].startswith("bad arguments"))
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
