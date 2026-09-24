"""Tests for ov_memory_guard_patch.

    python3 test_ov_memory_guard_patch.py

The guard's semantics run on fakes anywhere. The ``Installed`` cases need the openviking
v0.4.20 image (run with PYTHONPATH=<this dir> /app/.venv/bin/python …) and are skipped
elsewhere: they check the version/source-hash guard against the real ``MemoryUpdater``
and read a summary back through the real ``MemoryFileUtils``.
"""

import asyncio
import dataclasses
import importlib.util
import os
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ov_memory_guard_patch as mg

# The interpreter has already imported *a* sitecustomize (Homebrew's, or ours via
# PYTHONPATH in the pod), so load this directory's copy by path under its own name.
_spec = importlib.util.spec_from_file_location(
    "ov_sitecustomize", os.path.join(HERE, "sitecustomize.py")
)
sitecustomize = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sitecustomize)

BASE = "viking://user/u/peers/p/memories/events/2026/09/24"


class FakeFS:
    def __init__(self, files=None):
        self.files = dict(files or {})

    async def read_file(self, uri, ctx=None):
        if uri not in self.files:
            raise FileNotFoundError(uri)
        return self.files[uri]


class FakeMemoryFileUtils:
    @staticmethod
    def read(content, uri=None):
        summary = content.split("\n", 1)[0].removeprefix("summary: ")
        return types.SimpleNamespace(extra_fields={"summary": summary})


FAKE_MODULE = types.SimpleNamespace(MemoryFileUtils=FakeMemoryFileUtils)


MODES = {"events": "add_only", "trajectories": "add_only", "entities": "upsert"}


class Registry:
    def get(self, memory_type):
        return types.SimpleNamespace(operation_mode=MODES[memory_type])


def op(name, summary, memory_type="events", old=None):
    return types.SimpleNamespace(
        memory_type=memory_type,
        uris=[f"{BASE}/{name}.md"],
        memory_fields={"summary": summary, "event_name": name},
        old_memory_file_content=old,
    )


def ops(*items, links=()):
    return types.SimpleNamespace(
        upsert_operations=list(items),
        resolved_links=list(links),
        has_errors=lambda: False,
    )


def run(fs, operations, tags=None):
    updater = types.SimpleNamespace(_registry=Registry(), _get_viking_fs=lambda: fs)
    return asyncio.run(mg.guard_add_only(FAKE_MODULE, updater, operations, None, tags))


def stored(summary):
    return f"summary: {summary}\n# ChatLog"


class GuardSemantics(unittest.TestCase):
    def test_free_path_is_untouched(self):
        o = op("kinde_login_attempts", "Attempted login.")
        batch = ops(o)
        self.assertEqual(run(FakeFS(), batch), ([], []))
        self.assertEqual(o.uris, [f"{BASE}/kinde_login_attempts.md"])
        self.assertEqual(batch.upsert_operations, [o])

    def test_existing_memory_with_the_same_summary_is_dropped(self):
        fs = FakeFS({f"{BASE}/kinde_login_attempts.md": stored("Attempted login.")})
        batch = ops(op("kinde_login_attempts", "  Attempted login.  "))
        dropped, diverted = run(fs, batch)
        self.assertEqual(dropped, [(f"{BASE}/kinde_login_attempts.md", None)])
        self.assertEqual((diverted, batch.upsert_operations), ([], []))

    def test_existing_memory_with_a_different_summary_is_not_overwritten(self):
        target = f"{BASE}/kinde_login_attempts.md"
        fs = FakeFS({target: stored("Attempted login; device flow failed.")})
        o = op("kinde_login_attempts", "Pasted recall output.", old=object())
        batch = ops(o)
        _, diverted = run(fs, batch)
        self.assertEqual(diverted, [(target, f"{BASE}/kinde_login_attempts_2.md")])
        self.assertEqual(o.uris, [f"{BASE}/kinde_login_attempts_2.md"])
        self.assertIsNone(o.old_memory_file_content, "must report as a new write")
        self.assertIn(target, fs.files, "the original is left alone")

    def test_next_free_sibling_is_used(self):
        fs = FakeFS(
            {
                f"{BASE}/x.md": stored("a"),
                f"{BASE}/x_2.md": stored("b"),
            }
        )
        o = op("x", "c")
        run(fs, ops(o))
        self.assertEqual(o.uris, [f"{BASE}/x_3.md"])

    def test_same_path_twice_in_one_batch(self):
        first, second = op("x", "one"), op("x", "two")
        run(FakeFS(), ops(first, second))
        self.assertEqual(first.uris, [f"{BASE}/x.md"])
        self.assertEqual(second.uris, [f"{BASE}/x_2.md"])

    def test_identical_twice_in_one_batch_keeps_one(self):
        batch = ops(op("x", "same"), op("x", "same"))
        dropped, _ = run(FakeFS(), batch)
        self.assertEqual(len(batch.upsert_operations), 1)
        self.assertEqual(dropped, [(f"{BASE}/x.md", None)])

    def test_upsert_types_still_merge(self):
        path = f"{BASE}/person.md"
        o = op("person", "new", memory_type="entities")
        o.uris = [path]
        run(FakeFS({path: stored("old")}), ops(o))
        self.assertEqual(o.uris, [path])

    def test_trajectories_are_guarded_too(self):
        o = op("t", "new", memory_type="trajectories")
        run(FakeFS({f"{BASE}/t.md": stored("old")}), ops(o))
        self.assertEqual(o.uris, [f"{BASE}/t_2.md"])

    def test_unparseable_existing_file_counts_as_occupied(self):
        class Broken(FakeMemoryFileUtils):
            @staticmethod
            def read(content, uri=None):
                raise ValueError("bad frontmatter")

        module = types.SimpleNamespace(MemoryFileUtils=Broken)
        o = op("x", "s")
        updater = types.SimpleNamespace(
            _registry=Registry(), _get_viking_fs=lambda: FakeFS({f"{BASE}/x.md": "?"})
        )
        asyncio.run(mg.guard_add_only(module, updater, ops(o), None))
        self.assertEqual(o.uris, [f"{BASE}/x_2.md"])

    def test_links_and_search_tags_follow_the_diversion(self):
        target, other = f"{BASE}/x.md", f"{BASE}/y.md"
        links = [
            types.SimpleNamespace(from_uri=target, to_uri=other),
            types.SimpleNamespace(from_uri=other, to_uri=target),
        ]
        tags = {target: ["peer=p"]}
        run(FakeFS({target: stored("old")}), ops(op("x", "new"), links=links), tags)
        moved = f"{BASE}/x_2.md"
        self.assertEqual((links[0].from_uri, links[1].to_uri), (moved, moved))
        self.assertEqual(tags[moved], ["peer=p"])

    def test_nothing_happens_when_the_batch_already_has_errors(self):
        batch = ops(op("x", "new"))
        batch.has_errors = lambda: True
        self.assertEqual(run(FakeFS({f"{BASE}/x.md": stored("old")}), batch), ([], []))

    def test_sibling_uri(self):
        self.assertEqual(mg.sibling_uri(f"{BASE}/a.md", 2), f"{BASE}/a_2.md")
        self.assertEqual(mg.sibling_uri("viking://x/a.b/c", 3), "viking://x/a.b/c_3")


class WrapperTests(unittest.TestCase):
    def test_a_failing_guard_falls_through_to_the_stock_body(self):
        calls = []

        async def stock(
            self, operations, ctx, extract_context, isolation_handler, tags
        ):
            calls.append(operations)
            return "result"

        wrapped = mg.wrap_apply_operations(FAKE_MODULE, stock)
        updater = types.SimpleNamespace(_registry=None, _get_viking_fs=None)  # raises
        out = asyncio.run(wrapped(updater, "ops", None))
        self.assertEqual((out, calls), ("result", ["ops"]))
        self.assertTrue(wrapped._ov_memory_guard)


class ApplyGuardTests(unittest.TestCase):
    def setUp(self):
        self.saved = sys.modules.get("openviking")

    def tearDown(self):
        if self.saved is None:
            sys.modules.pop("openviking", None)
        else:
            sys.modules["openviking"] = self.saved
        os.environ.pop("OV_MEMORY_GUARD", None)

    def fake(self, version):
        ov = types.ModuleType("openviking")
        ov.__version__ = version
        sys.modules["openviking"] = ov

        class MemoryUpdater:
            async def apply_operations(self, operations, ctx):
                return None

            async def _apply_upsert(self, op, ctx):
                return None

        module = types.ModuleType("openviking.session.memory.memory_updater")
        module.MemoryUpdater = MemoryUpdater
        module.MemoryFileUtils = FakeMemoryFileUtils
        return module, MemoryUpdater

    def test_refuses_wrong_version(self):
        module, cls = self.fake("v0.4.99")
        orig = cls.apply_operations
        self.assertFalse(mg.apply(module))
        self.assertIs(cls.apply_operations, orig)

    def test_refuses_source_drift_on_the_right_version(self):
        module, cls = self.fake(mg.EXPECTED_VERSION)
        orig = cls.apply_operations
        self.assertFalse(mg.apply(module))
        self.assertIs(cls.apply_operations, orig)

    def test_kill_switch(self):
        module, _ = self.fake(mg.EXPECTED_VERSION)
        os.environ["OV_MEMORY_GUARD"] = "0"
        self.assertFalse(mg.apply(module))

    def test_already_applied_is_idempotent(self):
        module, cls = self.fake(mg.EXPECTED_VERSION)
        cls.apply_operations = mg.wrap_apply_operations(module, cls.apply_operations)
        self.assertTrue(mg.apply(module))


class LoaderTests(unittest.TestCase):
    def test_memory_updater_gets_both_patches_in_order(self):
        entries = sitecustomize._entries(
            sitecustomize.TARGETS["openviking.session.memory.memory_updater"]
        )
        self.assertEqual(
            entries,
            [
                ("ov_chatlog_patch", "apply"),
                ("ov_memory_guard_patch", "apply"),
                ("ov_memory_guard_patch", "apply_echo_guard"),
            ],
        )

    def test_single_pair_targets_still_load(self):
        self.assertEqual(
            sitecustomize._entries(("ov_nav_patch", "apply")),
            [("ov_nav_patch", "apply")],
        )


# The first lines of the real BUG-1180 paste (session bc765214, message 0), shortened.
PASTE = (
    "❯ what did we look at regarding the Kinde CLI?\n\n"
    "⏺ plugin:openviking-memory:openviking - read (MCP)(uris: "
    '["viking://user/noot-pilot/peers/p/memories/events/2026/09/24/kinde-cli_installation.md"])\n'
    "# Summary\nInstalled kinde-cli v0.1.20 via Homebrew.\n"
    "# 2026-09-24 (Thursday) ChatLog:\n**p**: let's look into kinde-cli\n"
    "- [memory 68%] viking://user/noot-pilot/peers/p/memories/events/2026/09/24/x.md\n"
    "    # Summary\nDetermined that manage needs an M2M app.\n"
)


class EchoStripTests(unittest.TestCase):
    def test_paste_is_cut_at_the_first_recall_line(self):
        self.assertEqual(
            mg.strip_recall_text(PASTE),
            "❯ what did we look at regarding the Kinde CLI?\n\n" + mg.PASTE_PLACEHOLDER,
        )

    def test_each_marker_alone_triggers_the_cut(self):
        for line in (
            "⏺ plugin:openviking-memory:openviking - search (MCP)(query: x)",
            "- [resource 66%] viking://resources/compendium/tasks/a.md",
            "# 2026-09-22 (Tuesday) ChatLog:",
        ):
            got = mg.strip_recall_text(f"keep this\n{line}\ndrop this")
            self.assertEqual(got, f"keep this\n\n{mg.PASTE_PLACEHOLDER}", line)

    def test_a_paste_with_nothing_before_it_becomes_the_placeholder(self):
        text = "# 2026-09-24 (Thursday) ChatLog:\n**p**: hi"
        self.assertEqual(mg.strip_recall_text(text), mg.PASTE_PLACEHOLDER)

    def test_ordinary_user_text_is_untouched(self):
        for text in (
            "look at viking://user/noot-pilot/memories/events/2026/09/24/x.md",
            "the header is `# 2026-09-22 (Tuesday) ChatLog:` in the body",
            "we saw [memory 68%] in the output",
            "",
        ):
            self.assertEqual(mg.strip_recall_text(text), text)

    def test_injected_context_block_is_removed_and_the_rest_kept(self):
        text = (
            'before <openviking-context n="2">\nrecalled\n</openviking-context> after'
        )
        self.assertEqual(
            mg.strip_recall_text(text), f"before {mg.CONTEXT_PLACEHOLDER} after"
        )


@dataclasses.dataclass
class FakeText:
    text: str = ""


@dataclasses.dataclass
class FakeTool:
    output: str = ""


@dataclasses.dataclass
class FakeMessage:
    id: str
    role: str
    parts: list


class EchoMessageTests(unittest.TestCase):
    def test_user_message_copy_is_stripped_and_other_parts_kept(self):
        tool = FakeTool("⏺ plugin:openviking-memory:openviking - read")
        msg = FakeMessage("m1", "user", [FakeText(PASTE), tool])
        got = mg.strip_recall_message(msg, FakeText)
        self.assertIsNot(got, msg)
        self.assertEqual(msg.parts[0].text, PASTE, "the original is not mutated")
        self.assertTrue(got.parts[0].text.endswith(mg.PASTE_PLACEHOLDER))
        self.assertIs(got.parts[1], tool)

    def test_assistant_and_clean_messages_are_returned_as_is(self):
        for msg in (
            FakeMessage("a", "assistant", [FakeText(PASTE)]),
            FakeMessage("u", "user", [FakeText("merge them")]),
        ):
            self.assertIs(mg.strip_recall_message(msg, FakeText), msg)


try:
    from openviking.session.memory import memory_updater as installed
except ImportError:  # not in the openviking image
    installed = None


@unittest.skipIf(installed is None, "needs the openviking v0.4.20 image")
class Installed(unittest.TestCase):
    def test_hook_applied_the_guard_on_import(self):
        self.assertTrue(
            getattr(installed.MemoryUpdater.apply_operations, "_ov_memory_guard", False)
        )
        self.assertEqual(
            mg._source_hash(installed.MemoryUpdater.apply_operations.__wrapped__),
            mg.EXPECTED_APPLY_SHA256,
        )
        self.assertEqual(
            mg._source_hash(installed.MemoryUpdater._apply_upsert),
            mg.EXPECTED_UPSERT_SHA256,
        )

    def test_summary_reads_back_through_the_real_memory_file_utils(self):
        uri = f"{BASE}/kinde_login_attempts.md"
        mf = installed.MemoryFile.from_parsed(
            uri=uri,
            parsed={
                "memory_type": "events",
                "summary": "Attempted login.",
                "content": "# Summary\nAttempted login.",
            },
        )
        content = installed.MemoryFileUtils.write(mf)
        existing = asyncio.run(
            mg._existing(installed, FakeFS({uri: content}), uri, None)
        )
        self.assertEqual(mg._summary(existing), "Attempted login.")

    def test_echo_guard_applied_on_import(self):
        init = installed.ExtractContext.__init__
        self.assertTrue(getattr(init, "_ov_echo_guard", False))
        self.assertEqual(
            mg._source_hash(init.__wrapped__), mg.EXPECTED_EXTRACT_INIT_SHA256
        )

    def test_extract_context_strips_the_paste_and_keeps_indices(self):
        from openviking.message import Message

        msgs = [
            Message(id="u0", role="user", parts=[installed.TextPart(PASTE)]),
            Message(id="a1", role="assistant", parts=[installed.TextPart("Here.")]),
            Message(id="u2", role="user", parts=[installed.TextPart("merge them")]),
        ]
        ctx = installed.ExtractContext(msgs)
        self.assertEqual([m.id for m in ctx.messages], ["u0", "a1", "u2"])
        self.assertTrue(ctx.messages[0].parts[0].text.endswith(mg.PASTE_PLACEHOLDER))
        self.assertIn(PASTE, msgs[0].parts[0].text, "session messages untouched")
        chatlog = ctx.read_message_ranges("0-2").pretty_print()
        self.assertNotIn("kinde-cli_installation", chatlog)
        self.assertIn("merge them", chatlog)


if __name__ == "__main__":
    unittest.main()
