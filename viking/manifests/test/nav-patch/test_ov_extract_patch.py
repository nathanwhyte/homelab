"""Tests for ov_extract_patch. Pure (fakes only) — runs anywhere:

    python3 test_ov_extract_patch.py

The version/source-hash guard is exercised against the INSTALLED openviking by piping
ov_extract_patch.py into a throwaway interpreter in the pod (see the BUG-1176 PR);
this file covers the wrapper semantics.
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ov_extract_patch as ep  # noqa: E402


class Ops:
    def __init__(self, upserts=(), errors=(), deletes=()):
        self.upsert_operations = list(upserts)
        self.errors = list(errors)
        self.delete_file_contents = list(deletes)

    def has_errors(self):
        return bool(self.errors)


class Loop:
    _last_llm_failure_kind = None


def make(result):
    async def orig(self):
        return result

    return ep.wrap_run(orig)


class WrapRunTests(unittest.TestCase):
    def run_wrapped(self, result, kind=None):
        loop = Loop()
        loop._last_llm_failure_kind = kind
        return asyncio.run(make(result)(loop))

    def test_errors_only_raises_degraded(self):
        ops = Ops(
            errors=[
                "Final response could not be parsed as operations after 3 iterations (failure_kind=empty_response)"
            ]
        )
        with self.assertRaises(ep.ExtractionDegradedError) as cm:
            self.run_wrapped((ops, []), kind="empty_response")
        self.assertIn("failure_kind=empty_response", str(cm.exception))

    def test_upserts_pass_through_even_with_errors(self):
        ops = Ops(upserts=["op"], errors=["some warning"])
        got, tools = self.run_wrapped((ops, ["t"]))
        self.assertIs(got, ops)
        self.assertEqual(tools, ["t"])

    def test_none_passes_through(self):
        got, tools = self.run_wrapped((None, []))
        self.assertIsNone(got)

    def test_zero_ops_without_errors_passes_through_with_warning(self):
        ops = Ops()
        with self.assertLogs("ov_extract_patch", level="WARNING") as logs:
            got, _ = self.run_wrapped((ops, []), kind="parse_error")
        self.assertIs(got, ops)
        self.assertTrue(any("0 upsert operations" in m for m in logs.output))

    def test_wrapper_is_marked_and_idempotent_marker(self):
        w = make((None, []))
        self.assertTrue(getattr(w, "_ov_extract_patch", False))


class RetryClassifierTests(unittest.TestCase):
    def test_degraded_is_retryable_and_others_delegate(self):
        calls = []

        def orig(err):
            calls.append(err)
            return False

        f = ep.wrap_is_retryable(orig)
        self.assertTrue(f(ep.ExtractionDegradedError("x")))
        self.assertFalse(f(ValueError("y")))
        self.assertEqual(len(calls), 1)

    def test_retry_budget_defaults_and_overrides(self):
        for k in (
            "OV_EXTRACT_PATCH_RETRIES",
            "OV_EXTRACT_PATCH_BASE_DELAY",
            "OV_EXTRACT_PATCH_MAX_DELAY",
        ):
            os.environ.pop(k, None)
        self.assertEqual(ep.retry_budget(), (5, 15.0, 120.0))
        os.environ["OV_EXTRACT_PATCH_RETRIES"] = "2"
        os.environ["OV_EXTRACT_PATCH_BASE_DELAY"] = "bogus"
        os.environ["OV_EXTRACT_PATCH_MAX_DELAY"] = "-1"
        try:
            self.assertEqual(ep.retry_budget(), (2, 15.0, 120.0))
        finally:
            for k in (
                "OV_EXTRACT_PATCH_RETRIES",
                "OV_EXTRACT_PATCH_BASE_DELAY",
                "OV_EXTRACT_PATCH_MAX_DELAY",
            ):
                os.environ.pop(k, None)


class ApplyGuardTests(unittest.TestCase):
    def test_apply_session_refuses_wrong_version(self):
        import types

        fake_openviking = types.ModuleType("openviking")
        fake_openviking.__version__ = "v0.4.99"
        sys.modules["openviking"] = fake_openviking
        try:
            mod = types.SimpleNamespace(
                is_retryable_api_error=lambda e: False,
                _MEMORY_EXTRACTION_MAX_RETRIES=3,
                _MEMORY_EXTRACTION_RETRY_BASE_DELAY_SECONDS=1.0,
                _MEMORY_EXTRACTION_RETRY_MAX_DELAY_SECONDS=8.0,
            )
            self.assertFalse(ep.apply_session(mod))
            self.assertEqual(mod._MEMORY_EXTRACTION_MAX_RETRIES, 3)
        finally:
            del sys.modules["openviking"]

    def test_apply_session_applies_on_expected_version(self):
        import types

        fake_openviking = types.ModuleType("openviking")
        fake_openviking.__version__ = ep.EXPECTED_VERSION
        sys.modules["openviking"] = fake_openviking
        try:
            mod = types.SimpleNamespace(
                is_retryable_api_error=lambda e: False,
                _MEMORY_EXTRACTION_MAX_RETRIES=3,
                _MEMORY_EXTRACTION_RETRY_BASE_DELAY_SECONDS=1.0,
                _MEMORY_EXTRACTION_RETRY_MAX_DELAY_SECONDS=8.0,
            )
            self.assertTrue(ep.apply_session(mod))
            self.assertEqual(mod._MEMORY_EXTRACTION_MAX_RETRIES, 5)
            self.assertTrue(mod.is_retryable_api_error(ep.ExtractionDegradedError("x")))
            self.assertTrue(ep.apply_session(mod))  # idempotent
        finally:
            del sys.modules["openviking"]

    def test_disabled_by_env(self):
        os.environ["OV_EXTRACT_PATCH"] = "0"
        try:
            self.assertFalse(ep.apply_session(object()))
        finally:
            os.environ.pop("OV_EXTRACT_PATCH", None)


if __name__ == "__main__":
    unittest.main()
