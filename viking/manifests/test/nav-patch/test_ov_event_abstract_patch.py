"""IMPR-1200 event-abstract patch: semantics on fakes, plus checks against the real module.

The ``Installed`` cases need openviking importable (the v0.5.1.1 image, or the
``openviking`` uv tool through viking/scripts/run-nav-patch-tests.py) and are skipped
only when no openviking package is found; another version fails its guard.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "_scripts" / "lib"))
from git_env import scrub_environ  # noqa: E402

scrub_environ()

import importlib.util
import os
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ov_event_abstract_patch as ea

EVENT_URI = (
    "viking://user/noot-pilot/peers/github.com-nathanwhyte-compendium/memories/events/"
    "2026/09/24/kinde_login_attempts.md"
)
BODY = (
    "# Summary\nAttempted login; the CLI adds its own https:// scheme.\n"
    "# 2026-09-24 (Thursday) ChatLog:\n**github.com-nathanwhyte-compendium**: kinde login\n"
    "**assistant**: needs --domain\n"
)
SUMMARY = "Attempted login; the CLI adds its own https:// scheme."
ACCOUNT = "noot"


def stock_shape(msg):
    """A stand-in with v0.5.1.1's ``from_context(context, action, *, telemetry_id)``."""
    return lambda context, action=None, *, telemetry_id=None: msg


def record(**overrides):
    data = {"uri": EVENT_URI, "context_type": "memory", "level": 2, "abstract": BODY}
    data.update(overrides)
    return data


class SummaryTests(unittest.TestCase):
    def test_heading_summary(self):
        self.assertEqual(ea.summary_section(BODY), SUMMARY)

    def test_legacy_inline_summary(self):
        text = "Summary: One line.\n2026-09-24 (Thursday) ChatLog:\nuser: hi"
        self.assertEqual(ea.summary_section(text), "One line.")

    def test_no_summary(self):
        self.assertEqual(ea.summary_section("just a body"), "")
        self.assertEqual(ea.summary_section(""), "")


class RecordTests(unittest.TestCase):
    def test_event_record_gets_its_summary(self):
        data = record()
        self.assertTrue(ea.summarize_event_abstract(data))
        self.assertEqual(data["abstract"], SUMMARY)

    def test_other_memory_types_untouched(self):
        for uri in (
            EVENT_URI.replace("/events/2026/09/24/", "/entities/tools/"),
            EVENT_URI.replace("/events/2026/09/24/", "/preferences/"),
        ):
            data = record(uri=uri)
            self.assertFalse(ea.summarize_event_abstract(data))
            self.assertEqual(data["abstract"], BODY)

    def test_resources_and_non_detail_levels_untouched(self):
        for data in (
            record(context_type="resource"),
            record(level=0),
            record(level=1),
            record(uri=EVENT_URI.replace(".md", "")),
        ):
            self.assertFalse(ea.summarize_event_abstract(data))
            self.assertEqual(data["abstract"], BODY)

    def test_body_without_summary_keeps_its_abstract(self):
        data = record(abstract="no summary heading here")
        self.assertFalse(ea.summarize_event_abstract(data))
        self.assertEqual(data["abstract"], "no summary heading here")

    def test_already_trimmed_is_a_no_op(self):
        data = record(abstract=f"# Summary\n{SUMMARY}")
        self.assertTrue(ea.summarize_event_abstract(data))
        self.assertFalse(ea.summarize_event_abstract(data))
        self.assertEqual(data["abstract"], SUMMARY)

    def test_non_dict_is_ignored(self):
        self.assertFalse(ea.summarize_event_abstract(None))

    def test_message_preferred_over_stale_stored_abstract(self):
        """Reindex staleness (Codex finding 2): an edited body's Summary wins even
        when the record's incoming abstract still carries the old Summary."""
        edited_body = BODY.replace(
            "Attempted login; the CLI adds its own https:// scheme.",
            "Retried login after adding --domain; it succeeded.",
        )
        data = record(abstract=SUMMARY)  # stale: the previously-stored Summary
        self.assertTrue(ea.summarize_event_abstract(data, message=edited_body))
        self.assertEqual(
            data["abstract"], "Retried login after adding --domain; it succeeded."
        )

    def test_message_falls_back_to_abstract_when_not_a_string(self):
        for message in (None, "", "   ", ["multimodal", "parts"]):
            data = record()
            self.assertTrue(ea.summarize_event_abstract(data, message=message))
            self.assertEqual(data["abstract"], SUMMARY)

    def test_message_without_summary_falls_back_to_abstract(self):
        data = record()  # abstract carries a Summary
        self.assertTrue(ea.summarize_event_abstract(data, message="no summary here"))
        self.assertEqual(data["abstract"], SUMMARY)


class WrapperTests(unittest.TestCase):
    def test_embedding_text_is_never_touched(self):
        msg = types.SimpleNamespace(message=BODY, context_data=record())
        wrapped = ea.summary_abstract(stock_shape(msg))
        out = wrapped(object())
        self.assertIs(out, msg)
        self.assertEqual(out.message, BODY)
        self.assertEqual(out.context_data["abstract"], SUMMARY)

    def test_reindex_style_message_wins_over_stale_abstract(self):
        """The wrapper reads msg.message, so a reindex whose incoming abstract is
        stale still lands the Summary from the (fresher) message body."""
        edited_body = BODY.replace("Attempted login", "Retried login")
        msg = types.SimpleNamespace(
            message=edited_body, context_data=record(abstract=SUMMARY)
        )
        wrapped = ea.summary_abstract(stock_shape(msg))
        out = wrapped(object())
        self.assertEqual(out.message, edited_body)  # untouched
        self.assertEqual(out.context_data["abstract"], ea.summary_section(edited_body))

    def test_multimodal_message_falls_back_to_abstract(self):
        msg = types.SimpleNamespace(
            message=[{"type": "text", "text": "x"}], context_data=record()
        )
        wrapped = ea.summary_abstract(stock_shape(msg))
        out = wrapped(object())
        self.assertEqual(out.context_data["abstract"], SUMMARY)

    def test_none_passes_through_and_args_are_forwarded(self):
        seen = {}

        def original(context, action=None, *, telemetry_id=None):
            seen["args"] = (context, action, telemetry_id)
            return None

        wrapped = ea.summary_abstract(original)
        self.assertIsNone(wrapped("ctx", "upsert", telemetry_id="t"))
        self.assertEqual(seen["args"], ("ctx", "upsert", "t"))

    def test_original_errors_propagate(self):
        def fail(context, action=None, *, telemetry_id=None):
            raise ValueError("Embedding context requires account_id")

        with self.assertRaisesRegex(ValueError, "requires account_id"):
            ea.summary_abstract(fail)(None)


class ApplyTests(unittest.TestCase):
    def _module(self):
        class Converter:
            @staticmethod
            def from_context(context, action=None, *, telemetry_id=None):
                return types.SimpleNamespace(message="m", context_data=record())

        return types.SimpleNamespace(EmbeddingMsgConverter=Converter)

    def test_guards_switch_and_idempotence(self):
        module = self._module()
        original = module.EmbeddingMsgConverter.from_context
        ov = types.SimpleNamespace(__version__="v0.4.99")
        digest = ea.hashlib.sha256(ea.inspect.getsource(original).encode()).hexdigest()
        with (
            patch.dict(sys.modules, {"openviking": ov}),
            patch.dict(os.environ, {}, clear=True),
        ):
            self.assertFalse(ea.apply(module))  # wrong version
            ov.__version__ = ea.EXPECTED_VERSION
            self.assertFalse(ea.apply(module))  # source drift
            self.assertIs(module.EmbeddingMsgConverter.from_context, original)
            with patch.object(ea, "EXPECTED_SHA256", digest):
                with patch.dict(os.environ, {"OV_EVENT_ABSTRACT_PATCH": "0"}):
                    self.assertFalse(ea.apply(module))
                self.assertTrue(ea.apply(module))
                installed = module.EmbeddingMsgConverter.from_context
                self.assertTrue(ea.apply(module))
                self.assertIs(module.EmbeddingMsgConverter.from_context, installed)
                msg = module.EmbeddingMsgConverter.from_context(object())
                self.assertEqual(msg.context_data["abstract"], SUMMARY)
                self.assertEqual(msg.message, "m")


# Skip only when the package is absent; a broken install fails (IMPR-1413).
if importlib.util.find_spec("openviking") is None:
    _ov = None
else:
    import openviking as _ov
    from openviking.retrieve.context_assembler import tiers as _tiers
    from openviking.storage.queuefs import embedding_msg_converter as _emc


@unittest.skipIf(_ov is None, "needs openviking v0.5.1.1 importable")
class Installed(unittest.TestCase):
    def test_source_hash_matches(self):
        digest = ea.hashlib.sha256(
            ea.inspect.getsource(_emc.EmbeddingMsgConverter.from_context).encode()
        ).hexdigest()
        self.assertEqual(digest, ea.EXPECTED_SHA256)

    def test_summary_matches_the_overview_tier_helper(self):
        for text in (BODY, "Summary: One line.\nChatLog:\nx", "no summary", ""):
            self.assertEqual(
                ea.summary_section(text), _tiers.extract_summary_section(text)
            )

    def test_real_converter_keeps_embedding_text(self):
        from openviking.core.context import Context, ContextLevel, Vectorize

        with patch.object(_ov, "__version__", ea.EXPECTED_VERSION):
            converter = _emc.EmbeddingMsgConverter
            original = converter.__dict__["from_context"]
            try:
                self.assertTrue(ea.apply(_emc))
                context = Context(
                    uri=EVENT_URI,
                    parent_uri=EVENT_URI.rsplit("/", 1)[0],
                    is_leaf=True,
                    abstract=BODY,
                    context_type="memory",
                    level=ContextLevel.DETAIL,
                    account_id=ACCOUNT,
                )
                context.set_vectorize(Vectorize(text=BODY))
                msg = converter.from_context(context)
                self.assertEqual(msg.message, BODY)
                self.assertEqual(msg.context_data["abstract"], SUMMARY)
                self.assertEqual(msg.context_data["account_id"], ACCOUNT)
            finally:
                converter.from_context = original

    def test_real_converter_still_refuses_a_context_without_an_account(self):
        """v0.5.1.1 raises for a blank account_id instead of defaulting it to
        "default" (embedding_msg_converter.py:46-51); the wrapper must not mask
        that. ``Context()`` itself fills "default", so blank it afterwards."""
        from openviking.core.context import Context, ContextLevel, Vectorize

        with patch.object(_ov, "__version__", ea.EXPECTED_VERSION):
            converter = _emc.EmbeddingMsgConverter
            original = converter.__dict__["from_context"]
            try:
                self.assertTrue(ea.apply(_emc))
                context = Context(
                    uri=EVENT_URI,
                    parent_uri=EVENT_URI.rsplit("/", 1)[0],
                    is_leaf=True,
                    abstract=BODY,
                    context_type="memory",
                    level=ContextLevel.DETAIL,
                )
                context.set_vectorize(Vectorize(text=BODY))
                context.account_id = "  "
                with self.assertRaisesRegex(ValueError, "requires account_id"):
                    converter.from_context(context)
            finally:
                converter.from_context = original

    def test_real_converter_picks_up_an_edited_body_on_reindex(self):
        """Codex finding 2: a reindex-shaped call (vectorize text = the current file
        body, incoming abstract = the previously-stored, now-stale Summary) must
        pick the Summary out of the fresher body, not keep the stale abstract."""
        from openviking.core.context import Context, ContextLevel, Vectorize

        edited_body = BODY.replace("Attempted login", "Retried login")
        with patch.object(_ov, "__version__", ea.EXPECTED_VERSION):
            converter = _emc.EmbeddingMsgConverter
            original = converter.__dict__["from_context"]
            try:
                self.assertTrue(ea.apply(_emc))
                context = Context(
                    uri=EVENT_URI,
                    parent_uri=EVENT_URI.rsplit("/", 1)[0],
                    is_leaf=True,
                    abstract=SUMMARY,  # stale: the Summary stored before the edit
                    context_type="memory",
                    level=ContextLevel.DETAIL,
                    account_id=ACCOUNT,
                )
                context.set_vectorize(Vectorize(text=edited_body))
                msg = converter.from_context(context)
                self.assertEqual(msg.message, edited_body)
                self.assertEqual(
                    msg.context_data["abstract"], ea.summary_section(edited_body)
                )
                self.assertNotEqual(msg.context_data["abstract"], SUMMARY)
            finally:
                converter.from_context = original

    def test_reindex_hook_source_hash_matches(self):
        from openviking.service.reindex_executor import ReindexExecutor

        digest = ea.hashlib.sha256(
            ea.inspect.getsource(ReindexExecutor._upsert_context).encode()
        ).hexdigest()
        self.assertEqual(digest, ea.EXPECTED_UPSERT_CONTEXT_SHA256)

    def test_reindex_text_equals_the_write_path_text(self):
        """The reindex rewrite must embed exactly what MemoryUpdater._vectorize_memories
        enqueues for the same stored file: run the real updater on a fake store and
        compare its message with write_path_embedding_text on the same body."""
        from openviking.session.memory.memory_type_registry import (
            get_default_registry,
        )
        from openviking.session.memory.memory_updater import (
            MemoryUpdater,
            MemoryUpdateResult,
        )

        from openviking_cli.utils.config.config_loader import resolve_config_path
        from openviking_cli.utils.config.consts import (
            DEFAULT_OV_CONF,
            OPENVIKING_CONFIG_ENV,
        )

        # Skip only when OpenViking's own resolver finds no ov.conf; any other
        # registry failure fails the case (IMPR-1413).
        if resolve_config_path(None, OPENVIKING_CONFIG_ENV, DEFAULT_OV_CONF) is None:
            self.skipTest("no OpenViking config here (needs the image's ov.conf)")
        registry = get_default_registry()
        stored = (
            "---\nevent_name: kinde_login_attempts\ngoal: log in to the dev tenant\n---\n"
            + BODY.replace("needs --domain", "needs --domain, see [docs](viking://x/y)")
        )

        class FS:
            async def read_file(self, uri, ctx=None):
                return stored

        captured = []

        class DB:
            async def enqueue_embedding_msg(self, msg):
                captured.append(msg)
                return True

        updater = MemoryUpdater(registry=registry, vikingdb=DB())
        updater._viking_fs = FS()
        result = MemoryUpdateResult()
        result.add_edited(EVENT_URI)
        import asyncio

        asyncio.run(
            updater._vectorize_memories(
                result,
                ctx=types.SimpleNamespace(user=None, account_id=ACCOUNT),
                uri_memory_type_map={EVENT_URI: "events"},
            )
        )
        self.assertEqual(len(captured), 1)
        self.assertEqual(
            ea.write_path_embedding_text(
                stored, EVENT_URI, schema=registry.get("events")
            ),
            captured[0].message,
        )


class ReindexHookTests(unittest.TestCase):
    def _kwargs(self, **over):
        kw = {
            "uri": EVENT_URI,
            "parent_uri": EVENT_URI.rsplit("/", 1)[0],
            "abstract": BODY,
            "vector_text": "RAW BODY <!-- MEMORY_FIELDS -->",
            "is_leaf": True,
            "context_type": "memory",
            "level": types.SimpleNamespace(value=2),
            "ctx": None,
        }
        kw.update(over)
        return kw

    def _run(self, kwargs, rewrite):
        seen = {}

        async def original(self_, **kw):
            seen.update(kw)
            return "done"

        wrapped = ea.write_path_upsert(original)
        with patch.object(ea, "write_path_embedding_text", rewrite):
            import asyncio

            self.assertEqual(asyncio.run(wrapped(object(), **kwargs)), "done")
        return seen

    def test_event_upsert_gets_write_path_text(self):
        seen = self._run(self._kwargs(), lambda body, uri: "WRITE PATH TEXT")
        self.assertEqual(seen["vector_text"], "WRITE PATH TEXT")
        self.assertEqual(seen["abstract"], BODY)

    def test_other_records_untouched(self):
        for over in (
            {"uri": EVENT_URI.replace("/events/2026/09/24/", "/entities/tools/")},
            {"context_type": "resource"},
            {"level": types.SimpleNamespace(value=1)},
            {"vector_text": ""},
            {"vector_text": None},
        ):
            kw = self._kwargs(**over)
            seen = self._run(kw, lambda body, uri: "WRITE PATH TEXT")
            self.assertEqual(seen["vector_text"], kw["vector_text"], over)

    def test_unreproducible_template_or_failure_keeps_stock_text(self):
        seen = self._run(self._kwargs(), lambda body, uri: None)
        self.assertEqual(seen["vector_text"], "RAW BODY <!-- MEMORY_FIELDS -->")

        def boom(body, uri):
            raise RuntimeError("parse failed")

        seen = self._run(self._kwargs(), boom)
        self.assertEqual(seen["vector_text"], "RAW BODY <!-- MEMORY_FIELDS -->")

    def test_extract_context_template_is_refused(self):
        schema = types.SimpleNamespace(
            embedding_template="{{ extract_context.x }} {{ content }}"
        )
        self.assertIsNone(
            ea.write_path_embedding_text("# Summary\nx", EVENT_URI, schema=schema)
        )

    def test_apply_reindex_guards_switches_and_idempotence(self):
        class Executor:
            async def _upsert_context(self, **kwargs):
                return kwargs

        module = types.SimpleNamespace(ReindexExecutor=Executor)
        original = Executor.__dict__["_upsert_context"]
        digest = ea.hashlib.sha256(ea.inspect.getsource(original).encode()).hexdigest()
        ov = types.SimpleNamespace(__version__="v0.4.99")
        with (
            patch.dict(sys.modules, {"openviking": ov}),
            patch.dict(os.environ, {}, clear=True),
        ):
            self.assertFalse(ea.apply_reindex(module))
            ov.__version__ = ea.EXPECTED_VERSION
            self.assertFalse(ea.apply_reindex(module))  # source drift
            with patch.object(ea, "EXPECTED_UPSERT_CONTEXT_SHA256", digest):
                for env in (
                    {"OV_EVENT_ABSTRACT_PATCH": "0"},
                    {"OV_EVENT_REINDEX_PATCH": "0"},
                ):
                    with patch.dict(os.environ, env):
                        self.assertFalse(ea.apply_reindex(module))
                self.assertIs(Executor.__dict__["_upsert_context"], original)
                self.assertTrue(ea.apply_reindex(module))
                installed = Executor.__dict__["_upsert_context"]
                self.assertTrue(ea.apply_reindex(module))
                self.assertIs(Executor.__dict__["_upsert_context"], installed)


if __name__ == "__main__":
    unittest.main()
