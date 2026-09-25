"""IMPR-1200 event-abstract patch: semantics on fakes, plus checks against the real module.

The ``Installed`` cases need openviking v0.4.20 importable (the image, or a local
site-packages on PYTHONPATH) and are skipped otherwise.
"""

import os
import sys
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


class WrapperTests(unittest.TestCase):
    def test_embedding_text_is_never_touched(self):
        msg = types.SimpleNamespace(message=BODY, context_data=record())
        wrapped = ea.summary_abstract(lambda context, creator_acl_grant=None: msg)
        out = wrapped(object())
        self.assertIs(out, msg)
        self.assertEqual(out.message, BODY)
        self.assertEqual(out.context_data["abstract"], SUMMARY)

    def test_none_passes_through_and_args_are_forwarded(self):
        seen = {}

        def original(context, creator_acl_grant=None):
            seen["args"] = (context, creator_acl_grant)
            return None

        self.assertIsNone(ea.summary_abstract(original)("ctx", creator_acl_grant="g"))
        self.assertEqual(seen["args"], ("ctx", "g"))

    def test_original_errors_propagate(self):
        def fail(context, creator_acl_grant=None):
            raise ValueError("bad context")

        with self.assertRaisesRegex(ValueError, "bad context"):
            ea.summary_abstract(fail)(None)


class ApplyTests(unittest.TestCase):
    def _module(self):
        class Converter:
            @staticmethod
            def from_context(context, creator_acl_grant=None):
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


try:
    import openviking as _ov
    from openviking.retrieve.context_assembler import tiers as _tiers
    from openviking.storage.queuefs import embedding_msg_converter as _emc
except Exception:  # noqa: BLE001 — anything short of a real install skips these cases
    _ov = None


@unittest.skipIf(_ov is None, "needs openviking v0.4.20 importable")
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
                )
                context.set_vectorize(Vectorize(text=BODY))
                msg = converter.from_context(context)
                self.assertEqual(msg.message, BODY)
                self.assertEqual(msg.context_data["abstract"], SUMMARY)
            finally:
                converter.from_context = original


if __name__ == "__main__":
    unittest.main()
