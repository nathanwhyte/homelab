"""BUG-1189: user-root grep skips session archives, and every grep is time-bounded.

The wrapper cases use a stand-in coroutine, so they check the scoping and timeout
contract, not OpenViking's walker. ``RealTargetTests`` need the installed openviking
(run inside the v0.5.1.1 image): they apply the guard to the real ``FSService.grep``
and drive the real ``_collect_grep_files`` walker over a fake tree, showing a
user-root grep still descends into ``sessions/`` unless it is excluded. The live
check is the BUG-1189 verification (a user-root grep returns inside the MCP limit).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "_scripts" / "lib"))
from git_env import scrub_environ  # noqa: E402

scrub_environ()

import asyncio
import importlib.util
import os
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ov_grep_scope_patch as gs

HAVE_OPENVIKING = importlib.util.find_spec("openviking") is not None


class FakeFSService:
    """Same signature as v0.5.1.1 ``FSService.grep``; records what it was called with."""

    def __init__(self, delay=0.0):
        self.calls = []
        self.delay = delay

    async def grep(
        self,
        uri,
        pattern,
        ctx,
        exclude_uri=None,
        case_insensitive=False,
        node_limit=None,
        level_limit=10,
        tags=None,
        include_tags=False,
        before_context=0,
        after_context=0,
    ):
        self.calls.append(
            {
                "uri": uri,
                "exclude_uri": exclude_uri,
                "ctx": ctx,
                "before_context": before_context,
                "after_context": after_context,
            }
        )
        if self.delay:
            await asyncio.sleep(self.delay)
        return {"matches": [{"uri": uri, "line": 1, "content": "hit"}], "count": 1}


def wrapped(delay=0.0):
    svc = FakeFSService(delay)
    fn = gs.scoped_grep(FakeFSService.grep)
    return svc, lambda *a, **k: asyncio.run(fn(svc, *a, **k))


class DefaultExcludeTests(unittest.TestCase):
    def test_user_root_excludes_its_sessions(self):
        for uri in ("viking://user/noot-pilot", "viking://user/noot-pilot/"):
            self.assertEqual(
                gs.default_exclude(uri, None), "viking://user/noot-pilot/sessions"
            )

    def test_explicit_exclude_wins(self):
        self.assertEqual(
            gs.default_exclude(
                "viking://user/noot-pilot/", "viking://user/noot-pilot/peers"
            ),
            "viking://user/noot-pilot/peers",
        )

    def test_subtrees_and_other_roots_are_untouched(self):
        for uri in (
            "viking://user/noot-pilot/memories/",
            "viking://user/noot-pilot/sessions/",
            "viking://user/noot-pilot/sessions/cc-1/history",
            "viking://user/",
            "viking://resources/compendium/",
            "viking://",
            "",
            None,
        ):
            self.assertIsNone(gs.default_exclude(uri, None), uri)


class ScopedGrepTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def test_user_root_call_gets_the_exclusion_and_says_so(self):
        # MCP shape: ctx and options as keywords
        svc, grep = wrapped()
        res = grep("viking://user/noot-pilot/", "x", ctx="C", node_limit=10)
        self.assertEqual(
            svc.calls[0]["exclude_uri"], "viking://user/noot-pilot/sessions"
        )
        self.assertEqual(svc.calls[0]["ctx"], "C")
        self.assertEqual(
            res["excluded_by_default"], "viking://user/noot-pilot/sessions"
        )
        self.assertEqual(res["count"], 1)

    def test_positional_ctx_and_explicit_exclude_pass_through(self):
        # REST shape passes exclude_uri explicitly (None when the caller set none)
        svc, grep = wrapped()
        res = grep(
            "viking://user/noot-pilot/",
            "x",
            "C",
            exclude_uri="viking://user/noot-pilot/peers",
        )
        self.assertEqual(svc.calls[0]["exclude_uri"], "viking://user/noot-pilot/peers")
        self.assertNotIn("excluded_by_default", res)

    def test_context_lines_pass_through(self):
        # REST shape (v0.5.1.1, #5168): before_context/after_context reach the original
        svc, grep = wrapped()
        grep(
            "viking://user/noot-pilot/",
            "x",
            ctx="C",
            exclude_uri=None,
            before_context=2,
            after_context=3,
        )
        self.assertEqual(svc.calls[0]["before_context"], 2)
        self.assertEqual(svc.calls[0]["after_context"], 3)
        self.assertEqual(
            svc.calls[0]["exclude_uri"], "viking://user/noot-pilot/sessions"
        )
        svc, grep = wrapped()
        grep("viking://user/noot-pilot/memories/", "x", ctx="C")
        self.assertEqual(svc.calls[0]["before_context"], 0)
        self.assertEqual(svc.calls[0]["after_context"], 0)

    def test_explicit_sessions_grep_still_searches_sessions(self):
        svc, grep = wrapped()
        grep("viking://user/noot-pilot/sessions/cc-1/", "x", ctx="C")
        self.assertIsNone(svc.calls[0]["exclude_uri"])

    def test_timeout_returns_a_marked_result_not_an_empty_one(self):
        # an empty result would read as "no matches" to the MCP tool, which swallows
        # exceptions into [] — the timeout must be visible as a result line
        with patch.dict(os.environ, {"OV_GREP_TIMEOUT_S": "0.05"}):
            _, grep = wrapped(delay=1.0)
            res = grep("viking://user/noot-pilot/memories/", "x", ctx="C")
        self.assertTrue(res["timed_out"])
        self.assertEqual(len(res["matches"]), 1)
        self.assertIn("timed out after 0.05 s", res["matches"][0]["content"])
        self.assertIn("incomplete", res["matches"][0]["content"])
        self.assertEqual(res["matches"][0]["uri"], "viking://user/noot-pilot/memories/")

    def test_timeout_zero_disables_the_bound(self):
        with patch.dict(os.environ, {"OV_GREP_TIMEOUT_S": "0"}):
            _, grep = wrapped(delay=0.1)
            res = grep("viking://user/noot-pilot/memories/", "x", ctx="C")
        self.assertNotIn("timed_out", res)

    def test_timeout_setting(self):
        for raw, want in (
            (None, gs.DEFAULT_TIMEOUT_S),
            ("5", 5.0),
            ("0", None),
            ("-1", None),
            ("junk", gs.DEFAULT_TIMEOUT_S),
        ):
            env = {} if raw is None else {"OV_GREP_TIMEOUT_S": raw}
            with patch.dict(os.environ, env, clear=True):
                self.assertEqual(gs.timeout_s(), want, raw)

    def test_errors_propagate(self):
        async def boom(self, uri, pattern, ctx, exclude_uri=None):
            raise ValueError("bad pattern")

        with self.assertRaisesRegex(ValueError, "bad pattern"):
            asyncio.run(gs.scoped_grep(boom)(None, "viking://user/u/", "(", "C"))


class ApplyTests(unittest.TestCase):
    def test_guards_and_idempotence(self):
        cls = type("FSService", (), {"grep": FakeFSService.grep})
        module = types.SimpleNamespace(FSService=cls)
        ov = types.SimpleNamespace(__version__="v0.4.99")
        with (
            patch.dict(sys.modules, {"openviking": ov}),
            patch.dict(os.environ, {}, clear=True),
        ):
            self.assertFalse(gs.apply(module))
            ov.__version__ = gs.EXPECTED_VERSION
            self.assertFalse(gs.apply(module))  # source hash differs
            digest = gs.hashlib.sha256(
                gs.inspect.getsource(FakeFSService.grep).encode()
            ).hexdigest()
            with patch.object(gs, "EXPECTED_SHA256", digest):
                with patch.dict(os.environ, {"OV_GREP_SCOPE_PATCH": "0"}):
                    self.assertFalse(gs.apply(module))
                self.assertTrue(gs.apply(module))
                installed = cls.grep
                self.assertTrue(installed._ov_grep_scope_patch)
                self.assertTrue(gs.apply(module))
                self.assertIs(cls.grep, installed)


ROOT = "viking://user/noot-pilot/"
TREE = {
    "viking://user/noot-pilot": [("memories", True), ("sessions", True)],
    "viking://user/noot-pilot/memories": [("a.md", False)],
    "viking://user/noot-pilot/sessions": [("cc-1", True)],
    "viking://user/noot-pilot/sessions/cc-1": [("history", True)],
    "viking://user/noot-pilot/sessions/cc-1/history": [("archive_001.md", False)],
}


class FakeVikingFS:
    """The two calls ``_collect_grep_files`` makes, over ``TREE``; records ``grep`` kwargs."""

    def __init__(self):
        self.listed = []
        self.grep_calls = []

    async def ls(self, uri, node_limit=None, offset=0, ctx=None):
        uri = uri.rstrip("/")
        self.listed.append(uri)
        entries = [{"name": n, "isDir": d} for n, d in TREE.get(uri, [])]
        return entries[offset : offset + node_limit]

    async def stat(self, uri, ctx=None, skip_count=False):
        return {"isDir": uri.rstrip("/") in TREE}

    async def grep(self, uri, pattern, **kwargs):
        self.grep_calls.append({"uri": uri, **kwargs})
        return {"matches": [], "count": 0}


@unittest.skipUnless(
    HAVE_OPENVIKING, "needs the installed openviking (run in the image)"
)
class RealTargetTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        from openviking.service import fs_service

        self.fs_service = fs_service
        # sitecustomize may already have wrapped it; the guard hashes the stock function
        self.stock = getattr(
            fs_service.FSService.grep, "__wrapped__", fs_service.FSService.grep
        )

    def test_guard_matches_the_real_target(self):
        cls = type("FSService", (), {"grep": self.stock})
        module = types.SimpleNamespace(FSService=cls)
        self.assertTrue(gs.apply(module))
        self.assertTrue(cls.grep._ov_grep_scope_patch)
        self.assertIs(cls.grep.__wrapped__, self.stock)

    def run_stock_grep(self, wrap, **kwargs):
        vfs = FakeVikingFS()
        svc = self.fs_service.FSService.__new__(self.fs_service.FSService)
        svc._ensure_initialized = lambda: vfs
        fn = gs.scoped_grep(self.stock) if wrap else self.stock
        asyncio.run(fn(svc, ROOT, "x", ctx=None, **kwargs))
        return vfs.grep_calls[0]

    def test_user_root_grep_takes_the_python_walker(self):
        # a content_transform forces the fs engine and skips the native fast path
        call = self.run_stock_grep(wrap=False, before_context=1, after_context=2)
        self.assertIsNotNone(call.get("content_transform"))
        self.assertIsNone(call["exclude_uri"])
        self.assertEqual((call["before_context"], call["after_context"]), (1, 2))
        call = self.run_stock_grep(wrap=True, before_context=1, after_context=2)
        self.assertEqual(call["exclude_uri"], "viking://user/noot-pilot/sessions")
        self.assertEqual((call["before_context"], call["after_context"]), (1, 2))

    def test_stock_walker_descends_into_sessions_unless_excluded(self):
        from openviking.storage.viking_fs._grep import _GrepMixin

        def collect(excluded):
            vfs = FakeVikingFS()
            files = asyncio.run(
                _GrepMixin._collect_grep_files(vfs, ROOT, excluded, level_limit=10)
            )
            return files, vfs.listed

        files, listed = collect(None)
        self.assertIn(
            "viking://user/noot-pilot/sessions/cc-1/history/archive_001.md", files
        )
        self.assertIn("viking://user/noot-pilot/sessions/cc-1/history", listed)
        files, listed = collect(gs.default_exclude(ROOT, None))
        self.assertEqual(files, ["viking://user/noot-pilot/memories/a.md"])
        self.assertFalse(any("/sessions" in u for u in listed), listed)


if __name__ == "__main__":
    unittest.main()
