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
    def __init__(self, files=None, failing=()):
        self.files = dict(files or {})
        self.failing = set(failing)

    async def read_file(self, uri, ctx=None):
        if uri in self.failing:
            raise TimeoutError(f"read timed out: {uri}")
        if uri not in self.files:
            raise FileNotFoundError(uri)
        return self.files[uri]


class FakeMemoryFileUtils:
    """Reads ``key: value`` lines up to a ``---`` line as the stored fields."""

    @staticmethod
    def read(content, uri=None):
        fields = {}
        for line in content.split("\n"):
            if line == "---":
                break
            key, _, value = line.partition(": ")
            fields[key] = value
        return types.SimpleNamespace(extra_fields=fields)


FAKE_MODULE = types.SimpleNamespace(MemoryFileUtils=FakeMemoryFileUtils)


MODES = {"events": "add_only", "trajectories": "add_only", "entities": "upsert"}


class Registry:
    def get(self, memory_type):
        return types.SimpleNamespace(operation_mode=MODES[memory_type])


def op(name, summary, memory_type="events", old=None, ranges="0-3"):
    return types.SimpleNamespace(
        memory_type=memory_type,
        uris=[f"{BASE}/{name}.md"],
        memory_fields={"summary": summary, "event_name": name, "ranges": ranges},
        old_memory_file_content=old,
    )


def ops(*items, links=(), replacements=None):
    return types.SimpleNamespace(
        upsert_operations=list(items),
        resolved_links=list(links),
        delete_replacements=dict(replacements or {}),
        has_errors=lambda: False,
    )


def run(fs, operations, tags=None, **kw):
    """``(dropped, diverted)``; the reservation list is checked where it matters."""
    dropped, diverted, _ = asyncio.run(
        mg.guard_add_only(FAKE_MODULE, fs, Registry(), operations, None, tags, **kw)
    )
    return dropped, diverted


def stored(summary, name="x", ranges="0-3"):
    return f"summary: {summary}\nevent_name: {name}\nranges: {ranges}\n---\n# ChatLog"


class GuardSemantics(unittest.TestCase):
    def test_free_path_is_untouched(self):
        o = op("kinde_login_attempts", "Attempted login.")
        batch = ops(o)
        self.assertEqual(run(FakeFS(), batch), ([], []))
        self.assertEqual(o.uris, [f"{BASE}/kinde_login_attempts.md"])
        self.assertEqual(batch.upsert_operations, [o])

    def test_the_same_event_already_stored_is_dropped(self):
        target = f"{BASE}/kinde_login_attempts.md"
        fs = FakeFS({target: stored("Attempted login.", "kinde_login_attempts")})
        batch = ops(op("kinde_login_attempts", "  Attempted login.  "))
        dropped, diverted = run(fs, batch)
        self.assertEqual(dropped, [(target, target)])
        self.assertEqual((diverted, batch.upsert_operations), ([], []))

    def test_a_distinct_event_with_the_same_summary_is_kept(self):
        # Codex P1: two same-day login attempts can share a summary; only equal
        # structured fields (here ranges differ) make a duplicate.
        target = f"{BASE}/x.md"
        fs = FakeFS({target: stored("Attempted login.", ranges="0-3")})
        o = op("x", "Attempted login.", ranges="10-14")
        run(fs, ops(o))
        self.assertEqual(o.uris, [f"{BASE}/x_2.md"])

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

    def test_replaying_a_diverted_write_is_idempotent(self):
        # Codex P2: after x.md collided and the event landed at x_2.md, a retry of the
        # same operation finds it there instead of writing x_3.md.
        fs = FakeFS({f"{BASE}/x.md": stored("other"), f"{BASE}/x_2.md": stored("mine")})
        batch = ops(op("x", "mine"))
        dropped, diverted = run(fs, batch)
        self.assertEqual(dropped, [(f"{BASE}/x.md", f"{BASE}/x_2.md")])
        self.assertEqual((diverted, batch.upsert_operations), ([], []))

    def test_an_unreadable_target_raises_instead_of_counting_as_free(self):
        # Codex P0: a read timeout is not "free"; letting the stock write run would
        # merge into whatever is there.
        o = op("x", "new")
        with self.assertRaises(mg.GuardError):
            run(FakeFS(failing={f"{BASE}/x.md"}), ops(o))
        self.assertEqual(o.uris, [f"{BASE}/x.md"], "op left untouched on failure")

    def test_running_out_of_sibling_slots_raises(self):
        files = {f"{BASE}/x.md": stored("s0")}
        files.update({f"{BASE}/x_{n}.md": stored(f"s{n}") for n in range(2, 51)})
        with self.assertRaises(mg.GuardError):
            run(FakeFS(files), ops(op("x", "new")))

    def test_same_path_twice_in_one_batch(self):
        first, second = op("x", "one"), op("x", "two")
        run(FakeFS(), ops(first, second))
        self.assertEqual(first.uris, [f"{BASE}/x.md"])
        self.assertEqual(second.uris, [f"{BASE}/x_2.md"])

    def test_identical_twice_in_one_batch_keeps_one(self):
        batch = ops(op("x", "same"), op("x", "same"))
        dropped, _ = run(FakeFS(), batch)
        self.assertEqual(len(batch.upsert_operations), 1)
        self.assertEqual(dropped, [(f"{BASE}/x.md", f"{BASE}/x.md")])

    def test_delete_replacements_follow_the_diversion(self):
        target = f"{BASE}/x.md"
        batch = ops(op("x", "new"), replacements={f"{BASE}/old.md": target})
        run(FakeFS({target: stored("other")}), batch)
        self.assertEqual(batch.delete_replacements[f"{BASE}/old.md"], f"{BASE}/x_2.md")

    def test_a_duplicate_found_at_a_sibling_takes_its_references_along(self):
        # Codex P1 (round 2): the event is already at x_2.md; links and replacements
        # that named x.md (another event) must follow to x_2.md.
        target, entity = f"{BASE}/x.md", "viking://user/u/memories/entities/e.md"
        link = types.SimpleNamespace(from_uri=entity, to_uri=target)
        batch = ops(
            op("x", "mine"), links=[link], replacements={f"{BASE}/old.md": target}
        )
        fs = FakeFS({target: stored("other"), f"{BASE}/x_2.md": stored("mine")})
        dropped, _ = run(fs, batch)
        self.assertEqual(dropped, [(target, f"{BASE}/x_2.md")])
        self.assertEqual(link.to_uri, f"{BASE}/x_2.md")
        self.assertEqual(batch.delete_replacements[f"{BASE}/old.md"], f"{BASE}/x_2.md")

    def test_another_extraction_of_an_identical_event_is_kept(self):
        # Codex P1 (round 2): identity includes source_extraction_id, which stock
        # attaches before the guard; only a replay of the same extraction is a duplicate.
        def with_source(extraction):
            o = op("x", "Attempted login.")
            o.memory_fields["source_extraction_id"] = extraction
            return o

        stored_a = (
            "summary: Attempted login.\nevent_name: x\nranges: 0-3\n"
            "source_extraction_id: A\n---\n"
        )
        other = with_source("B")
        run(FakeFS({f"{BASE}/x.md": stored_a}), ops(other))
        self.assertEqual(other.uris, [f"{BASE}/x_2.md"])
        replay = ops(with_source("A"))
        dropped, _ = run(FakeFS({f"{BASE}/x.md": stored_a}), replay)
        self.assertEqual(
            (dropped, replay.upsert_operations), ([(f"{BASE}/x.md",) * 2], [])
        )

    def test_concurrent_submits_reserve_different_slots(self):
        # Codex P1 (round 2): a slot reserved by an in-flight submit is not free.
        reserved = {}
        first, second = op("x", "one"), op("x", "two")
        run(FakeFS(), ops(first), reserved=reserved, reserve=True)
        run(FakeFS(), ops(second), reserved=reserved, reserve=True)
        self.assertEqual(
            (first.uris, second.uris), ([f"{BASE}/x.md"], [f"{BASE}/x_2.md"])
        )
        self.assertEqual(set(reserved), {f"{BASE}/x.md", f"{BASE}/x_2.md"})

    def test_the_apply_pass_verifies_its_own_reservation_instead_of_diverting(self):
        reserved = {}
        mine = op("x", "one")
        run(FakeFS(), ops(mine), reserved=reserved, reserve=True)
        again = op("x", "one")  # the deep copy the append path applies
        run(FakeFS(), ops(again), reserved=reserved)
        self.assertEqual(again.uris, [f"{BASE}/x.md"])
        taken = op("x", "one")
        with self.assertRaises(mg.GuardError):
            run(
                FakeFS({f"{BASE}/x.md": stored("other")}), ops(taken), reserved=reserved
            )

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
        fs = FakeFS({f"{BASE}/x.md": "?"})
        asyncio.run(mg.guard_add_only(module, fs, Registry(), ops(o), None))
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
    def test_a_failing_guard_blocks_the_stock_write(self):
        # Codex P0: no unguarded fallback; the commit fails instead of overwriting.
        calls = []

        async def stock(
            self, operations, ctx, extract_context, isolation_handler, tags
        ):
            calls.append(operations)
            return "result"

        wrapped = mg.wrap_apply_operations(FAKE_MODULE, stock)
        fs = FakeFS(failing={f"{BASE}/x.md"})
        updater = types.SimpleNamespace(_registry=Registry(), _get_viking_fs=lambda: fs)
        with self.assertRaises(mg.GuardError):
            asyncio.run(wrapped(updater, ops(op("x", "new")), None))
        self.assertEqual(calls, [])
        self.assertTrue(wrapped._ov_memory_guard)

    def test_guarded_operations_reach_the_stock_body(self):
        seen = []

        async def stock(
            self, operations, ctx, extract_context, isolation_handler, tags
        ):
            seen.append([list(o.uris) for o in operations.upsert_operations])
            return "result"

        wrapped = mg.wrap_apply_operations(FAKE_MODULE, stock)
        fs = FakeFS({f"{BASE}/x.md": stored("other")})
        updater = types.SimpleNamespace(_registry=Registry(), _get_viking_fs=lambda: fs)
        out = asyncio.run(wrapped(updater, ops(op("x", "new")), None))
        self.assertEqual((out, seen), ("result", [[[f"{BASE}/x_2.md"]]]))

    def test_submit_guards_the_whole_request_before_the_split(self):
        # Codex P1: a link between a diverted event and an upsert entity is sent to the
        # merge request; it must already carry the new URI when the split happens.
        target, entity = f"{BASE}/x.md", "viking://user/u/memories/entities/e.md"
        link = types.SimpleNamespace(from_uri=entity, to_uri=target)
        entity_op = op("e", "entity", memory_type="entities")
        entity_op.uris = [entity]
        request = types.SimpleNamespace(
            operations=ops(op("x", "new"), entity_op, links=[link]), ctx=object()
        )
        fs = FakeFS({target: stored("other")})
        module = types.SimpleNamespace(
            get_viking_fs=lambda: fs,
            create_default_registry=Registry,
            attach_source_to_request_operations=lambda req: None,
        )
        seen = []

        async def stock_submit(self, req):
            seen.append(link.to_uri)
            return "ok"

        updater = types.SimpleNamespace(registry=None)
        wrapped = mg.wrap_submit(module, stock_submit, memory_module=FAKE_MODULE)
        out = asyncio.run(wrapped(updater, request))
        self.assertEqual((out, seen), ("ok", [f"{BASE}/x_2.md"]))


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

    def test_streaming_updater_gets_the_request_level_guard(self):
        self.assertEqual(
            sitecustomize._entries(
                sitecustomize.TARGETS[
                    "openviking.session.memory.streaming_memory_updater"
                ]
            ),
            [("ov_memory_guard_patch", "apply_streaming")],
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

    def test_user_text_after_a_pasted_search_result_is_kept(self):
        # Codex P1: a correction typed after the paste is the user's own words.
        text = (
            "- [memory 68%] viking://user/noot-pilot/peers/p/memories/events/x.md\n"
            "    # Summary\n    The M2M app key is active.\n\n"
            "Correction: I revoked that key today."
        )
        self.assertEqual(
            mg.strip_recall_text(text),
            f"{mg.PASTE_PLACEHOLDER}\n\nCorrection: I revoked that key today.",
        )

    def test_every_paragraph_typed_after_the_paste_is_kept(self):
        # Codex P1 (round 2): two correction paragraphs, not just the last one.
        text = (
            "- [memory 68%] viking://user/noot-pilot/peers/p/memories/events/x.md\n"
            "    # Summary\n    The M2M app key is active.\n\n"
            "Correction: I revoked that key today.\n\n"
            "Also the dev tenant was renamed."
        )
        got = mg.strip_recall_text(text)
        self.assertTrue(got.startswith(mg.PASTE_PLACEHOLDER), got)
        self.assertIn("Correction: I revoked that key today.", got)
        self.assertIn("Also the dev tenant was renamed.", got)
        self.assertNotIn("M2M app key is active", got)

    def test_later_transcript_events_are_kept(self):
        text = (
            "❯ what did we look at?\n"
            "⏺ plugin:openviking-memory:openviking - read (MCP)(uris: [...])\n"
            "# Summary\nRecalled body.\n"
            "# 2026-09-24 (Thursday) ChatLog:\n**p**: old turn\n"
            "⏺ Bash(ls)\n  a.txt\n"
            "❯ now do the new thing"
        )
        got = mg.strip_recall_text(text)
        self.assertNotIn("Recalled body", got)
        self.assertNotIn("old turn", got)
        self.assertIn("⏺ Bash(ls)", got)
        self.assertTrue(got.endswith("❯ now do the new thing"), got)

    def test_recall_shaped_final_paragraph_stays_cut(self):
        text = "# 2026-09-22 (Tuesday) ChatLog:\n**p**: x\n\n**assistant**: y"
        self.assertEqual(mg.strip_recall_text(text), mg.PASTE_PLACEHOLDER)

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

    def test_event_fields_read_back_through_the_real_memory_file_utils(self):
        uri = f"{BASE}/kinde_login_attempts.md"
        mf = installed.MemoryFile.from_parsed(
            uri=uri,
            parsed={
                "memory_type": "events",
                "summary": "Attempted login.",
                "event_name": "kinde_login_attempts",
                "ranges": "0-3",
                "content": "# Summary\nAttempted login.",
            },
        )
        content = installed.MemoryFileUtils.write(mf)
        existing = asyncio.run(
            mg._existing(installed, FakeFS({uri: content}), uri, None)
        )
        same = {"summary": "Attempted login.", "event_name": "kinde_login_attempts"}
        self.assertTrue(mg._same_event(existing, {**same, "ranges": "0-3"}))
        self.assertFalse(mg._same_event(existing, {**same, "ranges": "10-14"}))

    def test_request_level_guard_applied_on_import(self):
        from openviking.session.memory import streaming_memory_updater as s

        submit = s.StreamingMemoryUpdater.submit
        self.assertTrue(getattr(submit, "_ov_memory_guard", False))
        self.assertEqual(mg._source_hash(submit.__wrapped__), mg.EXPECTED_SUBMIT_SHA256)

    def test_not_found_is_recognised_for_the_real_error(self):
        from openviking.storage.viking_fs import NotFoundError

        self.assertTrue(mg._is_not_found(NotFoundError("viking://x", "file")))
        self.assertFalse(mg._is_not_found(TimeoutError("slow")))

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
