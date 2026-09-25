"""Tests for ov-event-abstract-backfill.py. Pure (fakes only) — runs anywhere:

    python3 test-ov-event-abstract-backfill.py

``_rebuild`` and ``_dry_run_report`` call into the real openviking package (service
bootstrap, MemoryUpdater, EmbeddingMsgConverter) and are exercised end to end against
ov-test instead — see the IMPR-1200 PR for the receipts from that run. This file
covers the URI walk/filter, the receipt-capture wrapper, the sha256 of a text vs.
multimodal message, the restore ``__wrapped__`` resolution, and argument parsing.
"""

import asyncio
import functools
import importlib.util
import json
import os
import sys
import types
import unittest

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


if __name__ == "__main__":
    unittest.main()
