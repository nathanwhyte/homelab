"""Tests for ov-event-abstract-backfill.py. Pure (fakes only) — runs anywhere:

    python3 test-ov-event-abstract-backfill.py

``_rebuild`` and ``_dry_run_report`` call into the real openviking package (service
bootstrap, MemoryUpdater, EmbeddingMsgConverter) and are exercised end to end against
ov-test instead — see the IMPR-1200 PR for the receipts from that run. This file
covers the URI walk/filter, the receipt-capture wrapper, the sha256 of a text vs.
multimodal message, the restore ``__wrapped__`` resolution, and argument parsing, plus
the Codex-review fixes: events-namespace validation of the root and every selected URI,
the extract_context template guard, incremental JSONL receipts, per-URI enqueue
outcomes, read-back verification, and a dry run that never builds a writable service.
"""

import asyncio
import functools
import importlib.util
import json
import os
import sys
import types
import unittest
import unittest.mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_MODULE_PATH = os.path.join(_HERE, "ov-event-abstract-backfill.py")
_SPEC = importlib.util.spec_from_file_location(
    "ov_event_abstract_backfill", _MODULE_PATH
)
backfill = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(backfill)


class ArgParsingTests(unittest.TestCase):
    def test_user_is_required(self):
        with self.assertRaises(SystemExit):
            backfill.parse_args(["viking://user/u/peers/p/memories/events"])

    def test_dry_run_and_restore_are_exclusive(self):
        with self.assertRaises(SystemExit):
            backfill.parse_args(
                [
                    "viking://user/u/peers/p/memories/events",
                    "--user",
                    "u",
                    "--dry-run",
                    "--restore",
                ]
            )

    def test_defaults(self):
        args = backfill.parse_args(
            ["viking://user/u/peers/p/memories/events", "--user", "u"]
        )
        self.assertEqual(args.account, "default")
        self.assertFalse(args.dry_run)
        self.assertFalse(args.restore)
        self.assertIsNone(args.limit)
        self.assertIsNone(args.receipt)

    def test_limit_must_be_positive(self):
        with self.assertRaises(SystemExit):
            backfill.parse_args(
                [
                    "viking://user/u/peers/p/memories/events",
                    "--user",
                    "u",
                    "--limit",
                    "0",
                ]
            )


class IsTargetFileTests(unittest.TestCase):
    def test_directories_excluded(self):
        self.assertFalse(
            backfill._is_target_file({"isDir": True, "uri": "viking://x/y.md"})
        )

    def test_non_markdown_excluded(self):
        self.assertFalse(backfill._is_target_file({"uri": "viking://x/y.json"}))

    def test_directory_records_excluded(self):
        for name in (".overview.md", ".abstract.md"):
            self.assertFalse(backfill._is_target_file({"uri": f"viking://x/{name}"}))

    def test_event_file_included(self):
        self.assertTrue(
            backfill._is_target_file(
                {"uri": "viking://user/u/peers/p/memories/events/2026/09/24/x.md"}
            )
        )


class FakeVikingFS:
    def __init__(self, is_dir_root=True, entries=()):
        self._is_dir_root = is_dir_root
        self._entries = list(entries)
        self.exists_calls = []
        self.tree_calls = []

    async def exists(self, uri, ctx=None):
        self.exists_calls.append(uri)
        return True

    async def stat(self, uri, ctx=None, skip_count=False):
        return {"isDir": self._is_dir_root}

    async def tree(self, **kwargs):
        self.tree_calls.append(kwargs)
        return self._entries


class WalkEventFilesTests(unittest.TestCase):
    def test_missing_root_raises(self):
        fs = FakeVikingFS()

        async def exists(uri, ctx=None):
            return False

        fs.exists = exists
        with self.assertRaises(SystemExit):
            asyncio.run(backfill._walk_event_files(fs, "viking://missing", None))

    def test_single_file_root(self):
        fs = FakeVikingFS(is_dir_root=False)
        out = asyncio.run(backfill._walk_event_files(fs, "viking://x/y.md", None))
        self.assertEqual(out, ["viking://x/y.md"])

    def test_single_file_root_non_markdown_yields_nothing(self):
        fs = FakeVikingFS(is_dir_root=False)
        out = asyncio.run(backfill._walk_event_files(fs, "viking://x/y.json", None))
        self.assertEqual(out, [])

    def test_directory_walk_filters_and_sorts(self):
        entries = [
            {"uri": "viking://e/b.md", "isDir": False},
            {"uri": "viking://e/a.md", "isDir": False},
            {"uri": "viking://e/sub", "isDir": True},
            {"uri": "viking://e/.overview.md", "isDir": False},
            {"uri": "viking://e/.abstract.md", "isDir": False},
            {"uri": "viking://e/notes.txt", "isDir": False},
        ]
        fs = FakeVikingFS(is_dir_root=True, entries=entries)
        out = asyncio.run(backfill._walk_event_files(fs, "viking://e", None))
        self.assertEqual(out, ["viking://e/a.md", "viking://e/b.md"])
        self.assertEqual(fs.tree_calls[0]["node_limit"], None)
        self.assertEqual(fs.tree_calls[0]["level_limit"], None)
        self.assertTrue(fs.tree_calls[0]["show_all_hidden"])


class EmbeddingTextSha256Tests(unittest.TestCase):
    def test_string_message(self):
        import hashlib

        self.assertEqual(
            backfill._embedding_text_sha256("hello"),
            hashlib.sha256(b"hello").hexdigest(),
        )

    def test_multimodal_message_is_json_serialized(self):
        import hashlib

        message = [{"type": "text", "text": "hi"}]
        expected = hashlib.sha256(
            json.dumps(message, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        self.assertEqual(backfill._embedding_text_sha256(message), expected)

    def test_different_messages_hash_differently(self):
        self.assertNotEqual(
            backfill._embedding_text_sha256("a"), backfill._embedding_text_sha256("b")
        )


class ResolveBaseFromContextTests(unittest.TestCase):
    def test_backfill_uses_installed_as_is(self):
        def stock(*a, **kw):
            return "stock"

        self.assertIs(backfill._resolve_base_from_context(stock, restore=False), stock)

    def test_restore_unwraps_a_patched_callable(self):
        def stock(*a, **kw):
            return "stock"

        @functools.wraps(stock)
        def patched(*a, **kw):
            return "patched"

        self.assertIs(backfill._resolve_base_from_context(patched, restore=True), stock)

    def test_restore_is_a_no_op_when_never_patched(self):
        def stock(*a, **kw):
            return "stock"

        self.assertIs(backfill._resolve_base_from_context(stock, restore=True), stock)

    def test_restore_unwraps_through_a_staticmethod_wrapper(self):
        """Regression: apply() installs staticmethod(summary_abstract(original)), so
        the class dict holds a staticmethod object, not a plain function. A single
        getattr(..., "__wrapped__", ...) only unwraps that one level (staticmethod
        forwards __wrapped__ to its underlying callable as of Python 3.10) and lands
        back on the patched closure -- inspect.unwrap must keep going to the true
        original (caught live against ov-test, 2026-09-25)."""

        def stock(*a, **kw):
            return "stock"

        @functools.wraps(stock)
        def patched(*a, **kw):
            return "patched"

        installed = staticmethod(patched)
        self.assertIs(
            backfill._resolve_base_from_context(installed, restore=True), stock
        )


class ReceiptCaptureTests(unittest.TestCase):
    def test_captures_uri_and_lengths_and_hash(self):
        context = types.SimpleNamespace(abstract="the full body")

        def base_from_context(ctx_arg):
            self.assertIs(ctx_arg, context)
            return types.SimpleNamespace(
                message="the summary",
                context_data={"uri": "viking://x/y.md", "abstract": "the summary"},
            )

        converter = types.SimpleNamespace()
        receipts = []
        backfill._install_receipt_capture(converter, base_from_context, receipts)
        result = converter.from_context(context)

        self.assertEqual(result.message, "the summary")
        self.assertEqual(len(receipts), 1)
        entry = receipts[0]
        self.assertEqual(entry["uri"], "viking://x/y.md")
        self.assertEqual(entry["old_abstract_len"], len(b"the full body"))
        self.assertEqual(entry["new_abstract_len"], len(b"the summary"))
        self.assertEqual(
            entry["embedding_sha256"], backfill._embedding_text_sha256("the summary")
        )

    def test_none_result_is_not_captured(self):
        converter = types.SimpleNamespace()
        receipts = []
        backfill._install_receipt_capture(converter, lambda ctx: None, receipts)
        self.assertIsNone(converter.from_context(types.SimpleNamespace(abstract="x")))
        self.assertEqual(receipts, [])

    def test_restore_receipt_shows_equal_lengths(self):
        """--restore calls the unwrapped (stock) implementation, so old and new
        abstract lengths should agree — the patch never gets a chance to shrink it."""

        def stock(ctx_arg):
            return types.SimpleNamespace(
                message=ctx_arg.abstract,
                context_data={"uri": "u", "abstract": ctx_arg.abstract},
            )

        @functools.wraps(stock)
        def patched(ctx_arg):
            msg = stock(ctx_arg)
            msg.context_data["abstract"] = "SUMMARY ONLY"
            return msg

        base = backfill._resolve_base_from_context(patched, restore=True)
        converter = types.SimpleNamespace()
        receipts = []
        backfill._install_receipt_capture(converter, base, receipts)
        converter.from_context(types.SimpleNamespace(abstract="full body text"))

        self.assertEqual(
            receipts[0]["old_abstract_len"], receipts[0]["new_abstract_len"]
        )


EV = "viking://user/u/peers/p/memories/events"


class RootValidationTests(unittest.TestCase):
    """Codex P1: the tool must refuse anything but the user's events namespace."""

    def test_accepted_roots(self):
        for root in (
            EV,
            EV + "/",
            "viking://user/u/memories/events",
            EV + "/2026",
            EV + "/2026/09",
            EV + "/2026/09/24",
            EV + "/2026/09/24/kinde_login_attempts.md",
        ):
            backfill.validate_root(root, "u")

    def test_rejected_roots(self):
        for root in (
            "viking://resources/compendium",
            "viking://user/u/peers/p/memories/entities",
            "viking://user/u/sessions",
            "viking://user/u/peers/p/memories/events/notes",
            EV + "/2026/09/24/sub/x.md",
            EV + "/2026/09/24/x.txt",
        ):
            with self.assertRaises(ValueError, msg=root):
                backfill.validate_root(root, "u")

    def test_other_users_root_rejected(self):
        with self.assertRaises(ValueError):
            backfill.validate_root("viking://user/other/memories/events", "u")

    def test_parse_args_refuses_a_resources_root(self):
        with self.assertRaises(SystemExit):
            backfill.parse_args(["viking://resources/compendium", "--user", "u"])


class SelectionValidationTests(unittest.TestCase):
    def test_event_files_under_root_pass(self):
        backfill.validate_selection(
            EV + "/2026/09",
            "u",
            [EV + "/2026/09/24/a.md", EV + "/2026/09/25/b.md"],
        )

    def test_any_stray_uri_aborts_the_run(self):
        for stray in (
            "viking://resources/compendium/bugs/x.md",
            EV + "/2026/10/01/outside_the_root.md",
            "viking://user/u/peers/other/memories/events/2026/09/24/a.md",
            EV + "/2026/09/.overview.md",
            EV + "/2026/09/24/nested/a.md",
        ):
            with self.assertRaises(SystemExit, msg=stray):
                backfill.validate_selection(EV + "/2026/09", "u", [stray])


class TemplateGuardTests(unittest.TestCase):
    def test_v0420_events_template_passes(self):
        schema = types.SimpleNamespace(
            embedding_template="EventName: {{ event_name }}\nGoal: {{ goal }}\n{{ content }}"
        )
        backfill.refuse_context_templates(schema)
        backfill.refuse_context_templates(
            types.SimpleNamespace(embedding_template=None)
        )

    def test_extract_context_template_refused(self):
        schema = types.SimpleNamespace(
            embedding_template="{{ extract_context.marker }} {{ content }}"
        )
        with self.assertRaises(SystemExit):
            backfill.refuse_context_templates(schema)


class ReceiptLogTests(unittest.TestCase):
    def test_each_record_is_written_as_it_happens(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "r.jsonl")
            log = backfill.ReceiptLog(backfill.Path(path))
            log.append({"stage": "selected", "uri": "a"})
            with open(path) as fh:  # visible before close: survives an interruption
                self.assertEqual(json.loads(fh.readline())["stage"], "selected")
            log.append({"stage": "persisted", "uri": "a"})
            log.close()
            with open(path) as fh:
                self.assertEqual(len(fh.readlines()), 2)
            self.assertEqual(len(log), 2)
            self.assertIn("ts", log[0])


class EnqueueRecorderTests(unittest.TestCase):
    def _msg(self, uri):
        return types.SimpleNamespace(context_data={"uri": uri})

    def test_outcomes_are_recorded_per_uri(self):
        results = iter([True, False])

        class Inner:
            other = "passthrough"

            async def enqueue_embedding_msg(self, msg):
                return next(results)

        receipts = []
        rec = backfill._EnqueueRecorder(Inner(), receipts)
        self.assertEqual(rec.other, "passthrough")
        asyncio.run(rec.enqueue_embedding_msg(self._msg("a")))
        asyncio.run(rec.enqueue_embedding_msg(self._msg("b")))
        self.assertEqual(
            [(r["stage"], r["uri"]) for r in receipts],
            [("enqueued", "a"), ("enqueue_failed", "b")],
        )

    def test_an_exception_is_recorded_and_reraised(self):
        class Inner:
            async def enqueue_embedding_msg(self, msg):
                raise RuntimeError("queue closed")

        receipts = []
        rec = backfill._EnqueueRecorder(Inner(), receipts)
        with self.assertRaises(RuntimeError):
            asyncio.run(rec.enqueue_embedding_msg(self._msg("a")))
        self.assertEqual(receipts[0]["stage"], "enqueue_failed")
        self.assertIn("queue closed", receipts[0]["error"])


class VerifyPersistedTests(unittest.TestCase):
    """Codex P1/P2: success means the record reads back with the enqueued abstract."""

    def test_persisted_missing_mismatch_and_not_converted(self):
        stored = {"id:a": "SUMMARY A", "id:c": "SOMETHING ELSE"}

        class DB:
            async def get(self, ids, ctx):
                return [{"abstract": stored[i]} for i in ids if i in stored]

        receipts = [
            {
                "stage": "converted",
                "uri": "a",
                "new_abstract_sha256": backfill._sha256("SUMMARY A"),
            },
            {
                "stage": "converted",
                "uri": "b",
                "new_abstract_sha256": backfill._sha256("SUMMARY B"),
            },
            {
                "stage": "converted",
                "uri": "c",
                "new_abstract_sha256": backfill._sha256("SUMMARY C"),
            },
        ]
        ctx = types.SimpleNamespace(account_id="default")
        asyncio.run(
            backfill._verify_persisted(
                DB(),
                ctx,
                ["a", "b", "c", "d"],
                receipts,
                record_id=lambda account, uri, level: f"id:{uri}",
            )
        )
        final = {r["uri"]: r["stage"] for r in receipts[3:]}
        self.assertEqual(
            final,
            {"a": "persisted", "b": "missing", "c": "mismatch", "d": "not_converted"},
        )
        summary = backfill._summarize(
            "backfill", ["a", "b", "c", "d"], receipts, drained=True
        )
        self.assertFalse(summary["complete"])
        self.assertEqual(summary["persisted"], 1)

    def test_polls_until_the_record_lands_or_times_out(self):
        """The queue is shared with the live server, so a record can land after this
        process's counters read complete; read-back is retried until the deadline."""
        want = backfill._sha256("SUMMARY")
        answers = {
            "a": iter([None, "OLD FULL BODY", "SUMMARY"]),
            "b": iter([None] * 50),
        }

        class DB:
            async def get(self, ids, ctx):
                value = next(answers[ids[0]])
                return [{"abstract": value}] if value is not None else []

        now = [0.0]

        async def fake_sleep(seconds):
            now[0] += seconds

        receipts = [
            {"stage": "converted", "uri": "a", "new_abstract_sha256": want},
            {"stage": "converted", "uri": "b", "new_abstract_sha256": want},
        ]
        asyncio.run(
            backfill._verify_persisted(
                DB(),
                types.SimpleNamespace(account_id="default"),
                ["a", "b"],
                receipts,
                record_id=lambda account, uri, level: uri,
                timeout=10,
                interval=2,
                sleep=fake_sleep,
                clock=lambda: now[0],
            )
        )
        final = {r["uri"]: r["stage"] for r in receipts[2:]}
        self.assertEqual(final, {"a": "persisted", "b": "missing"})
        self.assertGreaterEqual(now[0], 10)

    def test_complete_requires_every_uri_persisted_and_a_drained_queue(self):
        receipts = [{"stage": "persisted", "uri": "a"}]
        self.assertTrue(
            backfill._summarize("backfill", ["a"], receipts, drained=True)["complete"]
        )
        self.assertFalse(
            backfill._summarize("backfill", ["a"], receipts, drained=False)["complete"]
        )


class DryRunIsReadOnlyTests(unittest.TestCase):
    """Codex P1: --dry-run must never build a writable OpenVikingService."""

    def test_dry_run_uses_rest_only(self):
        import tempfile

        calls = []

        class FakeReader:
            def __init__(self, server, api_key, account, user):
                calls.append(("init", server, account, user))

            def list_files(self, root):
                return [root + "/2026/09/24/a.md"]

        def fake_report(reader, uris, receipts):
            for uri in uris:
                receipts.append(
                    {
                        "stage": "dry_run",
                        "uri": uri,
                        "old_abstract_len": 9,
                        "new_abstract_len": 3,
                        "would_change": True,
                    }
                )

        saved = backfill._RestReader, backfill._dry_run_report
        backfill._RestReader, backfill._dry_run_report = FakeReader, fake_report
        before = set(sys.modules)
        try:
            with (
                tempfile.TemporaryDirectory() as tmp,
                unittest.mock.patch.dict(os.environ, {"OPENVIKING_API_KEY": "k"}),
            ):
                rc = backfill.main(
                    [
                        EV,
                        "--user",
                        "u",
                        "--dry-run",
                        "--receipt",
                        os.path.join(tmp, "r.jsonl"),
                    ]
                )
        finally:
            backfill._RestReader, backfill._dry_run_report = saved
        self.assertEqual(rc, 0)
        self.assertEqual(calls[0][0], "init")
        self.assertNotIn("openviking.service.core", set(sys.modules) - before)

    def test_dry_run_without_a_key_stops(self):
        import tempfile

        with (
            tempfile.TemporaryDirectory() as tmp,
            unittest.mock.patch.dict(os.environ, {}, clear=True),
        ):
            rc = backfill.main(
                [
                    EV,
                    "--user",
                    "u",
                    "--dry-run",
                    "--receipt",
                    os.path.join(tmp, "r.jsonl"),
                ]
            )
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
