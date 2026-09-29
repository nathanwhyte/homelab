"""IMPR-1215: assembled context carries each memory's time; reindex keeps it true.

The offline tests use stand-ins for the retriever, gather, budget, render and reindex
functions. ``RealModuleTests`` run only inside the v0.4.20 image: they apply the hooks
to the real modules (so the version and source guards run too), stack them on the
event-abstract hooks the way ``sitecustomize`` orders them, and render end to end.
"""

import asyncio
import os
import sys
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ov_recall_time_patch as rt


class NormalizeTests(unittest.TestCase):
    def test_iso_strings_become_utc_seconds(self):
        self.assertEqual(
            rt.normalize("2026-09-29T17:45:58.123456Z"), "2026-09-29T17:45:58Z"
        )
        self.assertEqual(
            rt.normalize("2026-09-29T12:45:58-05:00"), "2026-09-29T17:45:58Z"
        )
        self.assertEqual(rt.normalize("2026-09-29T17:45:58"), "2026-09-29T17:45:58Z")

    def test_datetimes_are_accepted(self):
        dt = datetime(2026, 9, 29, 12, 45, 58, tzinfo=timezone(timedelta(hours=-5)))
        self.assertEqual(rt.normalize(dt), "2026-09-29T17:45:58Z")

    def test_unusable_values_are_none(self):
        for value in (None, "", "  ", "not a date", 17, [], {}):
            self.assertIsNone(rt.normalize(value))


class FakeRetriever:
    @classmethod
    def _append_level_suffix(cls, uri, level):
        return f"{uri}/.overview.md" if level == 1 else uri

    async def _convert_to_matched_contexts(self, candidates, ctx, apply_hotness=True):
        await asyncio.sleep(0)
        return [c["uri"] for c in candidates]


@dataclass
class FakeCandidate:
    uri: str
    base_uri: str


def fake_gather(convert):
    """A gather that retrieves ``hits`` in a sub-task, the way find fans out."""

    async def gather_candidates(hits, delay=0.0):
        async def retrieve():
            await asyncio.sleep(delay)
            return await convert(FakeRetriever(), hits, None)

        uris = await asyncio.create_task(retrieve())
        return [FakeCandidate(u, u) for u in uris], {"n": len(uris)}

    return gather_candidates


class ReadPathTests(unittest.TestCase):
    def setUp(self):
        self.convert = rt.recording_convert(FakeRetriever._convert_to_matched_contexts)
        self.gather = rt.timed_gather(fake_gather(self.convert))

    def test_times_ride_on_the_request_candidates(self):
        hits = [
            {
                "uri": "viking://m/events/a.md",
                "level": 2,
                "updated_at": "2026-09-29T17:45:58Z",
            },
            {
                "uri": "viking://m/events",
                "level": 1,
                "updated_at": "2026-09-28T01:02:03+00:00",
            },
            {"uri": "viking://m/events/b.md", "level": 2},
        ]
        candidates, stats = asyncio.run(self.gather(hits))
        self.assertEqual(stats, {"n": 3})
        times = {c.uri: getattr(c, "updated_at", None) for c in candidates}
        self.assertEqual(times["viking://m/events/a.md"], "2026-09-29T17:45:58Z")
        self.assertEqual(times["viking://m/events"], "2026-09-28T01:02:03Z")
        self.assertIsNone(times["viking://m/events/b.md"])

    def test_concurrent_requests_never_see_each_others_times(self):
        uri = "viking://user/u/memories/events/a.md"

        async def both():
            slow = self.gather(
                [{"uri": uri, "level": 2, "updated_at": "2026-01-01T00:00:00Z"}],
                delay=0.05,
            )
            fast = self.gather(
                [{"uri": uri, "level": 2, "updated_at": "2026-09-29T00:00:00Z"}]
            )
            return await asyncio.gather(slow, fast)

        (a, _), (b, _) = asyncio.run(both())
        self.assertEqual(a[0].updated_at, "2026-01-01T00:00:00Z")
        self.assertEqual(b[0].updated_at, "2026-09-29T00:00:00Z")

    def test_a_hit_without_a_time_does_not_inherit_an_earlier_one(self):
        uri = "viking://m/events/a.md"
        asyncio.run(
            self.gather(
                [{"uri": uri, "level": 2, "updated_at": "2026-09-29T17:45:58Z"}]
            )
        )
        candidates, _ = asyncio.run(self.gather([{"uri": uri, "level": 2}]))
        self.assertFalse(hasattr(candidates[0], "updated_at"))

    def test_convert_outside_an_assembly_records_nothing(self):
        hits = [
            {"uri": "viking://m/a.md", "level": 2, "updated_at": "2026-09-29T17:45:58Z"}
        ]
        self.assertEqual(
            asyncio.run(self.convert(FakeRetriever(), hits, None)), ["viking://m/a.md"]
        )
        self.assertIsNone(rt._request_times.get())


@dataclass
class FakeEntry:
    uri: str
    detail: str
    text: str = ""
    tokens: int = 0


def fake_render(entry):
    head = (
        f'<memory uri="{entry.uri}" type="events" score="0.77" detail="{entry.detail}"'
    )
    return f"{head} />" if not entry.text else f"{head}>\n{entry.text}\n</memory>"


class CarryTests(unittest.TestCase):
    def test_make_entry_reserves_the_localized_length(self):
        render = rt.timed_render_entry(fake_render)
        make = rt.timed_make_entry(
            lambda c, tier, text: FakeEntry(
                uri=c.base_uri, detail=tier, text=text, tokens=1
            ),
            lambda e: len(render(e)),
        )
        candidate = FakeCandidate("viking://m/a.md", "viking://m/a.md")
        candidate.updated_at = "2026-09-29T17:45:58Z"
        entry = make(candidate, "overview", "body")
        self.assertEqual(entry.updated_at, "2026-09-29T17:45:58Z")
        localized = FakeEntry(entry.uri, entry.detail, entry.text)
        localized.updated_at = "2026-09-29T12:45:58-05:00"
        self.assertEqual(entry.tokens, len(render(localized)))
        self.assertGreater(entry.tokens, len(render(entry)))

    def test_make_entry_without_a_time_is_untouched(self):
        make = rt.timed_make_entry(
            lambda c, tier, text: FakeEntry(
                uri=c.base_uri, detail=tier, text=text, tokens=1
            ),
            lambda e: 999,
        )
        entry = make(FakeCandidate("viking://m/x.md", "viking://m/x.md"), "uri", "")
        self.assertFalse(hasattr(entry, "updated_at"))
        self.assertEqual(entry.tokens, 1)

    def test_render_adds_updated_after_detail_on_both_tag_shapes(self):
        render = rt.timed_render_entry(fake_render)
        full = FakeEntry("viking://m/a.md", "overview", "body")
        full.updated_at = "2026-09-29T17:45:58Z"
        bare = FakeEntry("viking://m/b.md", "uri")
        bare.updated_at = "2026-09-28T01:02:03Z"
        self.assertEqual(
            render(full),
            '<memory uri="viking://m/a.md" type="events" score="0.77" detail="overview"'
            ' updated="2026-09-29T17:45:58Z">\nbody\n</memory>',
        )
        self.assertTrue(
            render(bare).endswith('detail="uri" updated="2026-09-28T01:02:03Z" />')
        )

    def test_render_without_a_time_is_the_original(self):
        entry = FakeEntry("viking://m/a.md", "overview", "body")
        self.assertEqual(rt.timed_render_entry(fake_render)(entry), fake_render(entry))

    def test_to_dict_adds_updated_at_only_when_known(self):
        to_dict = rt.timed_to_dict(lambda self: {"uri": self.uri})
        entry = FakeEntry("viking://m/a.md", "overview")
        self.assertEqual(to_dict(entry), {"uri": "viking://m/a.md"})
        entry.updated_at = "2026-09-29T17:45:58Z"
        self.assertEqual(to_dict(entry)["updated_at"], "2026-09-29T17:45:58Z")


class FakeExecutor:
    def __init__(self, converter):
        self.converter = converter
        self.built = []

    async def _upsert_context(self, *, uri, ctx, **_):
        # Like v0.4.20: a fresh context whose times default to now, then the converter.
        now = datetime.now(timezone.utc)
        context = SimpleNamespace(uri=uri, created_at=now, updated_at=now)
        self.converter(context)
        self.built.append(context)


class ReindexTests(unittest.TestCase):
    def run_upsert(self, uri, stat_result=None, stat_error=None):
        converter = rt.mod_time_from_context(lambda context: context)
        executor = FakeExecutor(converter)
        upsert = rt.mod_time_upsert(FakeExecutor._upsert_context)

        async def fake_mod_time(u, ctx):
            if stat_error:
                return None  # file_mod_time's contract: a failed stat is None
            return rt.parse(stat_result.get("modTime"))

        with mock.patch.object(rt, "file_mod_time", new=fake_mod_time):
            asyncio.run(upsert(executor, uri=uri, ctx=None))
        return executor.built[0]

    def test_reindex_keeps_the_files_mod_time(self):
        context = self.run_upsert(
            "viking://m/events/2026/09/22/a.md", {"modTime": "2026-09-22T14:23:37Z"}
        )
        expected = datetime(2026, 9, 22, 14, 23, 37, tzinfo=timezone.utc)
        self.assertEqual(context.created_at, expected)
        self.assertEqual(context.updated_at, expected)

    def test_a_failed_stat_keeps_the_default(self):
        before = datetime.now(timezone.utc) - timedelta(seconds=5)
        context = self.run_upsert("viking://m/a.md", stat_error=OSError("gone"))
        self.assertGreater(context.updated_at, before)

    def test_the_converter_outside_a_reindex_is_untouched(self):
        converter = rt.mod_time_from_context(lambda context: context)
        now = datetime.now(timezone.utc)
        context = converter(
            SimpleNamespace(uri="viking://m/a.md", created_at=now, updated_at=now)
        )
        self.assertEqual(context.updated_at, now)

    def test_a_context_for_another_uri_is_untouched(self):
        converter = rt.mod_time_from_context(lambda context: context)
        now = datetime.now(timezone.utc)
        token = rt._reindex_time.set(
            ("viking://m/a.md", datetime(2020, 1, 1, tzinfo=timezone.utc))
        )
        try:
            other = converter(
                SimpleNamespace(uri="viking://m/b.md", created_at=now, updated_at=now)
            )
        finally:
            rt._reindex_time.reset(token)
        self.assertEqual(other.updated_at, now)


class FileModTimeTests(unittest.TestCase):
    def test_stat_errors_and_bad_values_become_none(self):
        fs = mock.Mock()
        fs.stat = mock.AsyncMock(side_effect=OSError("gone"))
        viking_fs = SimpleNamespace(get_viking_fs=lambda: fs)
        with mock.patch.dict(sys.modules, {"openviking.storage.viking_fs": viking_fs}):
            self.assertIsNone(asyncio.run(rt.file_mod_time("viking://m/a.md", None)))
            fs.stat = mock.AsyncMock(return_value={"modTime": "garbage"})
            self.assertIsNone(asyncio.run(rt.file_mod_time("viking://m/a.md", None)))
            fs.stat = mock.AsyncMock(return_value={"modTime": "2026-09-22T14:23:37Z"})
            self.assertEqual(
                asyncio.run(rt.file_mod_time("viking://m/a.md", None)),
                datetime(2026, 9, 22, 14, 23, 37, tzinfo=timezone.utc),
            )
            fs.stat.assert_awaited_with("viking://m/a.md", ctx=None, skip_count=True)


class GuardTests(unittest.TestCase):
    def test_off_switch_disables_every_hook(self):
        with mock.patch.dict(os.environ, {"OV_RECALL_TIME_PATCH": "0"}):
            self.assertFalse(rt._guarded("render_entry", fake_render))

    def test_a_changed_source_is_not_patched(self):
        try:
            import openviking  # noqa: F401
        except ImportError:
            self.skipTest("needs the openviking package")
        self.assertFalse(rt._guarded("render_entry", fake_render))


try:
    import openviking  # noqa: F401

    HAVE_OV = True
except ImportError:
    HAVE_OV = False


@unittest.skipUnless(HAVE_OV, "needs the v0.4.20 openviking package")
class RealModuleTests(unittest.TestCase):
    def test_read_hooks_apply_and_render_end_to_end(self):
        from openviking.retrieve import hierarchical_retriever as h
        from openviking.retrieve.context_assembler import budget, gather, models, render

        self.assertTrue(rt.apply_render(render))
        self.assertTrue(rt.apply_models(models))
        self.assertTrue(rt.apply_budget(budget))
        self.assertTrue(rt.apply_retriever(h))
        self.assertTrue(rt.apply_gather(gather))
        self.assertTrue(rt.apply_render(render))  # idempotent, not a double wrap
        self.assertTrue(getattr(render.render_entry, "_ov_recall_time_patch", False))

        fields = {f.name for f in gather.Candidate.__dataclass_fields__.values()}
        values = {
            "uri": "viking://user/u/memories/events/a.md",
            "base_uri": "viking://user/u/memories/events/a.md",
            "category": "events",
            "score": 0.77,
            "ranked_score": 0.77,
            "level": 2,
            "abstract": "abs",
            "origin": "own",
            "is_directory": False,
            "read_ctx": None,
        }
        candidate = gather.Candidate(**{k: v for k, v in values.items() if k in fields})
        candidate.updated_at = "2026-09-29T17:45:58Z"
        entry = budget._make_entry(candidate, "overview", "body")
        rendered = render.render_context([entry])
        self.assertIn('detail="overview" updated="2026-09-29T17:45:58Z">', rendered)
        self.assertEqual(entry.to_dict()["updated_at"], "2026-09-29T17:45:58Z")
        self.assertGreaterEqual(entry.tokens, render.fragment_tokens(entry))

    def test_write_hooks_stack_on_the_event_abstract_hooks(self):
        import ov_event_abstract_patch as ea
        from openviking.service import reindex_executor
        from openviking.storage.queuefs import embedding_msg_converter

        ea.apply(embedding_msg_converter)
        ea.apply_reindex(reindex_executor)
        self.assertTrue(rt.apply_converter(embedding_msg_converter))
        self.assertTrue(rt.apply_reindex(reindex_executor))
        converter = embedding_msg_converter.EmbeddingMsgConverter.__dict__[
            "from_context"
        ]
        self.assertIsInstance(converter, staticmethod)
        self.assertTrue(getattr(converter.__func__, "_ov_recall_time_patch", False))
        upsert = reindex_executor.ReindexExecutor.__dict__["_upsert_context"]
        self.assertTrue(getattr(upsert, "_ov_recall_time_patch", False))


if __name__ == "__main__":
    unittest.main()
