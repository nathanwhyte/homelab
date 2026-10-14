"""Model matching, parameter drop, pass-through, and guard checks for ov_claude_params_patch."""

import os
import sys
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ov_claude_params_patch as claude_patch


def _original(self, kwargs, thinking, max_tokens=None):
    kwargs["temperature"] = self.temperature
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    kwargs["extra_body"] = {"thinking": {"type": "disabled"}}


class _VLM:
    temperature = 0.0


class MatchTests(unittest.TestCase):
    def test_claude_models_match(self):
        self.assertTrue(claude_patch.is_claude_model("claude-haiku-5-5"))
        self.assertTrue(claude_patch.is_claude_model("Claude-Sonnet-5-5"))

    def test_other_models_do_not(self):
        for model in (
            "gemma4:vlm",
            "gpt-4o-mini",
            "anthropic/claude-haiku-5-5",
            None,
            5,
        ):
            self.assertFalse(claude_patch.is_claude_model(model), model)


class WrapperTests(unittest.TestCase):
    def setUp(self):
        self.wrapped = claude_patch.dropping_claude_params(_original)

    def test_claude_request_loses_temperature_only(self):
        kwargs = {"model": "claude-haiku-5-5", "messages": []}
        self.wrapped(_VLM(), kwargs, False, max_tokens=16384)
        self.assertNotIn("temperature", kwargs)
        self.assertEqual(kwargs["max_tokens"], 16384)
        self.assertEqual(kwargs["extra_body"], {"thinking": {"type": "disabled"}})

    def test_other_models_keep_temperature(self):
        kwargs = {"model": "gemma4:vlm", "messages": []}
        self.wrapped(_VLM(), kwargs, False)
        self.assertEqual(kwargs["temperature"], 0.0)

    def test_positional_max_tokens_passes_through(self):
        kwargs = {"model": "claude-haiku-5-5"}
        self.wrapped(_VLM(), kwargs, True, 64)
        self.assertEqual(kwargs["max_tokens"], 64)
        self.assertNotIn("temperature", kwargs)


class GuardTests(unittest.TestCase):
    def test_guards_and_idempotence(self):
        cls = type("OpenAIVLM", (), {"_apply_completion_params": _original})
        module = types.SimpleNamespace(OpenAIVLM=cls)
        ov = types.SimpleNamespace(__version__="v0.4.99")
        with (
            patch.dict(sys.modules, {"openviking": ov}),
            patch.dict(os.environ, {}, clear=True),
        ):
            self.assertFalse(claude_patch.apply(module))
            ov.__version__ = claude_patch.EXPECTED_VERSION
            self.assertFalse(claude_patch.apply(module))
            digest = claude_patch.hashlib.sha256(
                claude_patch.inspect.getsource(_original).encode()
            ).hexdigest()
            with patch.object(claude_patch, "EXPECTED_SHA256", digest):
                with patch.dict(os.environ, {"OV_CLAUDE_PARAMS_PATCH": "0"}):
                    self.assertFalse(claude_patch.apply(module))
                self.assertTrue(claude_patch.apply(module))
                installed = cls._apply_completion_params
                self.assertTrue(claude_patch.apply(module))
                self.assertIs(cls._apply_completion_params, installed)
                self.assertTrue(getattr(installed, "_ov_claude_params_patch", False))

    def test_missing_class_is_not_applied(self):
        ov = types.SimpleNamespace(__version__=claude_patch.EXPECTED_VERSION)
        with patch.dict(sys.modules, {"openviking": ov}):
            self.assertFalse(claude_patch.apply(types.SimpleNamespace()))


if __name__ == "__main__":
    unittest.main()
