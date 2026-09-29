"""IMPR-1215: assembled context carries each memory's vector ``updated_at``.

The offline tests use stand-ins for the retriever and the render/budget functions.
The ``RealModuleTests`` run only inside the v0.4.20 image: they apply the hooks to
the real modules (so the version and source guards are exercised too) and render
entries end to end.
"""

import asyncio
import os
import sys
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
        return [c["uri"] for c in candidates]


class RecordingTests(unittest.TestCase):
    def setUp(self):
        rt._times.clear()

    def test_convert_records_raw_and_display_uris_and_keeps_results(self):
        convert = rt.recording_convert(FakeRetriever._convert_to_matched_contexts)
        candidates = [
            {
                "uri": "viking://user/u/memories/events/a.md",
                "level": 2,
                "updated_at": "2026-09-29T17:45:58Z",
            },
            {
                "uri": "viking://user/u/memories/events",
                "level": 1,
                "updated_at": "2026-09-28T01:02:03+00:00",
            },
            {"uri": "viking://user/u/memories/events/b.md", "level": 2},
        ]
        result = asyncio.run(convert(FakeRetriever(), candidates, None))
        self.assertEqual(result, [c["uri"] for c in candidates])
        self.assertEqual(
            rt.lookup("viking://user/u/memories/events/a.md"), "2026-09-29T17:45:58Z"
        )
        self.assertEqual(
            rt.lookup("viking://user/u/memories/events/.overview.md"),
            "2026-09-28T01:02:03Z",
        )
        self.assertIsNone(rt.lookup("viking://user/u/memories/events/b.md"))

    def test_the_map_is_bounded_and_refreshes_on_reuse(self):
        with mock.patch.object(rt, "MAX_TIMES", 2):
            rt.remember("a", "1")
            rt.remember("b", "2")
            rt.remember("a", "3")
            rt.remember("c", "4")
        self.assertEqual(dict(rt._times), {"a": "3", "c": "4"})


@dataclass
class FakeEntry:
    uri: str
    detail: str
    text: str = ""
    tokens: int = 0


@dataclass
class FakeCandidate:
    uri: str
    base_uri: str


def fake_render(entry):
    head = (
        f'<memory uri="{entry.uri}" type="events" score="0.77" detail="{entry.detail}"'
    )
    return f"{head} />" if not entry.text else f"{head}>\n{entry.text}\n</memory>"


class CarryTests(unittest.TestCase):
    def setUp(self):
        rt._times.clear()

    def test_make_entry_copies_the_time_and_recounts_tokens(self):
        rt.remember("viking://m/a.md", "2026-09-29T17:45:58Z")
        render = rt.timed_render_entry(fake_render)
        make = rt.timed_make_entry(
            lambda c, tier, text: FakeEntry(
                uri=c.base_uri, detail=tier, text=text, tokens=1
            ),
            lambda e: len(render(e)),
        )
        entry = make(
            FakeCandidate("viking://m/a.md", "viking://m/a.md"), "overview", "body"
        )
        self.assertEqual(entry.updated_at, "2026-09-29T17:45:58Z")
        self.assertEqual(entry.tokens, len(render(entry)))

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
    def setUp(self):
        rt._times.clear()

    def test_all_four_hooks_apply_and_render_end_to_end(self):
        from openviking.retrieve import hierarchical_retriever as h
        from openviking.retrieve.context_assembler import budget, gather, models, render

        self.assertTrue(rt.apply_render(render))
        self.assertTrue(rt.apply_models(models))
        self.assertTrue(rt.apply_budget(budget))
        self.assertTrue(rt.apply_retriever(h))
        # Applying again is a no-op, not a double wrap.
        self.assertTrue(rt.apply_render(render))
        self.assertTrue(getattr(render.render_entry, "_ov_recall_time_patch", False))

        rt.remember("viking://user/u/memories/events/a.md", "2026-09-29T17:45:58Z")
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
        entry = budget._make_entry(candidate, "overview", "body")
        rendered = render.render_context([entry])
        self.assertIn('detail="overview" updated="2026-09-29T17:45:58Z">', rendered)
        self.assertEqual(entry.to_dict()["updated_at"], "2026-09-29T17:45:58Z")
        self.assertEqual(entry.tokens, render.fragment_tokens(entry))


if __name__ == "__main__":
    unittest.main()
