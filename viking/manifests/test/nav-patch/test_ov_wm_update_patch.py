"""Detection, pass-through, skip response, and guard checks for ov_wm_update_patch."""

import asyncio
import io
import os
import sys
import types
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ov_wm_update_patch as wm_patch

UPDATE_TOOL = {"type": "function", "function": {"name": "update_working_memory"}}
CREATE_TOOL = {"type": "function", "function": {"name": "create_working_memory"}}
CHOICE = {"type": "function", "function": {"name": "update_working_memory"}}


class _FakeVLMResponse:
    def __init__(self, content=None, tool_calls=None, finish_reason="stop", usage=None):
        self.content = content
        self.tool_calls = list(tool_calls or [])
        self.finish_reason = finish_reason
        self.usage = usage or {}

    @property
    def has_tool_calls(self):
        return bool(self.tool_calls)


def _fake_base_module():
    return types.SimpleNamespace(VLMResponse=_FakeVLMResponse)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class DetectionTests(unittest.TestCase):
    def test_tools_list_names_the_update_tool(self):
        self.assertTrue(wm_patch.is_wm_update_call([UPDATE_TOOL]))
        self.assertTrue(wm_patch.is_wm_update_call([CREATE_TOOL, UPDATE_TOOL]))

    def test_other_tools_and_no_tools_pass(self):
        self.assertFalse(wm_patch.is_wm_update_call([CREATE_TOOL]))
        self.assertFalse(wm_patch.is_wm_update_call(None))
        self.assertFalse(wm_patch.is_wm_update_call([]))
        self.assertFalse(wm_patch.is_wm_update_call(["not a dict", {"function": "x"}]))

    def test_tool_choice_dict_alone_is_enough(self):
        self.assertTrue(wm_patch.is_wm_update_call(None, CHOICE))
        self.assertFalse(wm_patch.is_wm_update_call(None, "auto"))
        self.assertFalse(wm_patch.is_wm_update_call(None, {"type": "function"}))


class WrapperTests(unittest.TestCase):
    def setUp(self):
        self.calls = []

        async def original(self_, prompt="", *args, **kwargs):
            self.calls.append((prompt, args, kwargs))
            return "model text"

        self.original = original
        self.wrapped = wm_patch.skipping_wm_update(original)
        self.vlm = object()

    def test_update_call_is_answered_locally(self):
        err = io.StringIO()
        with (
            patch.dict(
                sys.modules, {"openviking.models.vlm.base": _fake_base_module()}
            ),
            redirect_stderr(err),
        ):
            resp = _run(
                self.wrapped(
                    self.vlm, "prompt", tools=[UPDATE_TOOL], tool_choice=CHOICE
                )
            )
        self.assertEqual(self.calls, [])
        self.assertIsInstance(resp, _FakeVLMResponse)
        self.assertFalse(resp.has_tool_calls)
        self.assertEqual(resp.tool_calls, [])
        self.assertEqual(resp.finish_reason, "stop")
        line = err.getvalue().strip()
        self.assertTrue(line.startswith("ov-wm-update-skip {"), line)
        self.assertIn('"prompt_chars": 6', line)
        self.assertIn('"tool": "update_working_memory"', line)

    def test_positional_tools_are_detected(self):
        with patch.dict(
            sys.modules, {"openviking.models.vlm.base": _fake_base_module()}
        ):
            with redirect_stderr(io.StringIO()):
                resp = _run(self.wrapped(self.vlm, "p", None, [UPDATE_TOOL], CHOICE))
        self.assertEqual(self.calls, [])
        self.assertFalse(resp.has_tool_calls)

    def test_other_calls_pass_through_with_arguments_intact(self):
        resp = _run(self.wrapped(self.vlm, "plain prompt"))
        self.assertEqual(resp, "model text")
        resp = _run(
            self.wrapped(
                self.vlm,
                "create",
                tools=[CREATE_TOOL],
                tool_choice="auto",
                max_tokens=5,
            )
        )
        self.assertEqual(resp, "model text")
        self.assertEqual(
            self.calls,
            [
                ("plain prompt", (), {}),
                (
                    "create",
                    (),
                    {"tools": [CREATE_TOOL], "tool_choice": "auto", "max_tokens": 5},
                ),
            ],
        )

    def test_original_errors_propagate(self):
        async def fail(self_, prompt="", *args, **kwargs):
            raise RuntimeError("upstream down")

        with self.assertRaisesRegex(RuntimeError, "upstream down"):
            _run(wm_patch.skipping_wm_update(fail)(self.vlm, "p"))


class GuardTests(unittest.TestCase):
    def test_guards_and_idempotence(self):
        async def original(self, prompt="", *args, **kwargs):
            return "text"

        cls = type("OpenAIVLM", (), {"get_completion_async": original})
        module = types.SimpleNamespace(OpenAIVLM=cls)
        ov = types.SimpleNamespace(__version__="v0.4.99")
        with (
            patch.dict(sys.modules, {"openviking": ov}),
            patch.dict(os.environ, {}, clear=True),
        ):
            self.assertFalse(wm_patch.apply(module))
            ov.__version__ = wm_patch.EXPECTED_VERSION
            self.assertFalse(wm_patch.apply(module))
            with patch.object(
                wm_patch,
                "EXPECTED_SHA256",
                wm_patch.hashlib.sha256(
                    wm_patch.inspect.getsource(original).encode()
                ).hexdigest(),
            ):
                with patch.dict(os.environ, {"OV_WM_UPDATE_SKIP": "0"}):
                    self.assertFalse(wm_patch.apply(module))
                self.assertTrue(wm_patch.apply(module))
                installed = cls.get_completion_async
                self.assertTrue(wm_patch.apply(module))
                self.assertIs(cls.get_completion_async, installed)
                self.assertTrue(getattr(installed, "_ov_wm_update_patch", False))

    def test_missing_class_is_not_applied(self):
        ov = types.SimpleNamespace(__version__=wm_patch.EXPECTED_VERSION)
        with patch.dict(sys.modules, {"openviking": ov}):
            self.assertFalse(wm_patch.apply(types.SimpleNamespace()))


if __name__ == "__main__":
    unittest.main()
