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
import json
import os
import sys
import types
import unittest
from unittest import mock

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
        reserved, a, b = {}, object(), object()
        first, second = op("x", "one"), op("x", "two")
        run(FakeFS(), ops(first), reserved=reserved, reserve=True, owner=a)
        run(FakeFS(), ops(second), reserved=reserved, reserve=True, owner=b)
        self.assertEqual(
            (first.uris, second.uris), ([f"{BASE}/x.md"], [f"{BASE}/x_2.md"])
        )
        self.assertEqual(set(reserved), {f"{BASE}/x.md", f"{BASE}/x_2.md"})

    def test_the_apply_pass_verifies_its_own_reservation_instead_of_diverting(self):
        reserved, mine_owner = {}, object()
        mine = op("x", "one")
        run(FakeFS(), ops(mine), reserved=reserved, reserve=True, owner=mine_owner)
        again = op("x", "one")  # the deep copy the append path applies
        run(FakeFS(), ops(again), reserved=reserved, owner=mine_owner)
        self.assertEqual(again.uris, [f"{BASE}/x.md"])
        taken = op("x", "one")
        with self.assertRaises(mg.GuardError):
            run(
                FakeFS({f"{BASE}/x.md": stored("other")}),
                ops(taken),
                reserved=reserved,
                owner=mine_owner,
            )

    def _race(self, owner_writes: bool):
        """Submit A reserves x.md; an identical replay B waits on it; A then finishes."""

        async def scenario():
            reserved, fs = {}, FakeFS()
            a, b = object(), object()
            mine = op("x", "same")
            _, _, taken = await mg.guard_add_only(
                FAKE_MODULE,
                fs,
                Registry(),
                ops(mine),
                None,
                reserved=reserved,
                reserve=True,
                owner=a,
            )
            replay = ops(op("x", "same"))

            async def run_b():
                async def guard():
                    return await mg.guard_add_only(
                        FAKE_MODULE,
                        fs,
                        Registry(),
                        replay,
                        None,
                        reserved=reserved,
                        reserve=True,
                        owner=b,
                    )

                for _ in range(mg.MAX_WAITS):
                    try:
                        return await guard()
                    except mg._PendingDuplicate as pending:
                        await pending.reservation.done.wait()
                raise AssertionError("never settled")

            task = asyncio.ensure_future(run_b())
            await asyncio.sleep(0)
            self.assertFalse(task.done(), "B must wait while A is in flight")
            if owner_writes:
                fs.files[f"{BASE}/x.md"] = (
                    "summary: same\nevent_name: x\nranges: 0-3\n---\n"
                )
            mg.release_reservations(taken, reserved)
            await task
            return replay, reserved

        return asyncio.run(scenario())

    def test_a_replay_waits_for_the_owner_and_drops_only_after_it_stored(self):
        # Codex P1 (round 3): a pending reservation is not a stored write.
        replay, _ = self._race(owner_writes=True)
        self.assertEqual(replay.upsert_operations, [])

    def test_a_replay_takes_the_slot_when_the_owner_failed(self):
        replay, reserved = self._race(owner_writes=False)
        self.assertEqual([o.uris for o in replay.upsert_operations], [[f"{BASE}/x.md"]])
        self.assertIn(f"{BASE}/x.md", reserved)

    def test_release_is_synchronous_and_wakes_waiters(self):
        # Codex P2 (round 3): cleanup has no await, so cancellation cannot interrupt it.
        self.assertFalse(asyncio.iscoroutinefunction(mg.release_reservations))

        async def scenario():
            reservation = mg.Reservation({"k": "v"}, object(), asyncio.Event())
            mg._RESERVED["viking://t/x.md"] = reservation
            mg.release_reservations([("viking://t/x.md", reservation)])
            return reservation.done.is_set(), "viking://t/x.md" in mg._RESERVED

        self.assertEqual(asyncio.run(scenario()), (True, False))

    def test_a_cancelled_submit_releases_its_reservations(self):
        async def scenario():
            fs = FakeFS()
            module = types.SimpleNamespace(
                get_viking_fs=lambda: fs,
                create_default_registry=Registry,
                attach_source_to_request_operations=lambda req: None,
            )

            async def stock_submit(self, req):
                await asyncio.sleep(3600)

            wrapped = mg.wrap_submit(module, stock_submit, memory_module=FAKE_MODULE)
            request = types.SimpleNamespace(
                operations=ops(op("x", "new")), ctx=object()
            )
            task = asyncio.ensure_future(
                wrapped(types.SimpleNamespace(registry=None), request)
            )
            await asyncio.sleep(0.01)
            held = f"{BASE}/x.md" in mg._RESERVED
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return held, f"{BASE}/x.md" in mg._RESERVED

        self.assertEqual(asyncio.run(scenario()), (True, False))

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


@dataclasses.dataclass
class ShadowMessage(FakeMessage):
    turn_id: object = None
    message_kind: object = None
    created_at: object = None


@dataclasses.dataclass
class ShadowToolPart:
    tool_name: str
    tool_input: dict = dataclasses.field(default_factory=dict)
    tool_status: str = "success"


def umsg(mid, text_="hi", turn_id=None, message_kind=None, created_at=None):
    return ShadowMessage(
        mid, "user", [FakeText(text_)], turn_id, message_kind, created_at
    )


def amsg(mid, parts, turn_id=None, created_at=None):
    return ShadowMessage(mid, "assistant", list(parts), turn_id, None, created_at)


def tpart(name, tool_input=None, status="success"):
    return ShadowToolPart(name, tool_input or {}, status)


class ClassifyToolTests(unittest.TestCase):
    def test_ov_memory_read_tools(self):
        for verb in ("read", "search", "find", "list", "tree", "grep", "glob"):
            name = f"mcp__plugin_openviking-memory_openviking__{verb}"
            self.assertEqual(mg.classify_tool(tpart(name)), "ov_read", name)

    def test_ov_memory_write_tools_are_mutating(self):
        for verb in ("write", "edit", "remember", "forget", "add_resource"):
            name = f"mcp__plugin_openviking-memory_openviking__{verb}"
            self.assertEqual(mg.classify_tool(tpart(name)), "mutating", name)

    def test_ov_read_over_resources_only_is_read_only(self):
        part = tpart(
            "mcp__plugin_openviking-memory_openviking__read",
            {"uris": ["viking://resources/compendium/tasks/a.md"]},
        )
        self.assertEqual(mg.classify_tool(part), "read_only")

    def test_ov_read_with_a_mixed_target_list_is_still_a_memory_read(self):
        part = tpart(
            "mcp__plugin_openviking-memory_openviking__read",
            {
                "uris": [
                    "viking://resources/x.md",
                    "viking://user/u/memories/events/2026/09/24/x.md",
                ]
            },
        )
        self.assertEqual(mg.classify_tool(part), "ov_read")

    def test_ov_read_with_no_explicit_target_counts_as_a_memory_read(self):
        part = tpart(
            "mcp__plugin_openviking-memory_openviking__search", {"query": "kinde"}
        )
        self.assertEqual(mg.classify_tool(part), "ov_read")

    def test_read_only_tools(self):
        for name in ("ToolSearch", "Read", "Glob", "Grep", "LS"):
            self.assertEqual(mg.classify_tool(tpart(name)), "read_only", name)

    def test_context_mode_search_and_index_are_read_only(self):
        for name in (
            "mcp__plugin_context-mode_context-mode__ctx_search",
            "mcp__plugin_context-mode_context-mode__ctx_index",
        ):
            self.assertEqual(mg.classify_tool(tpart(name)), "read_only", name)

    def test_context_mode_execution_tools_are_unknown(self):
        # ctx_execute*/ctx_batch_execute can run anything, same as Bash.
        for name in (
            "mcp__plugin_context-mode_context-mode__ctx_execute",
            "mcp__plugin_context-mode_context-mode__ctx_execute_file",
            "mcp__plugin_context-mode_context-mode__ctx_batch_execute",
        ):
            self.assertEqual(mg.classify_tool(tpart(name)), "unknown", name)

    def test_mutating_tools(self):
        for name in ("Edit", "Write", "NotebookEdit", "MultiEdit", "Artifact"):
            self.assertEqual(mg.classify_tool(tpart(name)), "mutating", name)

    def test_an_mcp_tool_named_by_its_verb_is_mutating(self):
        for name in (
            "mcp__plugin_slack_slack__send_message",
            "mcp__plugin_jira_jira__create_issue",
            "mcp__plugin_x_x__publish_post",
        ):
            self.assertEqual(mg.classify_tool(tpart(name)), "mutating", name)

    def test_bash_and_unlisted_tools_are_unknown(self):
        for name in ("Bash", "SomeFutureTool"):
            self.assertEqual(mg.classify_tool(tpart(name)), "unknown", name)

    def test_malformed_tool_input_does_not_raise(self):
        for bad_input in (None, "not a dict", 5, ["a", "list"]):
            part = tpart("mcp__plugin_openviking-memory_openviking__read", bad_input)
            self.assertEqual(mg.classify_tool(part), "ov_read")


class SegmentTurnsTests(unittest.TestCase):
    def test_empty_message_list(self):
        self.assertEqual(mg.segment_turns([]), ([], "text"))

    def test_text_boundaries_without_turn_id(self):
        msgs = [umsg("u0"), amsg("a1", [FakeText("hello")]), umsg("u2", "next")]
        ranges, mode = mg.segment_turns(msgs)
        self.assertEqual(mode, "text")
        self.assertEqual(ranges, [range(2), range(2, 3)])

    def test_tool_only_user_transport_message_does_not_split_a_turn(self):
        msgs = [
            umsg("u0"),
            amsg("a1", [tpart("Read")]),
            ShadowMessage("u2", "user", [tpart("Bash")]),  # tool-only, no text part
            amsg("a3", [FakeText("done")]),
        ]
        ranges, mode = mg.segment_turns(msgs)
        self.assertEqual(ranges, [range(4)])
        self.assertEqual(mode, "text")

    def test_checkpoint_text_does_not_start_a_turn(self):
        msgs = [
            umsg("u0"),
            amsg("a1", [FakeText("hello")]),
            umsg("u2", "compacted", message_kind="checkpoint"),
            amsg("a3", [FakeText("ok")]),
        ]
        ranges, _ = mg.segment_turns(msgs)
        self.assertEqual(ranges, [range(4)])

    def test_turn_id_used_when_every_message_carries_one(self):
        msgs = [
            umsg("u0", turn_id="t1"),
            amsg("a1", [FakeText("hello")], turn_id="t1"),
            umsg("u2", "next", turn_id="t2"),
        ]
        ranges, mode = mg.segment_turns(msgs)
        self.assertEqual(mode, "turn_id")
        self.assertEqual(ranges, [range(2), range(2, 3)])

    def test_a_partial_turn_id_falls_back_to_text_boundaries_as_mixed(self):
        msgs = [
            umsg("u0", turn_id="t1"),
            amsg("a1", [FakeText("hello")]),  # no turn_id
            umsg("u2", "next", turn_id="t2"),
        ]
        ranges, mode = mg.segment_turns(msgs)
        self.assertEqual(mode, "mixed")
        self.assertEqual(ranges, [range(2), range(2, 3)])

    def test_a_turn_starting_mid_list_has_no_leading_user_text(self):
        msgs = [
            amsg("a0", [FakeText("orphaned reply")]),
            umsg("u1"),
            amsg("a2", [FakeText("ok")]),
        ]
        ranges, _ = mg.segment_turns(msgs)
        self.assertEqual(ranges, [range(1), range(1, 3)])
        self.assertFalse(mg._is_turn_boundary(msgs[ranges[0].start]))
        self.assertTrue(mg._is_turn_boundary(msgs[ranges[1].start]))


class RecallShadowTests(unittest.TestCase):
    def _turn(self, *assistant_parts):
        return [umsg("u0", "what did we look at?"), amsg("a1", assistant_parts)]

    def test_ov_read_then_an_answer_is_strip(self):
        msgs = self._turn(
            tpart("mcp__plugin_openviking-memory_openviking__read"),
            FakeText("Here it is."),
        )
        recs = mg.shadow_classify(msgs)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["verdict"], "strip")
        self.assertEqual(
            recs[0]["ov_tools"], ["mcp__plugin_openviking-memory_openviking__read"]
        )

    def test_read_search_bash_answer_is_keep_ambiguous(self):
        # The a71c8501 shape (BUG-1180): Bash is unknown, not neutral-read-only.
        msgs = self._turn(
            tpart("mcp__plugin_openviking-memory_openviking__read"),
            tpart("mcp__plugin_openviking-memory_openviking__search"),
            tpart("Bash", {"command": "rg kinde"}),
            FakeText("Here it is."),
        )
        recs = mg.shadow_classify(msgs)
        self.assertEqual(recs[0]["verdict"], "keep-ambiguous")
        self.assertEqual(recs[0]["bash_first_tokens"], ["rg"])
        self.assertEqual(recs[0]["other_tools"], ["unknown"])

    def test_read_then_edit_is_keep_mixed(self):
        msgs = self._turn(
            tpart("mcp__plugin_openviking-memory_openviking__read"),
            tpart("Edit"),
            FakeText("done"),
        )
        self.assertEqual(mg.shadow_classify(msgs)[0]["verdict"], "keep-mixed")

    def test_read_then_an_ov_write_is_keep_mixed(self):
        msgs = self._turn(
            tpart("mcp__plugin_openviking-memory_openviking__read"),
            tpart("mcp__plugin_openviking-memory_openviking__write"),
        )
        self.assertEqual(mg.shadow_classify(msgs)[0]["verdict"], "keep-mixed")

    def test_read_then_artifact_is_keep_mixed(self):
        msgs = self._turn(
            tpart("mcp__plugin_openviking-memory_openviking__read"), tpart("Artifact")
        )
        self.assertEqual(mg.shadow_classify(msgs)[0]["verdict"], "keep-mixed")

    def test_read_with_tool_status_error_is_keep_ambiguous(self):
        msgs = self._turn(
            tpart("mcp__plugin_openviking-memory_openviking__read", status="error"),
            FakeText("sorry, that failed"),
        )
        recs = mg.shadow_classify(msgs)
        self.assertEqual(recs[0]["verdict"], "keep-ambiguous")
        self.assertTrue(recs[0]["errored"])

    def test_resources_only_reads_produce_no_record(self):
        msgs = self._turn(
            tpart(
                "mcp__plugin_openviking-memory_openviking__read",
                {"uris": ["viking://resources/compendium/tasks/a.md"]},
            ),
            FakeText("Here it is."),
        )
        self.assertEqual(mg.shadow_classify(msgs), [])

    def test_a_turn_with_no_ov_read_produces_no_record(self):
        msgs = self._turn(tpart("Bash", {"command": "ls"}), FakeText("done"))
        self.assertEqual(mg.shadow_classify(msgs), [])

    def test_text_before_and_after_the_last_ov_read_are_split(self):
        msgs = self._turn(
            FakeText("thinking"),
            tpart("mcp__plugin_openviking-memory_openviking__read"),
            FakeText("answer"),
        )
        rec = mg.shadow_classify(msgs)[0]
        self.assertEqual(rec["assistant_chars_before"], len("thinking"))
        self.assertEqual(rec["assistant_chars_after"], len("answer"))
        self.assertEqual(rec["assistant_text_hashes_before"], [mg._hash12("thinking")])
        self.assertEqual(rec["assistant_text_hashes_after"], [mg._hash12("answer")])

    def test_only_text_after_the_last_of_two_ov_reads_counts_as_after(self):
        msgs = self._turn(
            tpart("mcp__plugin_openviking-memory_openviking__read"),
            FakeText("middle"),
            tpart("mcp__plugin_openviking-memory_openviking__search"),
            FakeText("final"),
        )
        rec = mg.shadow_classify(msgs)[0]
        self.assertEqual(rec["assistant_chars_before"], len("middle"))
        self.assertEqual(rec["assistant_chars_after"], len("final"))

    def test_record_never_carries_message_text(self):
        msgs = self._turn(
            tpart("mcp__plugin_openviking-memory_openviking__read"),
            FakeText("a very identifiable secret sentence"),
        )
        rec = mg.shadow_classify(msgs)[0]
        blob = json.dumps(rec)
        self.assertNotIn("secret", blob)
        self.assertNotIn("identifiable", blob)

    def test_record_carries_message_ids_and_partial_flag(self):
        msgs = self._turn(tpart("mcp__plugin_openviking-memory_openviking__read"))
        rec = mg.shadow_classify(msgs)[0]
        self.assertEqual(
            (rec["first_message_id"], rec["last_message_id"]), ("u0", "a1")
        )
        self.assertFalse(rec["partial"])
        self.assertEqual(rec["message_count"], 2)

    def test_a_partial_turn_is_flagged(self):
        msgs = [amsg("a0", [tpart("mcp__plugin_openviking-memory_openviking__read")])]
        rec = mg.shadow_classify(msgs)[0]
        self.assertTrue(rec["partial"])

    def test_created_at_min_and_max_span_the_turn(self):
        msgs = [
            umsg(
                "u0",
                "what did we look at?",
                created_at="2026-09-24T20:39:14+00:00",
            ),
            amsg(
                "a1",
                [
                    tpart("mcp__plugin_openviking-memory_openviking__read"),
                    FakeText("here"),
                ],
                created_at="2026-09-24T20:39:20+00:00",
            ),
        ]
        rec = mg.shadow_classify(msgs)[0]
        self.assertEqual(rec["created_at_min"], "2026-09-24T20:39:14+00:00")
        self.assertEqual(rec["created_at_max"], "2026-09-24T20:39:20+00:00")

    def test_created_at_is_none_when_no_message_carries_one(self):
        msgs = self._turn(tpart("mcp__plugin_openviking-memory_openviking__read"))
        rec = mg.shadow_classify(msgs)[0]
        self.assertIsNone(rec["created_at_min"])
        self.assertIsNone(rec["created_at_max"])

    def test_shadow_classify_never_mutates_messages(self):
        msgs = self._turn(
            tpart("mcp__plugin_openviking-memory_openviking__read"),
            FakeText("Here it is."),
        )
        before = [dataclasses.replace(m) for m in msgs]
        mg.shadow_classify(msgs)
        self.assertEqual(msgs, before)


class ShadowWrapperTests(unittest.TestCase):
    def _module(self):
        return types.SimpleNamespace(TextPart=FakeText)

    def test_invariance_message_list_identical_shadow_on_and_off(self):
        seen = []

        def orig(self, messages, chunk_meta, *, split_long_text_messages=True):
            seen.append(messages)

        wrapped = mg.wrap_extract_init(self._module(), orig)
        msgs = [
            umsg("u0", PASTE),
            amsg(
                "a1",
                [
                    tpart("mcp__plugin_openviking-memory_openviking__read"),
                    FakeText("hi"),
                ],
            ),
        ]
        os.environ.pop("OV_RECALL_SHADOW", None)
        wrapped(object(), list(msgs))
        os.environ["OV_RECALL_SHADOW"] = "0"
        try:
            wrapped(object(), list(msgs))
        finally:
            os.environ.pop("OV_RECALL_SHADOW", None)
        self.assertEqual(seen[0], seen[1])
        # the untouched assistant message is the same object both times
        self.assertIs(seen[0][1], msgs[1])
        self.assertIs(seen[1][1], msgs[1])

    def test_kill_switch_suppresses_shadow_logging(self):
        def orig(self, messages, chunk_meta, *, split_long_text_messages=True):
            return None

        wrapped = mg.wrap_extract_init(self._module(), orig)
        msgs = [
            umsg("u0"),
            amsg(
                "a1",
                [
                    tpart("mcp__plugin_openviking-memory_openviking__read"),
                    FakeText("ans"),
                ],
            ),
        ]
        os.environ["OV_RECALL_SHADOW"] = "0"
        try:
            with mock.patch.object(mg.logger, "warning") as warn:
                wrapped(object(), msgs)
        finally:
            os.environ.pop("OV_RECALL_SHADOW", None)
        shadow_calls = [
            c
            for c in warn.call_args_list
            if c.args and str(c.args[0]).startswith("ov-recall-shadow")
        ]
        self.assertEqual(shadow_calls, [])

    def test_prechunked_skips_classification_and_logs_once(self):
        called = []

        def orig(self, messages, chunk_meta, *, split_long_text_messages=True):
            called.append((messages, chunk_meta))

        wrapped = mg.wrap_extract_init(self._module(), orig)
        msgs = [umsg("u0")]
        chunk_meta = object()
        with (
            mock.patch.object(mg, "shadow_classify") as classify,
            mock.patch.object(mg.logger, "warning") as warn,
        ):
            wrapped(object(), msgs, chunk_meta)
        classify.assert_not_called()
        expected = (
            "ov-recall-shadow %s",
            json.dumps({"skipped": "prechunked"}, sort_keys=True),
        )
        skip_calls = [c for c in warn.call_args_list if c.args == expected]
        self.assertEqual(len(skip_calls), 1)
        self.assertEqual(called, [(msgs, chunk_meta)])

    def test_classifier_failure_leaves_the_stock_call_unchanged(self):
        seen = []

        def orig(self, messages, chunk_meta, *, split_long_text_messages=True):
            seen.append(messages)

        wrapped = mg.wrap_extract_init(self._module(), orig)
        msgs = [umsg("u0"), amsg("a1", [FakeText("ans")])]
        with mock.patch.object(mg, "shadow_classify", side_effect=RuntimeError("boom")):
            wrapped(object(), msgs)  # must not raise
        self.assertEqual(seen, [msgs])


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

    def test_shadow_classifier_does_not_change_split_messages(self):
        # IMPR-1188: a long assistant text that stock `_build_extraction_messages`
        # divides must come out identically whether the shadow classifier ran or not.
        from openviking.message import Message, ToolPart

        long_text = "answer " * 4000

        def build_messages():
            return [
                Message(
                    id="u0", role="user", parts=[installed.TextPart("what did we do?")]
                ),
                Message(
                    id="a1",
                    role="assistant",
                    parts=[
                        ToolPart(
                            tool_name="mcp__plugin_openviking-memory_openviking__read",
                            tool_input={},
                            tool_status="success",
                        ),
                        installed.TextPart(long_text),
                    ],
                ),
            ]

        def shape(ctx):
            return [
                (m.id, [getattr(p, "text", None) for p in m.parts])
                for m in ctx.messages
            ]

        os.environ["OV_RECALL_SHADOW"] = "0"
        try:
            ctx_off = installed.ExtractContext(build_messages())
        finally:
            os.environ.pop("OV_RECALL_SHADOW", None)
        ctx_on = installed.ExtractContext(build_messages())
        self.assertEqual(shape(ctx_off), shape(ctx_on))


if __name__ == "__main__":
    unittest.main()
