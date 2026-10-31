"""Tests for ov_memory_guard_patch.

    python3 test_ov_memory_guard_patch.py

The guard's semantics run on fakes anywhere. The ``Installed`` cases need openviking
v0.5.1.1 (the image, with the repo root mounted so the GIT_* scrub header finds
_scripts/lib: PYTHONPATH=<this dir> /app/.venv/bin/python <this dir>/…, or the
``openviking`` uv tool through viking/scripts/run-nav-patch-tests.py) and are skipped
only when no openviking package is found: they check the version/source-hash guard against the real ``MemoryUpdater``
and read a summary back through the real ``MemoryFileUtils``.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "_scripts" / "lib"))
from git_env import scrub_environ  # noqa: E402

scrub_environ()

import asyncio
import contextlib
import dataclasses
import importlib.util
import io
import json
import os
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


MODES = {
    "events": "add_only",
    "trajectories": "add_only",
    "entities": "upsert",
    "profile": "upsert",
    "soul": "upsert",
}


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
                get_default_registry=Registry,
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
            get_default_registry=Registry,
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

    def test_submit_reads_add_only_from_the_registry_the_stock_split_uses(self):
        # v0.5.1.1 splits on request.memory_registry first, then the updater's registry
        # (streaming_memory_updater.py L326); the guard must decide add_only the same way.
        class AllUpsert:
            def get(self, memory_type):
                return types.SimpleNamespace(operation_mode="upsert")

        target = f"{BASE}/x.md"
        fs = FakeFS({target: stored("other")})
        module = types.SimpleNamespace(
            get_viking_fs=lambda: fs,
            get_default_registry=AllUpsert,
            attach_source_to_request_operations=lambda req: None,
        )

        async def stock_submit(self, req):
            return [list(o.uris) for o in req.operations.upsert_operations]

        wrapped = mg.wrap_submit(module, stock_submit, memory_module=FAKE_MODULE)
        cases = [
            # (request registry, updater registry, expected target)
            (Registry(), AllUpsert(), f"{BASE}/x_2.md"),
            (AllUpsert(), Registry(), target),
            (None, Registry(), f"{BASE}/x_2.md"),
            (None, None, target),
        ]
        for request_registry, updater_registry, expected in cases:
            with self.subTest(request=request_registry, updater=updater_registry):
                request = types.SimpleNamespace(
                    operations=ops(op("x", "new")),
                    ctx=object(),
                    memory_registry=request_registry,
                )
                updater = types.SimpleNamespace(registry=updater_registry)
                self.assertEqual(asyncio.run(wrapped(updater, request)), [[expected]])


USER_PROFILE = "viking://user/u/memories/profile.md"
PEER_PROFILE = "viking://user/u/peers/p/memories/profile.md"
ENTITY = "viking://user/u/memories/entities/e.md"


def deleted(uri, memory_type=None, content="body"):
    return types.SimpleNamespace(
        uri=uri, memory_type=memory_type, extra_fields={}, content=content
    )


def singleton_records(run):
    """``(records, result)``: the ``ov-singleton-guard`` records ``run()`` wrote."""
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        result = run()
    records = [
        json.loads(line[len(mg.SINGLETON_PREFIX) :])
        for line in buf.getvalue().splitlines()
        if line.startswith(mg.SINGLETON_PREFIX)
    ]
    return records, result


def fake_materialize(cls, operations):
    """Stock ``_materialize_uri_migrations`` in miniature: queue each migration source."""
    for o in operations.upsert_operations:
        old = o.old_memory_file_content
        if old and old.uri and o.uris and o.uris[0] != old.uri:
            if all(f.uri != old.uri for f in operations.delete_file_contents):
                operations.delete_file_contents.append(old)
            operations.delete_replacements[old.uri] = o.uris[0]
    return []


class SingletonGuardTests(unittest.TestCase):
    """BUG-1276: an extraction never deletes or migrates away a profile/identity/soul."""

    def test_singleton_deletes_are_dropped_and_others_kept(self):
        operations = ops(replacements={PEER_PROFILE: USER_PROFILE, ENTITY: "x"})
        operations.delete_file_contents = [
            deleted(PEER_PROFILE, "profile", "# pop"),
            deleted(USER_PROFILE, None),  # matched by URI when the type is missing
            deleted("viking://user/u/peers/p/memories/soul.md", "soul"),
            deleted(ENTITY, "entities"),
        ]
        records, dropped = singleton_records(lambda: mg.guard_singletons(operations))
        self.assertEqual(
            dropped,
            [PEER_PROFILE, USER_PROFILE, "viking://user/u/peers/p/memories/soul.md"],
        )
        self.assertEqual([f.uri for f in operations.delete_file_contents], [ENTITY])
        # Replacements stay, so stock still moves links to the replacement (R2 P1).
        self.assertEqual(
            operations.delete_replacements, {PEER_PROFILE: USER_PROFILE, ENTITY: "x"}
        )
        self.assertEqual(
            records[0],
            {
                "action": "delete_dropped",
                "memory_type": "profile",
                "uri": PEER_PROFILE,
                "replacement": USER_PROFILE,
                "chars": 5,
            },
        )
        self.assertEqual(len(records), 3)

    def test_a_singleton_migration_writes_its_target_and_keeps_its_source(self):
        # enforce_merge_group_peer_id can retarget a user-level profile edit at the
        # peer scope; stock code would write the peer file and delete the user one.
        # Nothing is retargeted, so an in-place op on the target and every reference to
        # it keep their URIs (Codex review of homelab#285, R1's P1s), and the source's
        # replacement stays for stock link migration (R2's P1).
        moved = op("profile", "s", memory_type="profile", old=deleted(USER_PROFILE))
        moved.uris = [PEER_PROFILE]
        in_place = op("profile", "t", memory_type="profile", old=deleted(PEER_PROFILE))
        in_place.uris = [PEER_PROFILE]
        entity = op("e", "s", memory_type="entities", old=deleted(ENTITY))
        entity.uris = ["viking://user/u/peers/p/memories/entities/e.md"]
        link = types.SimpleNamespace(from_uri=ENTITY, to_uri=PEER_PROFILE)
        operations = ops(
            moved, in_place, entity, links=[link], replacements={"x": PEER_PROFILE}
        )
        operations.delete_file_contents = []
        wrapped = mg.wrap_materialize_uri_migrations(fake_materialize)
        records, errors = singleton_records(
            lambda: wrapped.__get__(None, object)(operations)
        )
        self.assertEqual(errors, [])
        self.assertEqual(
            [f.uri for f in operations.delete_file_contents], [ENTITY]
        )  # the entity migration still deletes its source
        self.assertEqual(
            operations.delete_replacements,
            {
                "x": PEER_PROFILE,
                USER_PROFILE: PEER_PROFILE,
                ENTITY: "viking://user/u/peers/p/memories/entities/e.md",
            },
        )
        self.assertEqual((moved.uris, in_place.uris), ([PEER_PROFILE], [PEER_PROFILE]))
        self.assertEqual((link.from_uri, link.to_uri), (ENTITY, PEER_PROFILE))
        self.assertEqual(
            [(r["action"], r["uri"], r["replacement"]) for r in records],
            [("migration_source_kept", USER_PROFILE, PEER_PROFILE)],
        )

    def test_materialize_errors_pass_through_untouched(self):
        operations = ops()
        operations.delete_file_contents = [deleted(USER_PROFILE, "profile")]
        err = [(USER_PROFILE, ValueError("x"))]
        wrapped = mg.wrap_materialize_uri_migrations(lambda cls, o: err)
        self.assertIs(wrapped.__get__(None, object)(operations), err)
        self.assertEqual(len(operations.delete_file_contents), 1)

    def test_disabled_by_env(self):
        operations = ops()
        operations.delete_file_contents = [deleted(USER_PROFILE, "profile")]
        with mock.patch.dict(os.environ, {"OV_SINGLETON_GUARD": "0"}):
            self.assertEqual(mg.guard_singletons(operations), [])
            self.assertEqual(mg.keep_singleton_migration_sources(operations, set()), [])
        self.assertEqual(len(operations.delete_file_contents), 1)

    def test_apply_operations_drops_the_delete_for_submitted_requests_too(self):
        # A flush runs apply_operations with no submit owner, a direct call with one set;
        # a cross-session merge emits deletes after submit, so both paths are guarded.
        seen = []

        async def stock(
            self, operations, ctx, extract_context, isolation_handler, tags
        ):
            seen.append([f.uri for f in operations.delete_file_contents])
            return "result"

        wrapped = mg.wrap_apply_operations(FAKE_MODULE, stock)
        updater = types.SimpleNamespace(
            _registry=Registry(), _get_viking_fs=lambda: FakeFS()
        )
        for owner in (None, object()):
            with self.subTest(owner=owner):
                operations = ops()
                operations.delete_file_contents = [
                    deleted(USER_PROFILE, "profile"),
                    deleted(ENTITY, "entities"),
                ]
                token = mg._OWNER.set(owner)
                try:
                    _, out = singleton_records(
                        lambda: asyncio.run(wrapped(updater, operations, None))
                    )
                finally:
                    mg._OWNER.reset(token)
                self.assertEqual(out, "result")
                self.assertEqual(seen[-1], [ENTITY])


def noise_records(run):
    """``(records, result)``: the ``ov-event-noise`` records ``run()`` wrote to stderr."""
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        result = run()
    records = [
        json.loads(line[len(mg.NOISE_PREFIX) :])
        for line in buf.getvalue().splitlines()
        if line.startswith(mg.NOISE_PREFIX)
    ]
    return records, result


class LifecycleNoiseTests(unittest.TestCase):
    # Event names and summaries from the pop/workbook peers, 2026-09-22..10-05 (some trimmed).
    NOISE = (
        (
            "compendium_1009_merged",
            "Compendium#1009 was merged as 0c1245f05 on 2026-10-02.",
        ),
        (
            "dotfiles_pr_134_merged",
            "Merged dotfiles#134 (commit 2d7af86). The h7 harness now logs to 03-gates/h7-hooks.log.",
        ),
        (
            "homelab_pr_151_opened",
            "Opened PR #151 in the homelab repository for documentation updates.",
        ),
        ("impr_1154_pr_opened", "The PR for IMPR-1154 was opened against main."),
        (
            "merge_dotfiles_127",
            "dotfiles #127 was merged (511f2bd) and the worktree/branch were cleaned up.",
        ),
        (
            "dipdash_cleanup",
            "Cleaned up worktrees, local branches, and fast-forwarded main after merging dipdash#1195.",
        ),
        (
            "claims_contract_commits",
            "Committed three changes on top of 4daf7791: 35cafb2f, 44fb912f and 0c83ef9a.",
        ),
        # Codex review of homelab#183: creation, and a reason word inside a quoted title
        ("pr_183_created", "Created PR 183 for the lifecycle filter."),
        (
            "compendium_900_merged",
            "Merged compendium#900, titled 'drop duplicate index rows'.",
        ),
    )
    KEEP = (
        # lifecycle, but the summary says why
        (
            "compendium_760_closed",
            "Closed compendium#760 as superseded by compendium#763. Removed worktree and branches.",
        ),
        (
            "dotfiles_232_merged",
            "Dotfiles PR #232 was merged. Sessions now save to the 'noot' account instead of the pilot.",
        ),
        (
            "compendium_1028_merge",
            "Compendium#1028 was merged without the required CI checks because ARC runners were down.",
        ),
        # lifecycle words in the name or summary, but not a lifecycle event
        (
            "pr_quiz_counterargument_questions",
            "The user proposed adding counterargument questions to the PR quiz.",
        ),
        (
            "homelab_pr_158_cluster_verification",
            "The user verified that the cluster remains clean after the accidental deploy.",
        ),
        (
            "dotfiles_worktree_rule_tightened",
            "Agents must now create a worktree before any write.",
        ),
        # Shadow week, 2026-10-06: a content cleanup is not lifecycle (confirmed false drop)
        (
            "dotfiles_template_split_and_cleanup",
            (
                "The PR template was split into a shared base (PR_TEMPLATE.md) and a "
                "work-specific file (PR_TEMPLATE_WORK.md) to separate personal and work "
                "PR rules. Eleven cleanup items were also applied to both files."
            ),
        ),
        # Codex review of homelab#183: not git, and reasons worded as purpose or choice
        (
            "incident_closed",
            "Closed the production incident after verifying successful replay and restored data.",
        ),
        ("pr_183_closed", "Closed PR 183 to avoid exposing credentials in logs."),
        (
            "dipdash_1141_merged",
            "Merged dipdash#1141. Chose cherry-picks to minimize regression risk.",
        ),
        # apostrophes must not hide the reason as if it were quoted
        (
            "idea_143_pr_opened",
            "The IDEA-143 PR was opened but wasn't merged because it's a prose change.",
        ),
    )

    def setUp(self):
        env = mock.patch.dict(os.environ, {"OV_EVENT_NOISE_FILTER": "1"})
        env.start()
        self.addCleanup(env.stop)

    def test_lifecycle_without_reasoning_is_noise(self):
        for name, summary in self.NOISE:
            with self.subTest(name=name):
                self.assertTrue(mg.is_lifecycle_noise(name, summary))

    def test_reasoning_or_non_lifecycle_is_kept(self):
        for name, summary in self.KEEP:
            with self.subTest(name=name):
                self.assertFalse(mg.is_lifecycle_noise(name, summary))

    def test_drops_only_noisy_events(self):
        noisy = op("compendium_1009_merged", self.NOISE[0][1])
        kept = op("compendium_760_closed", self.KEEP[0][1])
        entity = op("dotfiles_127_merged", self.NOISE[4][1], memory_type="entities")
        batch = ops(noisy, kept, entity)
        dropped = mg.drop_lifecycle_noise(batch)
        self.assertEqual(dropped, [(noisy.uris[0], "compendium_1009_merged")])
        self.assertEqual(batch.upsert_operations, [kept, entity])

    def test_links_to_a_dropped_event_go_with_it(self):
        noisy = op("compendium_1009_merged", self.NOISE[0][1])
        entity = "viking://user/u/memories/entities/e.md"
        dangling = types.SimpleNamespace(from_uri=entity, to_uri=noisy.uris[0])
        other = types.SimpleNamespace(from_uri=entity, to_uri=f"{BASE}/y.md")
        batch = ops(noisy, links=[dangling, other])
        mg.drop_lifecycle_noise(batch)
        self.assertEqual(batch.resolved_links, [other])

    def test_a_delete_replacement_is_kept(self):
        noisy = op("compendium_1009_merged", self.NOISE[0][1])
        batch = ops(noisy, replacements={f"{BASE}/old.md": noisy.uris[0]})
        self.assertEqual(mg.drop_lifecycle_noise(batch), [])
        self.assertEqual(batch.upsert_operations, [noisy])

    def test_off_switch(self):
        batch = ops(op("compendium_1009_merged", self.NOISE[0][1]))
        with mock.patch.dict(os.environ, {"OV_EVENT_NOISE_FILTER": "0"}):
            self.assertEqual(mg.drop_lifecycle_noise(batch), [])
        self.assertEqual(len(batch.upsert_operations), 1)

    def test_shadow_logs_but_keeps_everything(self):
        noisy = op("compendium_1009_merged", self.NOISE[0][1])
        link = types.SimpleNamespace(from_uri="viking://e", to_uri=noisy.uris[0])
        batch = ops(noisy, links=[link])
        with mock.patch.dict(os.environ, {"OV_EVENT_NOISE_FILTER": "shadow"}):
            records, matched = noise_records(lambda: mg.drop_lifecycle_noise(batch))
        self.assertEqual(matched, [(noisy.uris[0], "compendium_1009_merged")])
        self.assertEqual(
            (batch.upsert_operations, batch.resolved_links), ([noisy], [link])
        )
        self.assertEqual(
            records,
            [
                {
                    "action": "would_drop",
                    "event_name": "compendium_1009_merged",
                    "uri": noisy.uris[0],
                    "summary": self.NOISE[0][1],
                }
            ],
        )

    def test_a_record_is_one_line_whatever_the_summary(self):
        # Canary 2026-10-05: through OpenViking's rich handler a logger record wrapped
        # at 80 columns into separate Loki entries. The record bypasses logging, and
        # json.dumps keeps quotes and newlines inside the one line.
        summary = 'Merged dotfiles#1 (commit abc1234)\n"quoted" ' + "x" * 400
        batch = ops(op("dotfiles_1_merged", summary))
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            mg.drop_lifecycle_noise(batch)
        lines = buf.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith(mg.NOISE_PREFIX + "{"))
        record = json.loads(lines[0][len(mg.NOISE_PREFIX) :])
        self.assertEqual(record["action"], "dropped")
        self.assertEqual(len(record["summary"]), mg.SUMMARY_LOG_CHARS)

    def test_both_wrappers_filter_before_the_stock_body(self):
        seen = []

        async def stock(
            self, operations, ctx, extract_context, isolation_handler, tags
        ):
            seen.append(
                [o.memory_fields["event_name"] for o in operations.upsert_operations]
            )

        async def stock_submit(self, req):
            seen.append(
                [
                    o.memory_fields["event_name"]
                    for o in req.operations.upsert_operations
                ]
            )

        def batch():
            return ops(op("compendium_1009_merged", self.NOISE[0][1]), op("x", "kept"))

        fs = FakeFS()
        updater = types.SimpleNamespace(_registry=Registry(), _get_viking_fs=lambda: fs)
        asyncio.run(
            mg.wrap_apply_operations(FAKE_MODULE, stock)(updater, batch(), None)
        )
        module = types.SimpleNamespace(
            get_viking_fs=lambda: fs,
            get_default_registry=Registry,
            attach_source_to_request_operations=lambda req: None,
        )
        request = types.SimpleNamespace(operations=batch(), ctx=object())
        wrapped = mg.wrap_submit(module, stock_submit, memory_module=FAKE_MODULE)
        asyncio.run(wrapped(types.SimpleNamespace(registry=None), request))
        self.assertEqual(seen, [["x"], ["x"]])

    def chained(self, request, mode):
        """Run ``request`` through the patched submit, whose stock body splits it the way
        v0.5.1.1 does — the append request carries no delete_replacements — and applies the
        append half through the patched apply_operations."""
        applied = []

        async def stock_apply(
            self, operations, ctx, extract_context, isolation_handler, tags
        ):
            applied.append(
                [o.memory_fields["event_name"] for o in operations.upsert_operations]
            )

        fs = FakeFS()
        updater = types.SimpleNamespace(_registry=Registry(), _get_viking_fs=lambda: fs)
        apply_ops = mg.wrap_apply_operations(FAKE_MODULE, stock_apply)

        async def stock_submit(self, req):
            append = ops(*req.operations.upsert_operations)  # no replacements, as stock
            await apply_ops(updater, append, req.ctx)

        module = types.SimpleNamespace(
            get_viking_fs=lambda: fs,
            get_default_registry=Registry,
            attach_source_to_request_operations=lambda req: None,
        )
        wrapped = mg.wrap_submit(module, stock_submit, memory_module=FAKE_MODULE)
        with mock.patch.dict(os.environ, {"OV_EVENT_NOISE_FILTER": mode}):
            asyncio.run(wrapped(types.SimpleNamespace(registry=None), request))
        return applied

    def test_a_replacement_survives_the_split(self):
        # Codex P1: the submit filter keeps a replacement; the append request reaching
        # apply_operations has lost delete_replacements and must not filter again.
        noisy = op("compendium_1009_merged", self.NOISE[0][1])
        request = types.SimpleNamespace(
            operations=ops(noisy, replacements={f"{BASE}/old.md": noisy.uris[0]}),
            ctx=object(),
        )
        self.assertEqual(self.chained(request, "1"), [["compendium_1009_merged"]])

    def test_a_submitted_shadow_match_is_logged_once(self):
        # Codex P3: one write, one "would_drop" record.
        request = types.SimpleNamespace(
            operations=ops(op("compendium_1009_merged", self.NOISE[0][1])),
            ctx=object(),
        )
        records, applied = noise_records(lambda: self.chained(request, "shadow"))
        self.assertEqual(applied, [["compendium_1009_merged"]])
        self.assertEqual([r["action"] for r in records], ["would_drop"])

    def test_drop_needs_an_explicit_1(self):
        # Codex P2: unset, a typo, or "false" must never start deleting.
        for value in (None, "shadwo", "false", "true", ""):
            with self.subTest(value=value):
                env = {} if value is None else {"OV_EVENT_NOISE_FILTER": value}
                with mock.patch.dict(os.environ, env, clear=False):
                    if value is None:
                        os.environ.pop("OV_EVENT_NOISE_FILTER", None)
                    self.assertEqual(mg._noise_filter_mode(), "shadow")
                    batch = ops(op("compendium_1009_merged", self.NOISE[0][1]))
                    mg.drop_lifecycle_noise(batch)
                    self.assertEqual(len(batch.upsert_operations), 1)


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
        # BUG-1196: the extract patch's write tracking wraps other methods than the
        # guard's ``submit``, so the order between them does not matter.
        self.assertEqual(
            sitecustomize._entries(
                sitecustomize.TARGETS[
                    "openviking.session.memory.streaming_memory_updater"
                ]
            ),
            [
                ("ov_memory_guard_patch", "apply_streaming"),
                ("ov_extract_patch", "apply_streaming"),
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
    tool_id: str = ""


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

    # C-2 (ultrareview; Codex 2026-09-27): the verb heuristic is for MCP names only.
    def test_todowrite_is_session_bookkeeping_not_work(self):
        self.assertEqual(mg.classify_tool(tpart("TodoWrite")), "read_only")

    def test_sendmessage_is_mutating_explicitly(self):
        self.assertEqual(mg.classify_tool(tpart("SendMessage")), "mutating")

    def test_a_non_mcp_tool_with_a_verb_in_its_name_is_unknown_not_mutating(self):
        for name in ("TaskUpdate", "TaskCreate", "CronDelete"):
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

    # C-4 (ultrareview; Codex 2026-09-27): in real archives an assistant tool part stays
    # "running"; the outcome is on the user-side result part with the same tool_id.
    def _ov_call(self, tool_id="toolu_1"):
        return ShadowToolPart(
            "mcp__plugin_openviking-memory_openviking__read", {}, "running", tool_id
        )

    def _result(self, status, tool_id="toolu_1"):
        return ShadowMessage(
            "r1",
            "user",
            [
                ShadowToolPart(
                    "mcp__plugin_openviking-memory_openviking__read",
                    {},
                    status,
                    tool_id,
                )
            ],
        )

    def test_a_failed_ov_read_result_is_errored_and_never_strip(self):
        msgs = [
            umsg("u0", "what did we decide?"),
            amsg("a1", [self._ov_call()]),
            self._result("error"),
            amsg("a2", [FakeText("I could not read it, but from memory...")]),
        ]
        rec = mg.shadow_classify(msgs)[0]
        self.assertTrue(rec["errored"])
        self.assertEqual(rec["verdict"], "keep-ambiguous")

    def test_a_completed_ov_read_result_allows_strip(self):
        msgs = [
            umsg("u0", "what did we decide?"),
            amsg("a1", [self._ov_call()]),
            self._result("completed"),
            amsg("a2", [FakeText("We chose cobalt.")]),
        ]
        rec = mg.shadow_classify(msgs)[0]
        self.assertFalse(rec["errored"])
        self.assertEqual(rec["unresolved_ov_reads"], 0)
        self.assertEqual(rec["verdict"], "strip")

    def test_an_ov_read_with_no_result_in_the_turn_is_unresolved_and_not_strip(self):
        msgs = [
            umsg("u0", "what did we decide?"),
            amsg("a1", [self._ov_call(), FakeText("We chose cobalt.")]),
        ]
        rec = mg.shadow_classify(msgs)[0]
        self.assertEqual(rec["unresolved_ov_reads"], 1)
        self.assertEqual(rec["verdict"], "keep-ambiguous")

    def test_record_carries_tool_names_so_classification_can_be_redone(self):
        msgs = self._turn(
            tpart("mcp__plugin_openviking-memory_openviking__read"),
            tpart("TodoWrite"),
            tpart("Bash", {"command": "ls"}),
            FakeText("done"),
        )
        rec = mg.shadow_classify(msgs)[0]
        self.assertEqual(rec["other_tool_names"], ["Bash", "TodoWrite"])
        self.assertEqual(rec["verdict"], "keep-ambiguous")  # Bash is unknown

    def test_todowrite_alone_beside_an_ov_read_stays_strip(self):
        msgs = self._turn(
            tpart("mcp__plugin_openviking-memory_openviking__read"),
            tpart("TodoWrite"),
            FakeText("Here it is."),
        )
        self.assertEqual(mg.shadow_classify(msgs)[0]["verdict"], "strip")

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

    def test_source_archive_uri_reaches_the_stock_init(self):
        # v0.5.1.1 added the keyword (memory_updater.py L221); dropping it would lose the
        # archive suffix that get_source_archive_suffix() puts in file names.
        seen = []

        def orig(
            self,
            messages,
            chunk_meta,
            *,
            split_long_text_messages=True,
            source_archive_uri="",
        ):
            seen.append((split_long_text_messages, source_archive_uri))

        wrapped = mg.wrap_extract_init(self._module(), orig)
        wrapped(object(), [umsg("u0")])
        wrapped(
            object(),
            [umsg("u0")],
            split_long_text_messages=False,
            source_archive_uri="viking://s/history/archive_005",
        )
        wrapped(object(), [umsg("u0")], {}, source_archive_uri="viking://s/a")
        self.assertEqual(
            seen,
            [
                (True, ""),
                (False, "viking://s/history/archive_005"),
                (True, "viking://s/a"),
            ],
        )

    def test_invariance_message_list_identical_shadow_on_and_off(self):
        seen = []

        def orig(
            self,
            messages,
            chunk_meta,
            *,
            split_long_text_messages=True,
            source_archive_uri="",
        ):
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
        def orig(
            self,
            messages,
            chunk_meta,
            *,
            split_long_text_messages=True,
            source_archive_uri="",
        ):
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

        def orig(
            self,
            messages,
            chunk_meta,
            *,
            split_long_text_messages=True,
            source_archive_uri="",
        ):
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

        def orig(
            self,
            messages,
            chunk_meta,
            *,
            split_long_text_messages=True,
            source_archive_uri="",
        ):
            seen.append(messages)

        wrapped = mg.wrap_extract_init(self._module(), orig)
        msgs = [umsg("u0"), amsg("a1", [FakeText("ans")])]
        with mock.patch.object(mg, "shadow_classify", side_effect=RuntimeError("boom")):
            wrapped(object(), msgs)  # must not raise
        self.assertEqual(seen, [msgs])


# --- Write-time checks and the prefetch (IMPR-1401, BUG-1283, BUG-1284) ------------------

ENVS = (
    "OV_PROFILE_FILTER",
    "OV_EVENT_EXCERPT_CHECK",
    "OV_PREF_DEDUPE",
    "OV_PROFILE_PREFETCH_PEER_ONLY",
)
ACCOUNT = "viking://user/noot/memories"


def stderr_records(prefix, fn):
    """``(records, result)``: the ``<prefix> {json}`` lines ``fn()`` wrote to stderr."""
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        result = fn()
    marker = prefix + " "
    return [
        json.loads(line[len(marker) :])
        for line in buf.getvalue().splitlines()
        if line.startswith(marker)
    ], result


class Append:
    """Stands in for a StrPatch: the merged text is the old text plus ``text``."""

    def __init__(self, text):
        self.text = text


class WFile:
    def __init__(self, uri, content="", extra_fields=None):
        self.uri = uri
        self.content = content
        self.extra_fields = extra_fields or {}

    def plain_content(self):
        return self.content


class WUtils:
    @staticmethod
    def read(raw, uri=None):
        return WFile(uri, raw)

    @staticmethod
    def write(
        memory_file,
        content_template=None,
        extract_context=None,
        render_links=True,
        *,
        account_content_template_type=None,
    ):
        return memory_file.content


class WFS:
    """In-memory filesystem with the three calls the checks use."""

    def __init__(self, files=None, ls_error=None):
        self.files = dict(files or {})
        self.ls_calls = []
        self.ls_error = ls_error

    async def read_file(self, uri, ctx=None):
        if uri not in self.files:
            raise FileNotFoundError(uri)
        return self.files[uri]

    async def write_file(self, uri, content, ctx=None, lease_ref=None):
        self.files[uri] = content

    async def ls(self, uri, node_limit=1000, offset=0, sort_by=None, ctx=None):
        """Like ``VikingFS.ls``: one call returns at most ``node_limit`` entries."""
        self.ls_calls.append(uri)
        if self.ls_error:
            raise self.ls_error
        prefix = uri.rstrip("/") + "/"
        seen, out = set(), []
        for path in sorted(self.files):
            if not path.startswith(prefix):
                continue
            head, slash, _ = path[len(prefix) :].partition("/")
            if head in seen:
                continue
            seen.add(head)
            out.append({"name": head, "isDir": bool(slash), "uri": prefix + head})
        return out[offset : offset + node_limit]


def writer_module():
    """A module and updater shaped like memory_updater's: merge, then write, then save."""
    module = types.SimpleNamespace(MemoryFileUtils=type("Utils", (WUtils,), {}))

    class Updater:
        def __init__(self, fs):
            self.fs = fs

        def _get_viking_fs(self):
            return self.fs

        async def _apply_upsert(
            self, resolved_op, ctx, extract_context=None, lease_ref=None, uri_remap=None
        ):
            for uri in resolved_op.uris:
                fields = dict(resolved_op.memory_fields)
                patch = fields.pop("content")
                try:
                    old = await self.fs.read_file(uri)
                except FileNotFoundError:
                    old = ""
                merged = old + patch.text if isinstance(patch, Append) else patch
                mf = WFile(uri, merged, fields)
                out = module.MemoryFileUtils.write(mf, extract_context=extract_context)
                await self.fs.write_file(uri, out)

    module.Updater = Updater
    return module


def upsert(memory_type, uri, content, **fields):
    return types.SimpleNamespace(
        memory_type=memory_type,
        uris=[uri],
        memory_fields={"content": content, **fields},
    )


class WriteCheckCase(unittest.TestCase):
    """Installs the write checks on a fake module; the expected source hash is the fake's."""

    def setUp(self):
        for var in ENVS:
            os.environ.pop(var, None)
        mg._PREF_INDEX.clear()
        mg._MODE_WARNED.clear()
        self.module = writer_module()
        patch = mock.patch.object(
            mg,
            "EXPECTED_WRITE_SHA256",
            mg._source_hash(self.module.MemoryFileUtils.write),
        )
        patch.start()
        self.addCleanup(patch.stop)
        self.assertTrue(mg.install_write_checks(self.module, self.module.Updater))

    def tearDown(self):
        for var in ENVS:
            os.environ.pop(var, None)

    def apply(self, fs, operation, extract_context=None, prefix="ov-profile-filter"):
        updater = self.module.Updater(fs)
        return stderr_records(
            prefix,
            lambda: asyncio.run(
                updater._apply_upsert(operation, None, extract_context=extract_context)
            ),
        )


PROFILE_URI = "viking://user/noot/peers/pop/memories/profile.md"
USER_PROFILE_URI = f"{ACCOUNT}/profile.md"
CLEAN = "# Noot\n- Lives in Chicago (as of 2026-10-21)\n"
ACTIVITY = (
    "- Merged dotfiles#509 and compendium#1944 (as of 2026-10-21)\n"
    "- Tracks IMPR-1401 for the profile rewrite (as of 2026-10-21)\n"
    "- P2: the dotfiles filter misses a case (as of 2026-10-21)\n"
    "- Finished Phase 2 of the cutover (as of 2026-10-21)\n"
)


class ProfileFilterTests(WriteCheckCase):
    def test_reasons_each_pattern(self):
        for line, reason in (
            ("- Merged dotfiles#509 (as of 2026-10-21)", "pr_or_issue"),
            ("- Closed issue #12 (as of 2026-10-21)", "pr_or_issue"),
            ("- Works on BUG-1283 (as of 2026-10-21)", "item_id"),
            ("- Tracks TASK-7 (as of 2026-10-21)", "item_id"),
            ("- P1: a finding (as of 2026-10-21)", "finding_label"),
            ("* **P0**: a finding (as of 2026-10-21)", "finding_label"),
            ("- In Phase 3 of the cutover (as of 2026-10-21)", "phase"),
        ):
            self.assertEqual(mg.profile_bullet_reason(line), reason, line)

    def test_standing_facts_and_non_bullets_are_kept(self):
        for line in (
            "- Lives in Chicago (as of 2026-10-21)",
            "- Requires explicit OK before any live-cluster apply (as of 2026-10-21)",
            "- Prefers P2 over P3 triage order (as of 2026-10-21)",  # label not at start
            "# Noot, owner of #1 priorities",  # heading, not a bullet
            "Merged dotfiles#509 in prose",
        ):
            self.assertIsNone(mg.profile_bullet_reason(line), line)

    def test_continuation_lines_go_with_their_bullet(self):
        kept, removed = mg.filter_profile_content(
            "# N\n- Merged #5\n  and more detail\n- Lives in X\n"
        )
        self.assertEqual(kept, "# N\n- Lives in X\n")
        self.assertEqual([r for r, _ in removed], ["pr_or_issue", "continuation"])

    def test_shadow_is_the_default_and_writes_every_bullet(self):
        fs = WFS({PROFILE_URI: CLEAN})
        records, _ = self.apply(fs, upsert("profile", PROFILE_URI, Append(ACTIVITY)))
        self.assertEqual(fs.files[PROFILE_URI], CLEAN + ACTIVITY)
        self.assertEqual({r["action"] for r in records}, {"would_drop"})
        self.assertEqual(
            [r["reason"] for r in records],
            ["pr_or_issue", "item_id", "finding_label", "phase"],
        )
        self.assertTrue(all(r["uri"] == PROFILE_URI for r in records))

    def test_mode_1_filters_the_merged_text_of_a_patch(self):
        # The patch carries only the new bullets; the file after the merge is what is cut.
        os.environ["OV_PROFILE_FILTER"] = "1"
        fs = WFS({PROFILE_URI: CLEAN + "- Merged dotfiles#1 (as of 2026-10-01)\n"})
        records, _ = self.apply(
            fs,
            upsert(
                "profile",
                PROFILE_URI,
                Append("- Likes tea (as of 2026-10-21)\n" + ACTIVITY),
            ),
        )
        self.assertEqual(
            fs.files[PROFILE_URI], CLEAN + "- Likes tea (as of 2026-10-21)\n"
        )
        self.assertEqual({r["action"] for r in records}, {"dropped"})
        # the old bullet already in the file is cut as well: the check sees the merged text
        self.assertIn("Merged dotfiles#1", " ".join(r["bullet"] for r in records))

    def test_mode_0_is_off(self):
        os.environ["OV_PROFILE_FILTER"] = "0"
        fs = WFS({PROFILE_URI: CLEAN})
        records, _ = self.apply(fs, upsert("profile", PROFILE_URI, Append(ACTIVITY)))
        self.assertEqual(records, [])
        self.assertEqual(fs.files[PROFILE_URI], CLEAN + ACTIVITY)

    def test_a_typo_is_shadow_with_one_warning(self):
        os.environ["OV_PROFILE_FILTER"] = "on"
        fs = WFS({PROFILE_URI: CLEAN})
        with self.assertLogs("ov_memory_guard_patch", "WARNING") as logs:
            records, _ = self.apply(
                fs, upsert("profile", PROFILE_URI, Append(ACTIVITY))
            )
            self.apply(
                fs, upsert("profile", PROFILE_URI, Append("- x (as of 2026-10-21)\n"))
            )
        self.assertEqual(
            fs.files[PROFILE_URI], CLEAN + ACTIVITY + "- x (as of 2026-10-21)\n"
        )
        self.assertEqual({r["action"] for r in records}, {"would_drop"})
        self.assertEqual(sum("OV_PROFILE_FILTER" in line for line in logs.output), 1)

    def test_only_the_profile_type_is_filtered(self):
        os.environ["OV_PROFILE_FILTER"] = "1"
        uri = f"{ACCOUNT}/entities/repo.md"
        fs = WFS()
        records, _ = self.apply(fs, upsert("entities", uri, "- Merged dotfiles#509\n"))
        self.assertEqual(records, [])
        self.assertEqual(fs.files[uri], "- Merged dotfiles#509\n")

    def test_the_user_level_profile_is_never_filtered_or_logged(self):
        # profile-inject falls back to this file for a peer with no profile of its own
        for mode in ("1", "shadow"):
            os.environ["OV_PROFILE_FILTER"] = mode
            fs = WFS({USER_PROFILE_URI: CLEAN + "- Merged #12 (as of 2026-10-01)\n"})
            records, _ = self.apply(
                fs, upsert("profile", USER_PROFILE_URI, Append(ACTIVITY))
            )
            self.assertEqual(records, [], mode)
            self.assertEqual(
                fs.files[USER_PROFILE_URI],
                CLEAN + "- Merged #12 (as of 2026-10-01)\n" + ACTIVITY,
                mode,
            )

    def test_a_peer_profile_is_filtered_too(self):
        os.environ["OV_PROFILE_FILTER"] = "1"
        uri = "viking://user/noot/peers/workbook/memories/profile.md"
        fs = WFS()
        self.apply(fs, upsert("profile", uri, CLEAN + ACTIVITY))
        self.assertEqual(fs.files[uri], CLEAN)

    def test_a_failing_check_never_blocks_the_write(self):
        fs = WFS()
        with mock.patch.object(
            mg, "filter_profile_content", side_effect=RuntimeError("x")
        ):
            with self.assertLogs("ov_memory_guard_patch", "WARNING"):
                self.apply(fs, upsert("profile", PROFILE_URI, CLEAN))
        self.assertEqual(fs.files[PROFILE_URI], CLEAN)


class FakeExtractContext:
    def __init__(self, excerpts, resource=""):
        self.excerpts = excerpts
        self.resource = resource
        self.calls = []

    def get_event_content(self, ranges, summary, ratio=0.2):
        self.calls.append((ranges, summary, ratio))
        return self.excerpts.get(ranges, summary)

    def get_resource_event_content(self, ranges, summary):
        return self.resource


EVENT_URI = f"{ACCOUNT}/events/2026/10/30/compendium_pr_merged.md"


class EventExcerptTests(WriteCheckCase):
    def check(self, summary, excerpt, **kw):
        ec = FakeExtractContext({"0-3": excerpt}, **kw)
        records, _ = self.apply(
            WFS(),
            upsert(
                "events",
                EVENT_URI,
                "body",
                summary=summary,
                ranges="0-3",
                event_name="x",
            ),
            extract_context=ec,
            prefix="ov-event-excerpt",
        )
        return records, ec

    def test_the_three_mismatches_from_bug_1283_are_logged(self):
        for summary, excerpt, missing, seen in (
            (
                "Compendium PR #2216 was merged",
                "**noot**: merge #2283 now",
                ["#2216"],
                ["#2283"],
            ),
            (
                "Compendium PR #2227 was merged",
                "**noot**: merge #2283",
                ["#2227"],
                ["#2283"],
            ),
            (
                "Dotfiles PR #593 was merged",
                "**noot**: #2283 and #602",
                ["#593"],
                ["#2283", "#602"],
            ),
        ):
            records, _ = self.check(summary, excerpt)
            self.assertEqual(len(records), 1, summary)
            self.assertEqual(records[0]["missing"], missing)
            self.assertEqual(records[0]["excerpt_refs"], seen)
            self.assertEqual(records[0]["uri"], EVENT_URI)
            self.assertEqual(records[0]["action"], "mismatch")

    def test_the_excerpt_is_the_rendered_one(self):
        _, ec = self.check("PR #1 merged", "text #2")
        self.assertEqual(ec.calls, [("0-3", "PR #1 merged", 0)])

    def test_a_matching_or_numberless_summary_is_not_logged(self):
        self.assertEqual(self.check("PR #2283 merged", "merge #2283 now")[0], [])
        self.assertEqual(self.check("Merged the PR", "merge #2283 now")[0], [])
        # #22 is not #2283: whole numbers only
        self.assertEqual(len(self.check("PR #22 merged", "merge #2283")[0]), 1)

    def test_no_excerpt_to_compare_is_not_a_mismatch(self):
        summary = "PR #2216 merged"
        self.assertEqual(self.check(summary, summary)[0], [])
        self.assertEqual(
            self.check(
                summary,
                "",
            )[0],
            [],
        )
        self.assertEqual(
            self.check(summary, "x #1", resource="[link](viking://r)")[0], []
        )

    def test_the_write_is_never_changed_and_mode_0_is_off(self):
        os.environ["OV_EVENT_EXCERPT_CHECK"] = "1"  # log only: no enforcing mode
        fs = WFS()
        ec = FakeExtractContext({"0-3": "#2283"})
        records, _ = self.apply(
            fs,
            upsert("events", EVENT_URI, "body", summary="PR #1", ranges="0-3"),
            extract_context=ec,
            prefix="ov-event-excerpt",
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(fs.files[EVENT_URI], "body")
        os.environ["OV_EVENT_EXCERPT_CHECK"] = "0"
        records, _ = self.apply(
            WFS(),
            upsert("events", EVENT_URI, "body", summary="PR #1", ranges="0-3"),
            extract_context=ec,
            prefix="ov-event-excerpt",
        )
        self.assertEqual(records, [])


PREFS = f"{ACCOUNT}/preferences"
SAME_BODY = "Never delete a repo or worktree without an explicit OK from the user.\n"
URI_A = f"{PREFS}/noot/repo_deletion_safety.md"
URI_B = f"{PREFS}/pop/repo_rules_opt_out.md"


class PreferenceDedupeTests(WriteCheckCase):
    def put(self, fs, uri, body, **kw):
        return self.apply(
            fs,
            upsert("preferences", uri, body, user="x", topic="y"),
            prefix="ov-pref-dup",
            **kw,
        )

    def test_a_duplicate_under_another_user_and_topic_is_logged_and_written(self):
        for mode in ("shadow", "1"):
            mg._PREF_INDEX.clear()
            os.environ["OV_PREF_DEDUPE"] = mode
            fs = WFS({URI_A: SAME_BODY, f"{PREFS}/noot/other.md": "different\n"})
            records, _ = self.put(fs, URI_B, SAME_BODY)
            self.assertEqual(
                records,
                [
                    {
                        "action": "duplicate",
                        "uri": URI_B,
                        "duplicate_of": URI_A,
                        "chars": len(SAME_BODY.strip()),
                    }
                ],
                mode,
            )
            self.assertEqual(fs.files[URI_B], SAME_BODY, mode)  # never skipped

    def test_a_different_body_and_a_rewrite_of_the_same_file_are_not_duplicates(self):
        os.environ["OV_PREF_DEDUPE"] = "1"
        fs = WFS({URI_A: SAME_BODY})
        self.assertEqual(self.put(fs, URI_B, "Something else.\n")[0], [])
        self.assertEqual(fs.files[URI_B], "Something else.\n")
        records, _ = self.put(fs, URI_A, Append(""))  # same body, same file
        self.assertEqual(records, [])
        self.assertEqual(self.put(fs, f"{PREFS}/pop/empty.md", "  \n")[0], [])

    def test_an_update_to_an_existing_file_is_written_even_when_it_matches_another(
        self,
    ):
        # Codex round 1, P2: a skip here would leave the outdated preference in place
        os.environ["OV_PREF_DEDUPE"] = "1"
        old, new = "Prefers tea daily.\n", "Prefers coffee daily.\n"
        fs = WFS({URI_A: new, URI_B: old})
        records, _ = self.put(fs, URI_B, new)
        self.assertEqual([r["action"] for r in records], ["duplicate"])
        self.assertEqual(fs.files[URI_B], new)

    def test_every_target_of_a_multi_uri_operation_is_written(self):
        # Codex round 1, P2: one duplicate must not end the other targets' updates
        os.environ["OV_PREF_DEDUPE"] = "1"
        c = f"{PREFS}/pop/third.md"
        fs = WFS(
            {
                URI_A: "Prefers coffee daily.\n",
                URI_B: "Prefers tea daily.\n",
                c: "Prefers tea daily.\n",
            }
        )
        operation = upsert(
            "preferences", URI_B, "Prefers coffee daily.\n", user="x", topic="y"
        )
        operation.uris = [URI_B, c]
        records, _ = self.apply(fs, operation, prefix="ov-pref-dup")
        self.assertEqual(fs.files[URI_B], "Prefers coffee daily.\n")
        self.assertEqual(fs.files[c], "Prefers coffee daily.\n")
        self.assertEqual({r["uri"] for r in records}, {URI_B, c})

    def test_the_index_is_listed_once_then_extended_by_our_own_writes(self):
        os.environ["OV_PREF_DEDUPE"] = "1"
        fs = WFS({URI_A: SAME_BODY})
        self.put(fs, f"{PREFS}/pop/fresh.md", "Fresh fact.\n")
        listed = len(fs.ls_calls)
        self.assertGreater(listed, 0)
        # a fact written a moment ago is already known to the index
        records, _ = self.put(fs, f"{PREFS}/pop/fresh_again.md", "Fresh fact.\n")
        self.assertEqual([r["action"] for r in records], ["duplicate"])
        self.assertEqual(len(fs.ls_calls), listed)

    def test_the_index_expires(self):
        fs = WFS({URI_A: SAME_BODY})
        self.put(fs, URI_B, "x\n")
        listed = len(fs.ls_calls)
        root, (built, index) = next(iter(mg._PREF_INDEX.items()))
        mg._PREF_INDEX[root] = (built - mg.PREF_INDEX_TTL_S - 1, index)
        self.put(fs, f"{PREFS}/pop/z.md", "z\n")
        self.assertGreater(len(fs.ls_calls), listed)

    def test_the_index_pages_past_1000_files_in_a_folder(self):
        # Codex round 1, P2: VikingFS.ls returns at most 1000 entries per call
        files = {f"{PREFS}/noot/t{i:04d}.md": f"fact {i}\n" for i in range(1001)}
        fs = WFS(files)
        records, _ = self.put(fs, URI_B, "fact 1000\n")
        self.assertEqual(len(mg._PREF_INDEX[PREFS][1]), 1001 + 1)  # plus the write
        self.assertEqual(
            [r["duplicate_of"] for r in records], [f"{PREFS}/noot/t1000.md"]
        )

    def test_the_index_pages_past_1000_user_folders(self):
        files = {f"{PREFS}/u{i:04d}/t.md": f"fact {i}\n" for i in range(1001)}
        fs = WFS(files)
        records, _ = self.put(fs, URI_B, "fact 1000\n")
        self.assertEqual([r["duplicate_of"] for r in records], [f"{PREFS}/u1000/t.md"])

    def test_a_listing_failure_never_blocks_the_write(self):
        os.environ["OV_PREF_DEDUPE"] = "1"
        fs = WFS(ls_error=TimeoutError("ls"))
        with self.assertLogs("ov_memory_guard_patch", "WARNING"):
            self.put(fs, URI_B, SAME_BODY)
        self.assertEqual(fs.files[URI_B], SAME_BODY)

    def test_mode_0_lists_nothing(self):
        os.environ["OV_PREF_DEDUPE"] = "0"
        fs = WFS({URI_A: SAME_BODY})
        self.assertEqual(self.put(fs, URI_B, SAME_BODY)[0], [])
        self.assertEqual(fs.ls_calls, [])


class BrokenStderr:
    def write(self, text):
        raise OSError("stderr unavailable")

    def flush(self):
        raise OSError("stderr unavailable")


class RecordFailureTests(WriteCheckCase):
    """A logging failure never fails a write, in any mode (Codex round 1, P2)."""

    def apply_broken(self, fs, operation, extract_context=None):
        updater = self.module.Updater(fs)
        with (
            mock.patch.object(sys, "stderr", BrokenStderr()),
            self.assertLogs("ov_memory_guard_patch", "WARNING"),
        ):
            asyncio.run(
                updater._apply_upsert(operation, None, extract_context=extract_context)
            )

    def test_each_write_check_survives_a_failing_stderr(self):
        ec = FakeExtractContext({"0-3": "#2283"})
        for mode in ("shadow", "1"):
            for var in (
                "OV_PROFILE_FILTER",
                "OV_EVENT_EXCERPT_CHECK",
                "OV_PREF_DEDUPE",
            ):
                os.environ[var] = mode
            mg._PREF_INDEX.clear()
            fs = WFS({URI_A: SAME_BODY})
            self.apply_broken(fs, upsert("profile", PROFILE_URI, Append(ACTIVITY)))
            self.assertEqual("dotfiles#509" in fs.files[PROFILE_URI], mode == "shadow")
            self.apply_broken(
                fs,
                upsert("events", EVENT_URI, "body", summary="PR #1", ranges="0-3"),
                extract_context=ec,
            )
            self.assertEqual(fs.files[EVENT_URI], "body")
            self.apply_broken(
                fs, upsert("preferences", URI_B, SAME_BODY, user="x", topic="y")
            )
            self.assertEqual(fs.files[URI_B], SAME_BODY)


class PrefetchHandler:
    allow_self = True

    def __init__(self, dirs):
        self.dirs = dirs

    def render_schema_directories(self, memory_type_schema):
        return list(self.dirs)


class PrefetchProvider:
    def __init__(self, handler, schema):
        self.handler, self.schema = handler, schema

    async def prefetch(self):
        return self.handler.render_schema_directories(self.schema)


USER_DIR = "viking://user/noot/memories"
PEER_DIR = "viking://user/noot/peers/pop/memories"


class PrefetchPeerOnlyTests(unittest.TestCase):
    def setUp(self):
        os.environ.pop("OV_PROFILE_PREFETCH_PEER_ONLY", None)
        mg._MODE_WARNED.clear()
        self.addCleanup(os.environ.pop, "OV_PROFILE_PREFETCH_PEER_ONLY", None)
        self.render = mg.wrap_render_schema_directories(
            PrefetchHandler.render_schema_directories
        )
        self.prefetch = mg.wrap_prefetch(PrefetchProvider.prefetch)

    def handler(self, dirs=(USER_DIR, PEER_DIR), allow_self=True):
        handler = PrefetchHandler(dirs)
        handler.allow_self = allow_self
        handler.render_schema_directories = self.render.__get__(handler)
        return handler

    def run_prefetch(self, memory_type="profile", **kw):
        provider = PrefetchProvider(
            self.handler(**kw), types.SimpleNamespace(memory_type=memory_type)
        )
        provider.prefetch = self.prefetch.__get__(provider)
        return stderr_records(
            "ov-profile-prefetch", lambda: asyncio.run(provider.prefetch())
        )

    def test_shadow_keeps_both_and_logs_what_it_would_drop(self):
        records, dirs = self.run_prefetch()
        self.assertEqual(dirs, [USER_DIR, PEER_DIR])
        self.assertEqual(
            records,
            [
                {
                    "action": "would_drop",
                    "memory_type": "profile",
                    "dropped": USER_DIR,
                    "kept": [PEER_DIR],
                }
            ],
        )

    def test_mode_1_drops_the_user_level_directory_for_each_singleton_type(self):
        os.environ["OV_PROFILE_PREFETCH_PEER_ONLY"] = "1"
        for memory_type in ("profile", "identity", "soul"):
            records, dirs = self.run_prefetch(memory_type)
            self.assertEqual(dirs, [PEER_DIR], memory_type)
            self.assertEqual(records[0]["action"], "dropped")

    def test_other_cases_are_left_alone(self):
        os.environ["OV_PROFILE_PREFETCH_PEER_ONLY"] = "1"
        # not a singleton type
        self.assertEqual(self.run_prefetch("preferences")[1], [USER_DIR, PEER_DIR])
        # no peer directory in the set: the user-level file is the only one
        records, dirs = self.run_prefetch(dirs=(USER_DIR,))
        self.assertEqual((records, dirs), ([], [USER_DIR]))
        # a handler without the user level has nothing to drop
        self.assertEqual(self.run_prefetch(allow_self=False)[1], [USER_DIR, PEER_DIR])
        os.environ["OV_PROFILE_PREFETCH_PEER_ONLY"] = "0"
        self.assertEqual(self.run_prefetch()[1], [USER_DIR, PEER_DIR])

    def test_only_the_prefetch_is_affected(self):
        os.environ["OV_PROFILE_PREFETCH_PEER_ONLY"] = "1"
        handler = self.handler()
        schema = types.SimpleNamespace(memory_type="profile")
        records, dirs = stderr_records(
            "ov-profile-prefetch", lambda: handler.render_schema_directories(schema)
        )
        self.assertEqual((records, dirs), ([], [USER_DIR, PEER_DIR]))

    def test_the_scope_ends_with_the_prefetch(self):
        self.run_prefetch()
        self.assertFalse(mg._PREFETCH.get())

    def test_a_failing_stderr_never_fails_the_prefetch(self):
        for mode, expected in (("shadow", [USER_DIR, PEER_DIR]), ("1", [PEER_DIR])):
            os.environ["OV_PROFILE_PREFETCH_PEER_ONLY"] = mode
            with (
                mock.patch.object(sys, "stderr", BrokenStderr()),
                self.assertLogs("ov_memory_guard_patch", "WARNING"),
            ):
                handler = self.handler()
                provider = PrefetchProvider(
                    handler, types.SimpleNamespace(memory_type="profile")
                )
                provider.prefetch = self.prefetch.__get__(provider)
                self.assertEqual(asyncio.run(provider.prefetch()), expected, mode)

    def test_a_check_that_raises_falls_back_to_the_stock_directories(self):
        os.environ["OV_PROFILE_PREFETCH_PEER_ONLY"] = "1"
        with mock.patch.object(mg, "_peer_only", side_effect=RuntimeError("x")):
            with self.assertLogs("ov_memory_guard_patch", "WARNING"):
                self.assertEqual(self.run_prefetch()[1], [USER_DIR, PEER_DIR])

    def test_a_typo_is_shadow(self):
        os.environ["OV_PROFILE_PREFETCH_PEER_ONLY"] = "yes"
        with self.assertLogs("ov_memory_guard_patch", "WARNING"):
            records, dirs = self.run_prefetch()
        self.assertEqual(dirs, [USER_DIR, PEER_DIR])
        self.assertEqual(records[0]["action"], "would_drop")


class PrefetchApplyTests(unittest.TestCase):
    def setUp(self):
        self.saved = sys.modules.get("openviking")
        ov = types.ModuleType("openviking")
        ov.__version__ = mg.EXPECTED_VERSION
        sys.modules["openviking"] = ov

    def tearDown(self):
        if self.saved is None:
            sys.modules.pop("openviking", None)
        else:
            sys.modules["openviking"] = self.saved

    def test_each_apply_wraps_when_the_source_matches_and_refuses_on_drift(self):
        class Provider(PrefetchProvider):
            pass

        class Handler(PrefetchHandler):
            pass

        provider_module = types.SimpleNamespace(SessionExtractContextProvider=Provider)
        handler_module = types.SimpleNamespace(MemoryIsolationHandler=Handler)
        self.assertFalse(mg.apply_prefetch(provider_module))
        self.assertFalse(mg.apply_isolation(handler_module))
        self.assertFalse(getattr(Provider.prefetch, "_ov_memory_guard", False))
        with (
            mock.patch.object(
                mg,
                "EXPECTED_PREFETCH_SHA256",
                mg._source_hash(PrefetchProvider.prefetch),
            ),
            mock.patch.object(
                mg,
                "EXPECTED_RENDER_DIRS_SHA256",
                mg._source_hash(PrefetchHandler.render_schema_directories),
            ),
        ):
            self.assertTrue(mg.apply_prefetch(provider_module))
            self.assertTrue(mg.apply_isolation(handler_module))
            self.assertTrue(mg.apply_prefetch(provider_module))  # idempotent
        self.assertTrue(Provider.prefetch._ov_memory_guard)
        self.assertTrue(Handler.render_schema_directories._ov_memory_guard)

    def test_a_wrong_version_is_refused_and_the_kill_switch_works(self):
        sys.modules["openviking"].__version__ = "v0.4.99"
        self.assertFalse(
            mg.apply_prefetch(
                types.SimpleNamespace(SessionExtractContextProvider=PrefetchProvider)
            )
        )
        sys.modules["openviking"].__version__ = mg.EXPECTED_VERSION
        os.environ["OV_MEMORY_GUARD"] = "0"
        try:
            self.assertFalse(
                mg.apply_isolation(
                    types.SimpleNamespace(MemoryIsolationHandler=PrefetchHandler)
                )
            )
        finally:
            os.environ.pop("OV_MEMORY_GUARD", None)

    def test_write_checks_refuse_drifted_write_source(self):
        module = writer_module()
        self.assertFalse(mg.install_write_checks(module, module.Updater))
        self.assertFalse(
            getattr(module.MemoryFileUtils.write, "_ov_memory_guard", False)
        )


class WriteCheckLoaderTests(unittest.TestCase):
    def test_the_prefetch_modules_load_the_guard(self):
        self.assertEqual(
            sitecustomize._entries(
                sitecustomize.TARGETS[
                    "openviking.session.memory.session_extract_context_provider"
                ]
            ),
            [("ov_memory_guard_patch", "apply_prefetch")],
        )
        self.assertEqual(
            sitecustomize._entries(
                sitecustomize.TARGETS[
                    "openviking.session.memory.memory_isolation_handler"
                ]
            ),
            [("ov_memory_guard_patch", "apply_isolation")],
        )


# Skip only when the package is absent; a broken install fails (IMPR-1413).
if importlib.util.find_spec("openviking") is None:  # not in the openviking image
    installed = None
else:
    from openviking.session.memory import memory_updater as installed


@unittest.skipIf(installed is None, "needs the openviking v0.5.1.1 image")
class Installed(unittest.TestCase):
    def test_hook_applied_the_guard_on_import(self):
        self.assertTrue(
            getattr(installed.MemoryUpdater.apply_operations, "_ov_memory_guard", False)
        )
        self.assertEqual(
            mg._source_hash(installed.MemoryUpdater.apply_operations.__wrapped__),
            mg.EXPECTED_APPLY_SHA256,
        )
        upsert = installed.MemoryUpdater._apply_upsert
        self.assertEqual(
            mg._source_hash(getattr(upsert, "__wrapped__", upsert)),
            mg.EXPECTED_UPSERT_SHA256,
        )

    def test_write_checks_and_prefetch_applied_on_import(self):
        from openviking.session.memory import (
            memory_isolation_handler as handler,
            session_extract_context_provider as provider,
        )

        self.assertTrue(installed.MemoryUpdater._apply_upsert._ov_memory_guard)
        write = installed.MemoryFileUtils.write
        self.assertTrue(write._ov_memory_guard)
        self.assertEqual(mg._source_hash(write.__wrapped__), mg.EXPECTED_WRITE_SHA256)
        prefetch = provider.SessionExtractContextProvider.prefetch
        self.assertEqual(
            mg._source_hash(prefetch.__wrapped__), mg.EXPECTED_PREFETCH_SHA256
        )
        render = handler.MemoryIsolationHandler.render_schema_directories
        self.assertEqual(
            mg._source_hash(render.__wrapped__), mg.EXPECTED_RENDER_DIRS_SHA256
        )

    def test_the_real_write_drops_activity_bullets_only_in_scope(self):
        uri = PROFILE_URI

        def write():
            mf = installed.MemoryFile.from_parsed(
                uri=uri,
                parsed={
                    "memory_type": "profile",
                    "content": CLEAN + ACTIVITY,
                },
            )
            return installed.MemoryFileUtils.write(mf)

        os.environ["OV_PROFILE_FILTER"] = "1"
        self.addCleanup(os.environ.pop, "OV_PROFILE_FILTER", None)
        self.assertIn("dotfiles#509", write())  # outside an _apply_upsert: untouched
        token = mg._WRITE_SCOPE.set(mg._WriteScope("profile", frozenset({uri})))
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                out = write()
        finally:
            mg._WRITE_SCOPE.reset(token)
        self.assertIn("Lives in Chicago", out)
        for gone in ("dotfiles#509", "IMPR-1401", "P2:", "Phase 2"):
            self.assertNotIn(gone, out)

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
        self.assertEqual(
            mg._source_hash(s.StreamingMemoryUpdater._split_append_only_request),
            mg.EXPECTED_SPLIT_SHA256,
        )

    def test_a_diverted_write_is_not_taken_for_a_rename(self):
        # v0.5.1.1 treats an upsert whose snapshot names another URI as a rename that
        # deletes the snapshot's file; the diversion clears the snapshot.
        target = f"{BASE}/x.md"
        o = op("x", "new", old=types.SimpleNamespace(uri=target))
        self.assertFalse(installed.MemoryUpdater._is_uri_migration(o))
        run(FakeFS({target: stored("other")}), ops(o))
        self.assertEqual(o.uris, [f"{BASE}/x_2.md"])
        self.assertIsNone(o.old_memory_file_content)
        self.assertFalse(installed.MemoryUpdater._is_uri_migration(o))
        o.old_memory_file_content = types.SimpleNamespace(uri=target)
        self.assertTrue(installed.MemoryUpdater._is_uri_migration(o))

    def test_singleton_guard_on_the_real_operation_types(self):
        # The guard reads ResolvedOperations.delete_file_contents / delete_replacements
        # and MemoryFile.memory_type by name; check them on the real classes.
        from openviking.session.memory.dataclass import ResolvedOperations

        operations = ResolvedOperations(
            upsert_operations=[],
            delete_file_contents=[
                installed.MemoryFile(uri=PEER_PROFILE, memory_type="profile"),
                installed.MemoryFile(uri=ENTITY, memory_type="entities"),
            ],
            errors=[],
            delete_replacements={PEER_PROFILE: USER_PROFILE},
        )
        _, dropped = singleton_records(lambda: mg.guard_singletons(operations))
        self.assertEqual(dropped, [PEER_PROFILE])
        self.assertEqual([f.uri for f in operations.delete_file_contents], [ENTITY])
        self.assertEqual(operations.delete_replacements, {PEER_PROFILE: USER_PROFILE})

    def test_real_materialize_keeps_a_migrated_singleton_source(self):
        # The hook wrapped the real classmethod; a profile migration no longer queues
        # its source for deletion, an entity migration still does.
        from openviking.session.memory.dataclass import (
            ResolvedOperation,
            ResolvedOperations,
        )

        materialize = installed.MemoryUpdater.__dict__["_materialize_uri_migrations"]
        self.assertTrue(getattr(materialize.__func__, "_ov_memory_guard", False))
        self.assertEqual(
            mg._source_hash(materialize.__func__.__wrapped__),
            mg.EXPECTED_MATERIALIZE_SHA256,
        )
        entity_target = "viking://user/u/peers/p/memories/entities/e.md"
        operations = ResolvedOperations(
            upsert_operations=[
                ResolvedOperation(
                    memory_fields={},
                    memory_type="profile",
                    uris=[PEER_PROFILE],
                    old_memory_file_content=installed.MemoryFile(
                        uri=USER_PROFILE, memory_type="profile"
                    ),
                ),
                ResolvedOperation(
                    memory_fields={},
                    memory_type="entities",
                    uris=[entity_target],
                    old_memory_file_content=installed.MemoryFile(
                        uri=ENTITY, memory_type="entities"
                    ),
                ),
            ],
            delete_file_contents=[],
            errors=[],
        )
        records, errors = singleton_records(
            lambda: installed.MemoryUpdater._materialize_uri_migrations(operations)
        )
        self.assertEqual(errors, [])
        self.assertEqual([f.uri for f in operations.delete_file_contents], [ENTITY])
        self.assertEqual(
            operations.delete_replacements,
            {USER_PROFILE: PEER_PROFILE, ENTITY: entity_target},
        )
        self.assertEqual([r["action"] for r in records], ["migration_source_kept"])

    def test_real_apply_moves_links_like_stock_and_keeps_the_source(self):
        # Codex review of homelab#285, R2 P1: a profile migration, then an ordinary
        # entity replacement. The guarded store must link the target exactly as stock
        # does at each stage; the only difference is that the source file stays.
        from unittest.mock import AsyncMock

        from openviking.session.memory.dataclass import (
            ResolvedOperation,
            ResolvedOperations,
            StoredLink,
        )

        replacement = "viking://user/u/memories/entities/replacement.md"

        class Store:
            def __init__(self):
                self.files = {}

            async def read_file(self, uri, **kwargs):
                if uri not in self.files:
                    raise FileNotFoundError(uri)
                return self.files[uri]

            async def write_file(self, uri, content, **kwargs):
                self.files[uri] = content

            async def rm(self, uri, **kwargs):
                del self.files[uri]

            def put(self, mf):
                self.files[mf.uri] = installed.MemoryFileUtils.write(mf)

            def parsed(self, uri):
                return installed.MemoryFileUtils.read(self.files[uri], uri=uri)

        def run(guard):
            fs = Store()
            link = StoredLink(
                from_uri=USER_PROFILE, to_uri=ENTITY, match_text="Entity"
            ).model_dump()
            fs.put(
                installed.MemoryFile(
                    uri=USER_PROFILE,
                    memory_type="profile",
                    content="About Entity.",
                    links=[link],
                )
            )
            fs.put(
                installed.MemoryFile(
                    uri=ENTITY,
                    memory_type="entities",
                    content="Entity",
                    backlinks=[link],
                )
            )
            fs.put(
                installed.MemoryFile(
                    uri=replacement, memory_type="entities", content="Replacement"
                )
            )
            schema = types.SimpleNamespace(
                fields=[],
                content_template=None,
                _account_content_template=False,
                overview_template=None,
                operation_mode="upsert",
            )
            updater = installed.MemoryUpdater(
                registry=types.SimpleNamespace(get=lambda _: schema)
            )
            updater._viking_fs = fs
            updater._sync_resource_refs_for_result = AsyncMock()
            stages = []

            def apply(operations, neighbor):
                result = asyncio.run(updater.apply_operations(operations, None))
                self.assertEqual(result.errors, [])
                stages.append(
                    (
                        fs.parsed(PEER_PROFILE).links,
                        fs.parsed(neighbor).backlinks,
                        USER_PROFILE in fs.files,
                    )
                )

            with mock.patch.dict(os.environ, {"OV_SINGLETON_GUARD": guard}):
                migration = ResolvedOperation(
                    memory_fields={},
                    memory_type="profile",
                    uris=[PEER_PROFILE],
                    old_memory_file_content=fs.parsed(USER_PROFILE),
                )
                apply(
                    ResolvedOperations(
                        upsert_operations=[migration],
                        delete_file_contents=[],
                        errors=[],
                    ),
                    ENTITY,
                )
                apply(
                    ResolvedOperations(
                        upsert_operations=[],
                        delete_file_contents=[fs.parsed(ENTITY)],
                        errors=[],
                        delete_replacements={ENTITY: replacement},
                    ),
                    replacement,
                )
            return stages

        with contextlib.redirect_stderr(io.StringIO()):
            guarded, stock = run("1"), run("0")
        for (g_links, g_back, g_kept), (s_links, s_back, s_kept) in zip(guarded, stock):
            self.assertEqual((g_links, g_back), (s_links, s_back))
            self.assertEqual((g_kept, s_kept), (True, False))
        self.assertEqual(guarded[1][0][0]["to_uri"], replacement)

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

    def test_extract_context_keeps_the_source_archive_uri(self):
        from openviking.message import Message

        msgs = [Message(id="u0", role="user", parts=[installed.TextPart(PASTE)])]
        ctx = installed.ExtractContext(
            msgs, source_archive_uri="viking://s/history/archive_005/"
        )
        self.assertEqual(ctx.source_archive_uri, "viking://s/history/archive_005")
        self.assertTrue(ctx.get_source_archive_suffix())
        self.assertTrue(ctx.messages[0].parts[0].text.endswith(mg.PASTE_PLACEHOLDER))

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
