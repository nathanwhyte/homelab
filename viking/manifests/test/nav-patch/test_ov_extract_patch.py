"""Tests for ov_extract_patch. The wrapper tests use fakes only and run anywhere:

    python3 test_ov_extract_patch.py

``RealTargetTests`` and ``Phase2HarnessTests`` run only where openviking
``EXPECTED_VERSION`` is installed (the image). They apply the guards to the real targets
and drive the real ``Session._run_memory_extraction``, ``ExtractLoop.run``,
``StreamingMemoryUpdater`` and ``retry_async`` in a clean ``python -I`` subprocess, so
sitecustomize and the other patches stay out. ``OV_EXTRACT_PATCH=0`` in the environment
reaches that subprocess and shows stock behaviour.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "_scripts" / "lib"))
from git_env import scrub_environ  # noqa: E402

scrub_environ()

import asyncio
import contextlib
import contextvars
import dataclasses
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import types
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


class SessionExtractContextProvider:  # name is what the wrapper keys on
    pass


class PatchMergeContextProvider(SessionExtractContextProvider):
    pass


class Loop:
    _last_llm_failure_kind = None
    context_provider = SessionExtractContextProvider()


class MergeLoop(Loop):
    context_provider = PatchMergeContextProvider()


def make(result):
    async def orig(self):
        return result

    return ep.wrap_run(orig)


def parse_exhausted():
    """The RuntimeError v0.5.1.1 ``ExtractLoop.run`` raises (extract_loop.py:440-444)."""
    return RuntimeError(
        "Final response could not be parsed as operations after 4 iterations "
        "(failure_kind=empty_response)"
    )


def raising_run(exc):
    async def orig(self):
        raise exc

    return ep.wrap_run(orig)


class WrapRunTests(unittest.TestCase):
    def run_wrapped(self, result, kind=None, loop_cls=Loop):
        loop = loop_cls()
        loop._last_llm_failure_kind = kind
        return asyncio.run(make(result)(loop))

    def test_session_parse_exhaustion_becomes_degraded(self):
        exc = parse_exhausted()
        with (
            self.assertLogs("ov_extract_patch", level="WARNING"),
            self.assertRaises(ep.ExtractionDegradedError) as cm,
        ):
            asyncio.run(raising_run(exc)(Loop()))
        self.assertIn("failure_kind=empty_response", str(cm.exception))
        self.assertIn("could not be parsed", str(cm.exception))
        self.assertIs(cm.exception.__cause__, exc)
        self.assertEqual(ep.failure_class(cm.exception), "degraded")

    def test_merge_loop_parse_exhaustion_is_reraised_unchanged(self):
        # Merge loops run after append-only events are written; a whole-step retry
        # there would regenerate and duplicate them, so stock (not retried) stays.
        exc = parse_exhausted()
        with self.assertRaises(RuntimeError) as cm:
            asyncio.run(raising_run(exc)(MergeLoop()))
        self.assertIs(cm.exception, exc)

    def test_session_parse_exhaustion_after_a_write_is_reraised_unchanged(self):
        exc = parse_exhausted()
        token = ep._STEP.set({"armed": True, "wrote": True})
        try:
            with self.assertRaises(RuntimeError) as cm:
                asyncio.run(raising_run(exc)(Loop()))
        finally:
            ep._STEP.reset(token)
        self.assertIs(cm.exception, exc)

    def test_other_runtime_errors_are_reraised_unchanged(self):
        exc = RuntimeError("Reached 3 iterations without completion")
        with self.assertRaises(RuntimeError) as cm:
            asyncio.run(raising_run(exc)(Loop()))
        self.assertIs(cm.exception, exc)

    def test_errors_only_result_is_returned(self):
        # v0.4.20 returned parse exhaustion as an errors-only result; v0.5.1.1 raises
        # instead, so a returned result is passed through as upstream built it.
        ops = Ops(errors=["something"])
        got, _ = self.run_wrapped((ops, []))
        self.assertIs(got, ops)

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
    def setUp(self):
        ep._state["loop_applied"] = True

    def tearDown(self):
        ep._state["loop_applied"] = False

    def test_apply_session_refuses_without_loop_half(self):
        import types

        ep._state["loop_applied"] = False
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
            self.assertFalse(ep.apply_session(mod))
            self.assertEqual(mod._MEMORY_EXTRACTION_MAX_RETRIES, 3)
        finally:
            del sys.modules["openviking"]

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


# --- BUG-1196 ----------------------------------------------------------------------------

ENV_KEYS = (
    "OV_EXTRACT_PATCH_TIMEOUT_RETRIES",
    "OV_EXTRACT_PATCH_LOCK_RETRIES",
    "OV_EXTRACT_PATCH_BATCH_MESSAGES",
    "OV_EXTRACT_PATCH_BATCH_TOKENS",
    "OV_EXTRACT_PATCH_CHUNK",
)


class EnvMixin:
    def setUp(self):
        self._saved = {k: os.environ.pop(k, None) for k in ENV_KEYS}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class APITimeoutError(Exception):  # name-alike of openai.APITimeoutError
    def __init__(self):
        super().__init__("Request timed out.")


class ReadTimeout(Exception):  # name-alike of httpx.ReadTimeout
    pass


class LockError(Exception):
    pass


class LockAcquisitionError(LockError):  # name-alike of openviking.storage.errors
    pass


def lock_error():
    return LockAcquisitionError("lock acquire timed out after 300000ms")


async def fake_retry_async(
    func,
    *,
    max_retries,
    base_delay=0.5,
    max_delay=8.0,
    jitter=True,
    is_retryable=lambda e: False,
    logger=None,
    operation_name="operation",
    model_errors_only=False,
):
    """Upstream v0.5.1.1 ``retry_async`` control flow, without the sleep.

    As upstream's ``_retry_limit`` (model_retry.py:432-451) does for a non-model error,
    it asks ``is_retryable`` on every failure, the last one included. The model-error
    branch, which skips ``is_retryable``, is covered against the real function in
    ``RealTargetTests``.
    """
    attempt = 0
    while True:
        try:
            return await func()
        except Exception as e:
            retry_limit = max_retries if is_retryable(e) else 0
            if attempt >= retry_limit:
                raise
            attempt += 1


def scripted(*outcomes):
    """An async step that raises/returns the scripted outcomes in order."""
    calls = []

    async def step():
        calls.append(1)
        item = outcomes[min(len(calls), len(outcomes)) - 1]
        if isinstance(item, BaseException):
            raise item
        return item

    return step, calls


def tagged(exc):
    setattr(exc, ep.STAGE_ATTR, ep.STAGE_SESSION_EXTRACT)
    return exc


def before_write(exc):
    setattr(exc, ep.STAGE_ATTR, ep.STAGE_BEFORE_WRITE)
    return exc


def stock_like(error):
    """A stock classifier that retries the text "timeout" but not "timed out" (v0.4.20)."""
    return "timeout" in str(error).lower()


def stock_v0511(error):
    """The slice of v0.5.1.1 ``is_retryable_api_error`` these tests need.

    #6102 added "timed out" to the transient patterns (model_retry.py:186), so it also
    retries "lock acquire timed out after …ms".
    """
    text = str(error).lower()
    return "timeout" in text or "timed out" in text


class FailureClassTests(unittest.TestCase):
    def test_classes(self):
        self.assertEqual(ep.failure_class(ep.ExtractionDegradedError("x")), "degraded")
        self.assertEqual(ep.failure_class(lock_error()), "lock_wait")
        self.assertEqual(
            ep.failure_class(RuntimeError("lock acquire timed out after 300000ms")),
            "lock_wait",
        )
        self.assertEqual(ep.failure_class(APITimeoutError()), "timeout")
        self.assertEqual(ep.failure_class(ReadTimeout("")), "timeout")
        self.assertEqual(ep.failure_class(TimeoutError()), "timeout")
        self.assertEqual(
            ep.failure_class(RuntimeError("Request timed out.")), "timeout"
        )
        self.assertEqual(ep.failure_class(ValueError("400 bad request")), "other")

    def test_chained_cause_is_followed(self):
        outer = RuntimeError("phase 2 step failed")
        outer.__cause__ = APITimeoutError()
        self.assertEqual(ep.failure_class(outer), "timeout")
        outer2 = RuntimeError("apply failed")
        outer2.__context__ = lock_error()
        self.assertEqual(ep.failure_class(outer2), "lock_wait")

    def test_tag_required_for_session_extract_timeout(self):
        self.assertFalse(ep.is_session_extract_timeout(APITimeoutError()))
        self.assertTrue(ep.is_session_extract_timeout(tagged(APITimeoutError())))
        self.assertFalse(ep.is_session_extract_timeout(tagged(lock_error())))


class WrapRunTimeoutTagTests(unittest.TestCase):
    def raising(self, exc):
        async def orig(self):
            raise exc

        return ep.wrap_run(orig)

    def test_session_loop_timeout_is_tagged_and_reraised(self):
        exc = APITimeoutError()
        with self.assertRaises(APITimeoutError) as cm:
            asyncio.run(self.raising(exc)(Loop()))
        self.assertIs(cm.exception, exc)
        self.assertTrue(ep.is_session_extract_timeout(exc))

    def test_merge_loop_timeout_is_not_tagged(self):
        exc = APITimeoutError()
        with self.assertRaises(APITimeoutError):
            asyncio.run(self.raising(exc)(MergeLoop()))
        self.assertFalse(ep.is_session_extract_timeout(exc))

    def test_non_timeout_is_not_tagged(self):
        exc = ValueError("boom")
        with self.assertRaises(ValueError):
            asyncio.run(self.raising(exc)(Loop()))
        self.assertIsNone(getattr(exc, ep.STAGE_ATTR, None))


class RetryWrapperTests(EnvMixin, unittest.TestCase):
    def run_step(self, step, base=lambda e: False, max_retries=5):
        wrapped = ep.wrap_retry_async(fake_retry_async, base)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            try:
                result = asyncio.run(
                    wrapped(
                        step,
                        max_retries=max_retries,
                        base_delay=0,
                        max_delay=0,
                        is_retryable=base,
                        operation_name="long_term_memory_extraction",
                    )
                )
                exc = None
            except Exception as e:  # noqa: BLE001
                result, exc = None, e
        records = [
            json.loads(line[len(ep.OUTCOME_PREFIX) :])
            for line in err.getvalue().splitlines()
            if line.startswith(ep.OUTCOME_PREFIX)
        ]
        return result, exc, records

    def test_tagged_timeout_retried_to_budget_then_fails_visibly(self):
        step, calls = scripted(tagged(APITimeoutError()))
        _, exc, records = self.run_step(step)
        self.assertIsInstance(exc, APITimeoutError)
        self.assertEqual(len(calls), 3)  # 1 + DEFAULT_TIMEOUT_RETRIES
        self.assertEqual([r["event"] for r in records], ["retry", "retry", "failed"])
        self.assertEqual(records[-1]["failure_class"], "timeout")
        self.assertEqual(records[-1]["attempts"], 3)
        self.assertEqual(records[-1]["step"], "long_term_memory_extraction")

    def test_untagged_timeout_keeps_stock_behaviour(self):
        step, calls = scripted(APITimeoutError())
        _, exc, records = self.run_step(step)
        self.assertIsInstance(exc, APITimeoutError)
        self.assertEqual(len(calls), 1)
        self.assertEqual([r["event"] for r in records], ["failed"])

    def test_before_write_lock_wait_retried_on_own_budget_and_named(self):
        # Stock v0.5.1.1 would retry it up to max_retries; the lock budget still caps it.
        for base in (lambda e: False, stock_v0511):
            step, calls = scripted(before_write(lock_error()))
            _, exc, records = self.run_step(step, base=base)
            self.assertIsInstance(exc, LockAcquisitionError)
            self.assertEqual(len(calls), 4)  # 1 + DEFAULT_LOCK_RETRIES
            self.assertEqual(records[-1]["failure_class"], "lock_wait")
            self.assertEqual(
                [r["event"] for r in records], ["retry", "retry", "retry", "failed"]
            )

    def test_untagged_lock_wait_is_not_retried_although_stock_would(self):
        # A lock wait after the attempt wrote: a retry would re-extract with a new
        # source_extraction_id and duplicate the written events. v0.5.1.1's classifier
        # retries the text "timed out", so the wrapper has to refuse it.
        for exc_ in (lock_error(), RuntimeError("lock acquire timed out after 110ms")):
            self.assertTrue(stock_v0511(exc_))
            step, calls = scripted(exc_)
            _, exc, records = self.run_step(step, base=stock_v0511)
            self.assertIs(exc, exc_)
            self.assertEqual(len(calls), 1)
            self.assertEqual([r["event"] for r in records], ["failed"])
            self.assertEqual(records[-1]["failure_class"], "lock_wait")

    def test_final_failure_spends_no_budget_and_emits_no_retry(self):
        # v0.5.1.1 asks the classifier on the last failure too; that answer is moot.
        step, calls = scripted(before_write(lock_error()))
        _, exc, records = self.run_step(step, max_retries=1)
        self.assertEqual(len(calls), 2)
        self.assertEqual([r["event"] for r in records], ["retry", "failed"])

    def test_stock_retried_timeout_keeps_stock_retries(self):
        # "504 Gateway Timeout" is retried by stock; the class budget must add to that,
        # never replace it, at the default budget and with the class switched off.
        for budget in (None, "0"):
            if budget is not None:
                os.environ["OV_EXTRACT_PATCH_TIMEOUT_RETRIES"] = budget
            for exc in (
                RuntimeError("Error code: 504 - {'error': 'Gateway Timeout'}"),
                RuntimeError("read timeout"),
            ):
                self.assertEqual(ep.failure_class(exc), "timeout")
                step, calls = scripted(tagged(exc))
                _, got, _ = self.run_step(step, base=stock_like, max_retries=5)
                self.assertIs(got, exc)
                self.assertEqual(len(calls), 6, (budget, str(exc)))

    def test_class_budget_counts_only_retries_stock_refused(self):
        # Two stock-retried timeouts, then "Request timed out." twice: the budget of 2
        # is still whole when the untransient-looking ones arrive.
        step, calls = scripted(
            tagged(RuntimeError("upstream timeout")),
            tagged(RuntimeError("upstream timeout")),
            tagged(APITimeoutError()),
            tagged(APITimeoutError()),
            tagged(APITimeoutError()),
        )
        _, exc, _ = self.run_step(step, base=stock_like, max_retries=10)
        self.assertIsInstance(exc, APITimeoutError)
        self.assertEqual(len(calls), 5)  # 2 stock + 2 budget + the final failure

    def test_recovery_after_timeout_is_reported(self):
        step, _ = scripted(tagged(APITimeoutError()), {"contexts": []})
        result, exc, records = self.run_step(step)
        self.assertIsNone(exc)
        self.assertEqual(result, {"contexts": []})
        self.assertEqual([r["event"] for r in records], ["retry", "recovered"])
        self.assertEqual(records[-1]["attempts"], 2)

    def test_first_try_success_emits_nothing(self):
        step, _ = scripted("ok")
        result, _, records = self.run_step(step)
        self.assertEqual(result, "ok")
        self.assertEqual(records, [])

    def test_zero_budget_disables_a_class(self):
        os.environ["OV_EXTRACT_PATCH_TIMEOUT_RETRIES"] = "0"
        os.environ["OV_EXTRACT_PATCH_LOCK_RETRIES"] = "0"
        for exc in (tagged(APITimeoutError()), before_write(lock_error())):
            step, calls = scripted(exc)
            self.run_step(step)
            self.assertEqual(len(calls), 1)

    def test_overall_cap_still_applies(self):
        os.environ["OV_EXTRACT_PATCH_LOCK_RETRIES"] = "10"
        step, calls = scripted(before_write(lock_error()))
        self.run_step(step, max_retries=2)
        self.assertEqual(len(calls), 3)

    def test_budgets_are_per_call_not_shared(self):
        wrapped = ep.wrap_retry_async(fake_retry_async, lambda e: False)
        for _ in range(2):
            step, calls = scripted(tagged(APITimeoutError()))
            with (
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(APITimeoutError),
            ):
                asyncio.run(wrapped(step, max_retries=5))
            self.assertEqual(len(calls), 3)

    def test_other_classes_defer_to_caller_classifier(self):
        seen = []

        def base(e):
            seen.append(e)
            return isinstance(e, ep.ExtractionDegradedError)

        step, _ = scripted(ep.ExtractionDegradedError("x"), "ok")
        result, _, _ = self.run_step(step, base=base)
        self.assertEqual(result, "ok")
        self.assertEqual(len(seen), 1)
        step, _ = scripted(ValueError("400"))
        _, exc, records = self.run_step(step, base=base)
        self.assertIsInstance(exc, ValueError)
        self.assertEqual(records[-1]["failure_class"], "other")


class FakeUpdater:
    """The v0.5.1.1 ``StreamingMemoryUpdater.submit`` write order, with scripted leases.

    ``submit`` applies the append-only part first (lease, then write), then the merge
    groups. Merge groups flush in a batcher task whose context is not the submitter's,
    as upstream's ``StreamingBatcher`` does. ``leases`` scripts each lease acquisition
    in order: ``"ok"`` or ``"lock"`` (raises a lock wait). ``writes`` records each write.
    """

    def __init__(self, leases):
        self.leases = list(leases)
        self.writes = []
        self.acquire = ep.wrap_acquire_lease(self._acquire)

    async def _acquire(self, kind):
        outcome = self.leases.pop(0) if self.leases else "ok"
        if outcome == "lock":
            raise lock_error()
        return kind

    async def _apply(self, kind):
        await self.acquire(kind)
        self.writes.append(kind)

    async def _apply_append_only_request_now(self, request):
        await self._apply("append")

    async def _submit_grouped_merge_request(self, request):
        loop = asyncio.get_running_loop()
        await loop.create_task(self._apply("merge"), context=contextvars.Context())

    async def submit(self, append=True, merge=True):
        if append:
            await self._apply_append_only_request_now(None)
        if merge:
            await self._submit_grouped_merge_request(None)


FakeUpdater._apply_append_only_request_now = ep.wrap_append_only(
    FakeUpdater._apply_append_only_request_now
)
FakeUpdater._submit_grouped_merge_request = ep.wrap_grouped_merge(
    FakeUpdater._submit_grouped_merge_request
)


class WriteTrackingTests(EnvMixin, unittest.TestCase):
    """End to end: wrapped extraction loop + fake updater under the retry wrapper."""

    def run_steps(self, updater, submits, extract=True):
        """Run one Phase 2 step: session extraction, then each ``submit`` in order.

        The caller's classifier is v0.5.1.1's, which would retry every lock wait.
        """
        calls = []
        run = ep.wrap_run(lambda self: _ops_result())

        async def step():
            calls.append(1)
            if extract:
                await run(Loop())
            for kwargs in submits:
                await updater.submit(**kwargs)
            return "ok"

        wrapped = ep.wrap_retry_async(fake_retry_async, stock_v0511)
        with contextlib.redirect_stderr(io.StringIO()):
            try:
                result = asyncio.run(wrapped(step, max_retries=5))
                exc = None
            except Exception as e:  # noqa: BLE001
                result, exc = None, e
        return result, exc, calls

    def test_lease_wait_before_any_write_is_retried(self):
        updater = FakeUpdater(["lock", "lock", "ok", "ok"])
        result, exc, calls = self.run_steps(updater, [{}])
        self.assertIsNone(exc)
        self.assertEqual(result, "ok")
        self.assertEqual(len(calls), 3)
        self.assertEqual(updater.writes, ["append", "merge"])

    def test_lock_wait_after_append_only_write_is_not_retried(self):
        # The review finding: events are written, then the merge lease waits out.
        # A retry would re-extract and file the same events again at <name>_2.md.
        updater = FakeUpdater(["ok", "lock"])
        _, exc, calls = self.run_steps(updater, [{}])
        self.assertIsInstance(exc, LockAcquisitionError)
        self.assertEqual(len(calls), 1)
        self.assertEqual(updater.writes, ["append"])

    def test_lock_wait_in_a_later_submit_is_not_retried(self):
        updater = FakeUpdater(["ok", "lock"])
        _, exc, calls = self.run_steps(updater, [{"merge": False}, {"merge": False}])
        self.assertIsInstance(exc, LockAcquisitionError)
        self.assertEqual(len(calls), 1)
        self.assertEqual(updater.writes, ["append"])

    def test_lock_wait_after_merge_only_submit_is_not_retried(self):
        # The merge flush runs in a foreign context; the submitter still records it.
        updater = FakeUpdater(["ok", "lock"])
        _, exc, calls = self.run_steps(updater, [{"append": False}, {"merge": False}])
        self.assertIsInstance(exc, LockAcquisitionError)
        self.assertEqual(len(calls), 1)
        self.assertEqual(updater.writes, ["merge"])

    def test_lease_wait_without_session_extraction_is_not_retried(self):
        updater = FakeUpdater(["lock"])
        _, exc, calls = self.run_steps(updater, [{}], extract=False)
        self.assertIsInstance(exc, LockAcquisitionError)
        self.assertEqual(len(calls), 1)

    def test_each_attempt_starts_unwritten(self):
        # Attempt 1 writes nothing (lease wait); attempt 2 again waits on its first
        # lease and is still retried; attempt 3 succeeds.
        updater = FakeUpdater(["lock", "lock", "ok", "ok"])
        _, exc, calls = self.run_steps(updater, [{"merge": False}])
        self.assertIsNone(exc)
        self.assertEqual(len(calls), 3)

    def test_timeout_in_session_extraction_after_a_write_is_not_tagged(self):
        updater = FakeUpdater([])

        async def raising(self):
            raise APITimeoutError()

        run = ep.wrap_run(raising)
        seen = []

        async def step():
            await updater.submit(merge=False)
            try:
                await run(Loop())
            except APITimeoutError as e:
                seen.append(ep.is_session_extract_timeout(e))
                raise

        wrapped = ep.wrap_retry_async(fake_retry_async, lambda e: False)
        with (
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(APITimeoutError),
        ):
            asyncio.run(wrapped(step, max_retries=5))
        self.assertEqual(seen, [False])


async def _ops_result():
    return Ops(upserts=["op"]), []


class StreamingInstallTests(EnvMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        import types

        fake_openviking = types.ModuleType("openviking")
        fake_openviking.__version__ = ep.EXPECTED_VERSION
        sys.modules["openviking"] = fake_openviking

        class Updater:
            async def _apply_append_only_request_now(self, request):
                return request

            async def _submit_grouped_merge_request(self, request):
                return request

        async def acquire_memory_operation_lease(operations, viking_fs, ctx):
            return None

        self.cls = Updater
        self.mod = types.SimpleNamespace(
            StreamingMemoryUpdater=Updater,
            acquire_memory_operation_lease=acquire_memory_operation_lease,
        )
        self._hashes = (
            ep.EXPECTED_APPEND_ONLY_SHA256,
            ep.EXPECTED_GROUPED_MERGE_SHA256,
            ep.EXPECTED_ACQUIRE_LEASE_SHA256,
        )

    def tearDown(self):
        (
            ep.EXPECTED_APPEND_ONLY_SHA256,
            ep.EXPECTED_GROUPED_MERGE_SHA256,
            ep.EXPECTED_ACQUIRE_LEASE_SHA256,
        ) = self._hashes
        sys.modules.pop("openviking", None)
        super().tearDown()

    def match_hashes(self):
        ep.EXPECTED_APPEND_ONLY_SHA256 = ep._source_hash(
            self.cls._apply_append_only_request_now
        )
        ep.EXPECTED_GROUPED_MERGE_SHA256 = ep._source_hash(
            self.cls._submit_grouped_merge_request
        )
        ep.EXPECTED_ACQUIRE_LEASE_SHA256 = ep._source_hash(
            self.mod.acquire_memory_operation_lease
        )

    def test_hash_mismatch_installs_nothing(self):
        orig = self.mod.acquire_memory_operation_lease
        self.assertFalse(ep.apply_streaming(self.mod))
        self.assertIs(self.mod.acquire_memory_operation_lease, orig)
        self.assertFalse(
            getattr(self.cls._apply_append_only_request_now, "_ov_extract_patch", False)
        )

    def test_one_mismatch_installs_nothing(self):
        self.match_hashes()
        ep.EXPECTED_GROUPED_MERGE_SHA256 = "0" * 64
        self.assertFalse(ep.apply_streaming(self.mod))
        self.assertFalse(
            getattr(self.cls._apply_append_only_request_now, "_ov_extract_patch", False)
        )

    def test_hash_match_installs_all_three_idempotently(self):
        self.match_hashes()
        self.assertTrue(ep.apply_streaming(self.mod))
        for fn in (
            self.cls._apply_append_only_request_now,
            self.cls._submit_grouped_merge_request,
            self.mod.acquire_memory_operation_lease,
        ):
            self.assertTrue(getattr(fn, "_ov_extract_patch", False))
        wrapped = self.cls._apply_append_only_request_now
        self.assertTrue(ep.apply_streaming(self.mod))
        self.assertIs(self.cls._apply_append_only_request_now, wrapped)

    def test_wrong_version_installs_nothing(self):
        self.match_hashes()
        sys.modules["openviking"].__version__ = "v0.4.99"
        self.assertFalse(ep.apply_streaming(self.mod))


@dataclasses.dataclass(frozen=True)
class Limits:  # shape of openviking.session.extraction_batch.ExtractionBatchLimits
    max_message_tokens: int | None = None
    max_messages: int | None = None


def resolve_stock(policy):
    if not isinstance(policy, dict) or not policy:
        return Limits()
    return Limits(
        max_message_tokens=policy.get("pending_token_threshold") or None,
        max_messages=policy.get("message_count_threshold") or None,
    )


class ChunkingTests(EnvMixin, unittest.TestCase):
    def resolve(self, policy):
        return ep.wrap_resolve_limits(resolve_stock, Limits)(policy)

    def test_session_without_policy_gets_default_caps(self):
        self.assertEqual(
            self.resolve({}),
            Limits(
                max_message_tokens=ep.DEFAULT_BATCH_TOKENS,
                max_messages=ep.DEFAULT_BATCH_MESSAGES,
            ),
        )

    def test_policy_tighter_limit_is_kept_looser_is_capped(self):
        got = self.resolve(
            {"pending_token_threshold": 150000, "message_count_threshold": 20}
        )
        self.assertEqual(
            got, Limits(max_message_tokens=ep.DEFAULT_BATCH_TOKENS, max_messages=20)
        )

    def test_zero_drops_one_cap(self):
        os.environ["OV_EXTRACT_PATCH_BATCH_TOKENS"] = "0"
        os.environ["OV_EXTRACT_PATCH_BATCH_MESSAGES"] = "40"
        self.assertEqual(
            self.resolve(None), Limits(max_message_tokens=None, max_messages=40)
        )

    def test_chunk_off_returns_stock(self):
        os.environ["OV_EXTRACT_PATCH_CHUNK"] = "0"
        self.assertEqual(self.resolve({}), Limits())


class SessionInstallTests(EnvMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        import types

        ep._state["loop_applied"] = True
        fake_openviking = types.ModuleType("openviking")
        fake_openviking.__version__ = ep.EXPECTED_VERSION
        sys.modules["openviking"] = fake_openviking
        self.mod = types.SimpleNamespace(
            is_retryable_api_error=lambda e: False,
            retry_async=fake_retry_async,
            resolve_extraction_batch_limits=resolve_stock,
            ExtractionBatchLimits=Limits,
            _MEMORY_EXTRACTION_MAX_RETRIES=3,
            _MEMORY_EXTRACTION_RETRY_BASE_DELAY_SECONDS=1.0,
            _MEMORY_EXTRACTION_RETRY_MAX_DELAY_SECONDS=8.0,
        )
        self._hashes = (
            ep.EXPECTED_RETRY_ASYNC_SHA256,
            ep.EXPECTED_RESOLVE_LIMITS_SHA256,
        )

    def tearDown(self):
        ep.EXPECTED_RETRY_ASYNC_SHA256, ep.EXPECTED_RESOLVE_LIMITS_SHA256 = self._hashes
        ep._state["loop_applied"] = False
        sys.modules.pop("openviking", None)
        super().tearDown()

    def test_hash_mismatch_leaves_retry_and_batching_stock(self):
        self.assertTrue(ep.apply_session(self.mod))
        self.assertIs(self.mod.retry_async, fake_retry_async)
        self.assertIs(self.mod.resolve_extraction_batch_limits, resolve_stock)
        self.assertEqual(self.mod._MEMORY_EXTRACTION_MAX_RETRIES, 5)

    def test_hash_match_installs_both(self):
        ep.EXPECTED_RETRY_ASYNC_SHA256 = ep._source_hash(fake_retry_async)
        ep.EXPECTED_RESOLVE_LIMITS_SHA256 = ep._source_hash(resolve_stock)
        self.assertTrue(ep.apply_session(self.mod))
        self.assertTrue(getattr(self.mod.retry_async, "_ov_extract_patch", False))
        self.assertEqual(
            self.mod.resolve_extraction_batch_limits(None).max_messages,
            ep.DEFAULT_BATCH_MESSAGES,
        )

    def test_chunk_off_skips_batching_only(self):
        os.environ["OV_EXTRACT_PATCH_CHUNK"] = "0"
        ep.EXPECTED_RETRY_ASYNC_SHA256 = ep._source_hash(fake_retry_async)
        ep.EXPECTED_RESOLVE_LIMITS_SHA256 = ep._source_hash(resolve_stock)
        self.assertTrue(ep.apply_session(self.mod))
        self.assertTrue(getattr(self.mod.retry_async, "_ov_extract_patch", False))
        self.assertIs(self.mod.resolve_extraction_batch_limits, resolve_stock)


# --- real v0.5.1.1 targets (image only) -------------------------------------------------


def _installed_version():
    if importlib.util.find_spec("openviking") is None:
        return None
    import openviking

    return getattr(openviking, "__version__", None)


REAL = _installed_version() == ep.EXPECTED_VERSION
requires_real = unittest.skipUnless(
    REAL, f"needs openviking {ep.EXPECTED_VERSION} installed (run in the image)"
)
LOCK_TEXT = (
    "lock acquire timed out after 110ms"  # pathlock text (ragfs lock/types.rs:176)
)


@requires_real
class RealTargetTests(EnvMixin, unittest.TestCase):
    """``wrap_retry_async`` around the real, unpatched ``model_retry.retry_async``."""

    def run_real(self, step, max_retries=5):
        from openviking.utils import model_retry

        wrapped = ep.wrap_retry_async(
            model_retry.retry_async, model_retry.is_retryable_api_error
        )
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            try:
                result, exc = (
                    asyncio.run(
                        wrapped(
                            step,
                            max_retries=max_retries,
                            base_delay=0,
                            max_delay=0,
                            is_retryable=model_retry.is_retryable_api_error,
                            operation_name="long_term_memory_extraction",
                        )
                    ),
                    None,
                )
            except Exception as e:  # noqa: BLE001
                result, exc = None, e
        records = [
            json.loads(line[len(ep.OUTCOME_PREFIX) :])
            for line in err.getvalue().splitlines()
            if line.startswith(ep.OUTCOME_PREFIX)
        ]
        return result, exc, records

    def test_stock_retries_a_lock_wait(self):
        # The upstream behaviour the port has to counter (#6102).
        from openviking.storage.errors import LockAcquisitionError as RealLockError
        from openviking.utils import model_retry

        step, calls = scripted(RealLockError(LOCK_TEXT))
        with self.assertRaises(RealLockError):
            asyncio.run(
                model_retry.retry_async(
                    step, max_retries=3, base_delay=0, max_delay=0, jitter=False
                )
            )
        self.assertEqual(len(calls), 4)

    def test_untagged_real_lock_wait_is_not_retried(self):
        from openviking.storage.errors import LockAcquisitionError as RealLockError

        step, calls = scripted(RealLockError(LOCK_TEXT))
        _, exc, records = self.run_real(step)
        self.assertIsInstance(exc, RealLockError)
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            [(r["event"], r["failure_class"]) for r in records],
            [("failed", "lock_wait")],
        )

    def test_before_write_real_lock_wait_gets_the_lock_budget(self):
        from openviking.storage.errors import LockAcquisitionError as RealLockError

        step, calls = scripted(before_write(RealLockError(LOCK_TEXT)))
        _, exc, records = self.run_real(step)
        self.assertIsInstance(exc, RealLockError)
        self.assertEqual(len(calls), 1 + ep.DEFAULT_LOCK_RETRIES)

    def test_model_error_retries_still_emit_retry_lines(self):
        # A builtin TimeoutError is a model error: real retry_async retries it without
        # asking is_retryable (_retry_limit), so the wrapper reports it from the attempt.
        step, calls = scripted(TimeoutError("read timed out"), "ok")
        result, exc, records = self.run_real(step)
        self.assertIsNone(exc)
        self.assertEqual(result, "ok")
        self.assertEqual(
            [(r["event"], r.get("failure_class")) for r in records],
            [("retry", "timeout"), ("recovered", None)],
        )

    def written_then(self, exc):
        """A step that writes (as the streaming half records it), then raises ``exc``."""
        calls = []

        async def step():
            calls.append(1)
            ep._mark_wrote()
            raise exc

        return step, calls

    def test_nothing_is_retried_once_the_attempt_wrote(self):
        from openviking.models.model_error import ModelError, ModelErrorKind
        from openviking.storage.errors import LockAcquisitionError as RealLockError

        for exc in (
            TimeoutError(),
            TimeoutError("storage timed out"),
            ModelError(ModelErrorKind.RATE_LIMIT_ERROR, "429", status_code=429),
            ModelError(ModelErrorKind.TRANSIENT_SERVICE_ERROR, "503", status_code=503),
            before_write(RealLockError(LOCK_TEXT)),
            RuntimeError("Error code: 502 - bad gateway"),
        ):
            with self.subTest(exc=repr(exc)):
                step, calls = self.written_then(exc)
                _, got, records = self.run_real(step)
                self.assertIs(got, exc)
                self.assertEqual(len(calls), 1)
                self.assertEqual([r["event"] for r in records], ["failed"])
                self.assertEqual(records[0]["failure_class"], ep.failure_class(exc))

    def test_structured_model_error_before_a_write_keeps_stock_retry(self):
        from openviking.models.model_error import ModelError, ModelErrorKind

        exc = ModelError(ModelErrorKind.RATE_LIMIT_ERROR, "429", status_code=429)
        step, calls = scripted(exc, "ok")
        result, got, records = self.run_real(step)
        self.assertIsNone(got)
        self.assertEqual(result, "ok")
        self.assertEqual(len(calls), 2)
        self.assertEqual([r["event"] for r in records], ["retry", "recovered"])

    def test_degraded_is_retried_by_the_wrapped_classifier(self):
        from openviking.utils import model_retry

        classifier = ep.wrap_is_retryable(model_retry.is_retryable_api_error)
        self.assertFalse(model_retry.is_retryable_api_error(parse_exhausted()))
        degraded = ep.ExtractionDegradedError(
            f"memory extraction degraded: {parse_exhausted()}"
        )
        self.assertTrue(classifier(degraded))


# The PyPI wheel reports "0.5.1.1" where the image reports "v0.5.1.1". The suite
# runner maps one to the other in its own process only, so a child interpreter
# repeats that mapping before any patch target imports (a no-op in the image).
PIN_WHEEL_VERSION = (
    "import sys, openviking\n"
    "if 'v' + str(openviking.__version__) == {v!r}:\n"
    "    openviking.__version__ = {v!r}\n"
    "    _m = sys.modules.get('openviking._version')\n"
    "    if _m is not None:\n"
    "        _m.version = _m.__version__ = {v!r}\n"
).format(v=ep.EXPECTED_VERSION)


def _run_harness(scenario, extra_env=None):
    """Run ``_phase2_harness_main(scenario)`` in a clean interpreter; return its record."""
    here = os.path.dirname(os.path.abspath(__file__))
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env.setdefault("OV_EXTRACT_PATCH_BASE_DELAY", "0.001")
    env.setdefault("OV_EXTRACT_PATCH_MAX_DELAY", "0.001")
    env.update(extra_env or {})
    code = PIN_WHEEL_VERSION + (
        "sys.path.insert(0, %r); import test_ov_extract_patch as t; "
        "t._phase2_harness_main(%r)" % (here, scenario)
    )
    # A scratch cwd: the default config resolves its workspace to ./data.
    with tempfile.TemporaryDirectory() as cwd:
        proc = subprocess.run(
            [sys.executable, "-I", "-c", code],
            capture_output=True,
            text=True,
            env=env,
            cwd=cwd,
            timeout=300,
        )
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("HARNESS ")]
    if proc.returncode != 0 or not lines:
        raise AssertionError(
            f"harness {scenario} exited {proc.returncode}:\n{proc.stderr[-3000:]}"
        )
    record = json.loads(lines[-1][len("HARNESS ") :])
    record["stderr_tail"] = proc.stderr[-2000:]
    return record


ARCHIVE = "viking://session/default/s1/history/archive_001"
MEMORIES = "viking://user/default/memories"
# (extraction number, lease index within it) -> failure; every other lease succeeds.
# Lease 0 is the append-only lease (before any write); lease 1 is the merge group's,
# after the event landed.
LEASE_SCRIPTS = {
    "guard": {},
    "lock_after_append": {(1, 1): "lock"},
    "lock_before_write": {(1, 0): "lock"},
    "parse_exhaustion": {},
    # Errors upstream's _retry_limit retries without asking the classifier.
    "timeout_empty_after_append": {(1, 1): "timeout_empty"},
    "timeout_storage_after_append": {(1, 1): "timeout_storage"},
    "model_429_after_append": {(1, 1): "model_429"},
    "model_503_after_append": {(1, 1): "model_503"},
    "timeout_before_write": {(1, 0): "timeout_storage"},
}


def _scripted_failure(kind):
    """The exception a scripted lease raises."""
    from openviking.models.model_error import ModelError, ModelErrorKind
    from openviking.storage.errors import LockAcquisitionError as RealLockError

    return {
        "lock": lambda: RealLockError(LOCK_TEXT),
        "timeout_empty": lambda: TimeoutError(),
        "timeout_storage": lambda: TimeoutError("storage timed out"),
        "model_429": lambda: ModelError(
            ModelErrorKind.RATE_LIMIT_ERROR,
            "Error code: 429 - rate limit",
            provider="openai",
            status_code=429,
        ),
        "model_503": lambda: ModelError(
            ModelErrorKind.TRANSIENT_SERVICE_ERROR,
            "Error code: 503 - service unavailable",
            provider="openai",
            status_code=503,
        ),
    }[kind]()


def _phase2_harness_main(scenario):
    """Drive the real v0.5.1.1 Phase 2 for one long-term extraction step.

    Real: ``Session._run_memory_extraction`` (retry, batching, ``.done`` /
    ``.failed.json``), ``ExtractLoop.run``'s loop and parse-exhaustion raise,
    ``StreamingMemoryUpdater.submit`` with its append-only fast path, merge-group
    batcher and ``acquire_memory_operation_lease``, ``retry_async`` and
    ``is_retryable_api_error``, plus this module's ``apply_*`` guards on them. Faked:
    storage (a dict), pathlocks (scripted), the LLM (``_call_llm`` and operation
    resolution), and ``MemoryUpdater`` (``_apply_operations`` takes the real lease, then
    records one file per operation). Each extraction names its event after itself, as
    the LLM picks a fresh name on every run.
    """
    import openviking.service.task_tracker as task_tracker
    from openviking.message.message import Message
    from openviking.message.part import TextPart
    from openviking.session import session as sm
    from openviking.session.memory import extract_loop as el
    from openviking.session.memory import memory_updater as mu
    from openviking.session.memory import streaming_memory_updater as smu
    from openviking.session.memory.dataclass import (
        ResolvedOperation,
        ResolvedOperations,
    )
    from openviking_cli.utils.config import open_viking_config as ovc

    applied = {
        "extract_loop": ep.apply_extract_loop(el),
        "streaming": ep.apply_streaming(smu),
        "session": ep.apply_session(sm),
    }
    wrapped = {
        "ExtractLoop.run": el.ExtractLoop.run,
        "retry_async": sm.retry_async,
        "is_retryable_api_error": sm.is_retryable_api_error,
        "resolve_extraction_batch_limits": sm.resolve_extraction_batch_limits,
        "_apply_append_only_request_now": smu.StreamingMemoryUpdater._apply_append_only_request_now,
        "_submit_grouped_merge_request": smu.StreamingMemoryUpdater._submit_grouped_merge_request,
        "acquire_memory_operation_lease": smu.acquire_memory_operation_lease,
    }
    wrapped = {
        k: bool(getattr(v, "_ov_extract_patch", False)) for k, v in wrapped.items()
    }
    result = {
        "applied": applied,
        "wrapped": wrapped,
        "sitecustomize": "sitecustomize" in sys.modules,
    }
    if scenario == "guard":
        print("HARNESS " + json.dumps(result, sort_keys=True))
        return

    ovc.OpenVikingConfigSingleton._instance = ovc.OpenVikingConfig()
    script = LEASE_SCRIPTS[scenario]
    state = {"extractions": 0, "lease": 0}
    files = {}

    class Agfs:
        async def pathlock_acquire_exact_batch(self, paths, timeout_secs=0.0, **kw):
            key = (state["extractions"], state["lease"])
            state["lease"] += 1
            if key in script:
                raise _scripted_failure(script[key])
            return {"lease": key}

        pathlock_acquire_exact_tree_batch = pathlock_acquire_exact_batch

        async def pathlock_release(self, lease, **kw):
            return None

    class FS:
        _async_agfs = Agfs()

        def _uri_to_path(self, uri, ctx=None):
            return "/" + uri.split("://", 1)[1]

        async def write_file(self, uri, content, ctx=None, **kw):
            files[uri] = content

        async def read_file(self, uri, ctx=None, **kw):
            if uri in files:
                return files[uri]
            raise FileNotFoundError(uri)

        async def exists(self, uri, ctx=None, **kw):
            return uri in files

    fs = FS()

    class SessionExtractContextProvider:  # the exact class name the loop half keys on
        read_file_contents = {}

        def __init__(self, messages):
            self._extract_context = mu.ExtractContext(messages)

        def get_memory_schemas(self, ctx):
            return []

        def get_output_language(self):
            return "en"

        def get_tools(self):
            return []

        def get_extract_context(self):
            return self._extract_context

        def instruction(self):
            return "extract"

        async def prefetch(self):
            return []

    class Schema:
        def __init__(self, memory_type):
            self.operation_mode = "add_only" if memory_type == "events" else "upsert"
            self._account_content_template = None

        def model_dump(self):
            return {"operation_mode": self.operation_mode}

    class Registry:
        def get(self, memory_type):
            return Schema(memory_type)

    class Updater(smu.StreamingMemoryUpdater):
        async def _apply_operations(self, *, operations, request, messages):
            # Upstream streaming_memory_updater.py:497-520, MemoryUpdater recorded.
            lease = await smu.acquire_memory_operation_lease(
                operations, fs, request.ctx
            )
            try:
                applied_result = mu.MemoryUpdateResult()
                for op in operations.upsert_operations:
                    files[op.uris[0]] = f"extraction {state['extractions']}"
                    applied_result.add_written(op.uris[0])
                return applied_result
            finally:
                if lease is not None:
                    await fs._async_agfs.pathlock_release(lease)

    updater = Updater(
        registry=Registry(),
        config=smu.StreamingMemoryUpdaterConfig(
            max_operations_per_update=1,
            max_wait_seconds=0.05,
            timer_check_interval_seconds=0.01,
        ),
    )

    class Compressor:
        """``SessionCompressorV3._extract_user_memories``: loop, then submit."""

        async def extract_long_term_memories(
            self, messages, ctx=None, strict_extract_errors=False, **kwargs
        ):
            state["extractions"] += 1
            state["lease"] = 0
            n = state["extractions"]
            loop = el.ExtractLoop(
                vlm=types.SimpleNamespace(model="fake", max_tokens=None),
                viking_fs=fs,
                ctx=ctx,
                context_provider=SessionExtractContextProvider(messages),
            )
            resolved = ResolvedOperations(
                upsert_operations=[
                    ResolvedOperation(
                        memory_fields={},
                        memory_type="events",
                        uris=[f"{MEMORIES}/events/event-from-extraction-{n}.md"],
                    ),
                    ResolvedOperation(
                        memory_fields={},
                        memory_type="profile",
                        uris=[f"{MEMORIES}/profile.md"],
                    ),
                ],
                delete_file_contents=[],
                errors=[],
            )

            async def call_llm(_messages):
                if scenario == "parse_exhaustion":
                    loop._last_llm_failure_kind = "empty_response"
                    return None, None
                return None, object()

            async def resolve_operations(_operations):
                return resolved, []

            async def no_refetch(_operations):
                return {}

            async def no_patch_errors(_operations):
                return []

            async def finalize(_operations, _links):
                return None

            loop._call_llm = call_llm
            loop.resolve_operations = resolve_operations
            loop._retryable_resolution_issues = lambda _operations: []
            loop._check_unread_existing_files = no_refetch
            loop._validate_patch_operations = no_patch_errors
            loop.finalize_operations = finalize
            operations, _ = await loop.run()
            await updater.submit(
                smu.MemoryUpdateRequest(
                    operations=operations,
                    messages=list(messages),
                    ctx=ctx,
                    strict_extract_errors=strict_extract_errors,
                    metadata={"source_extraction_id": f"x{n}", "session_id": "s1"},
                )
            )
            return {"contexts": []}

    class Tracker:
        async def start(self, *a, **k):
            pass

        async def complete(self, *a, **k):
            pass

        async def fail(self, *a, **k):
            pass

        async def wait_for_task_work(self, *a, **k):
            pass

        async def is_cancellation_requested(self, *a, **k):
            return False

    task_tracker.get_task_tracker = lambda: Tracker()

    async def prepare(archive_uri, messages):
        return messages, "archive_001", "archive_001", [], {}

    async def hydrate(messages):
        return messages

    async def noop(*a, **k):
        return None

    session = sm.Session(viking_fs=fs, session_compressor=Compressor())
    session._prepare_phase2_archive_messages = prepare
    session._tool_outputs = types.SimpleNamespace(hydrate_for_extraction=hydrate)
    session._merge_archive_meta = noop
    session._merge_and_save_commit_meta = noop
    messages = [
        Message(id=f"m{i}", role="user", parts=[TextPart(text=f"hello {i}")])
        for i in range(3)
    ]

    err = io.StringIO()
    real_stderr = sys.stderr
    sys.stderr = err
    try:
        asyncio.run(
            session._run_memory_extraction(
                task_id="t1",
                archive_uri=ARCHIVE,
                messages=messages,
                first_message_id="m0",
                last_message_id="m2",
                memory_policy={},
            )
        )
    finally:
        sys.stderr = real_stderr
        real_stderr.write(err.getvalue()[-6000:])
    result.update(
        extractions=state["extractions"],
        files=files,
        outcomes=[
            json.loads(line[len(ep.OUTCOME_PREFIX) :])
            for line in err.getvalue().splitlines()
            if line.startswith(ep.OUTCOME_PREFIX)
        ],
    )
    print("HARNESS " + json.dumps(result, sort_keys=True))


@requires_real
class Phase2HarnessTests(unittest.TestCase):
    """Plan § 1.3: the real v0.5.1.1 Phase 2 with the patch applied by its own guards."""

    def event_files(self, record):
        return {
            uri: body
            for uri, body in record["files"].items()
            if uri.startswith(f"{MEMORIES}/events/")
        }

    def marker(self, record, name):
        raw = record["files"].get(f"{ARCHIVE}/{name}")
        return None if raw is None else json.loads(raw)

    def test_guards_apply_to_the_real_targets(self):
        record = _run_harness("guard")
        self.assertFalse(record["sitecustomize"])
        self.assertEqual(
            record["applied"],
            {"extract_loop": True, "streaming": True, "session": True},
        )
        self.assertTrue(all(record["wrapped"].values()), record["wrapped"])

    def test_a_lock_wait_after_an_append_only_write_is_not_retried(self):
        # The append-only event lands, then the merge group's lease times out. A stock
        # retry re-extracts and files a second event beside the first, then marks the
        # archive .done.
        record = _run_harness("lock_after_append")
        events = self.event_files(record)
        self.assertEqual(
            events,
            {f"{MEMORIES}/events/event-from-extraction-1.md": "extraction 1"},
            f"{record['extractions']} extraction(s)\n{record['stderr_tail']}",
        )
        self.assertEqual(record["extractions"], 1)
        self.assertIsNone(self.marker(record, ".done"))
        failed = self.marker(record, ".failed.json")
        self.assertEqual(failed["stage"], "memory_extraction")
        self.assertIn("lock acquire timed out", failed["error"])
        self.assertEqual(
            [(o["event"], o["failure_class"]) for o in record["outcomes"]],
            [("failed", "lock_wait")],
        )

    def assert_stopped_after_append(self, scenario, failure_class):
        """One extraction, one event file, ``.failed.json``, one failed outcome line."""
        record = _run_harness(scenario)
        self.assertEqual(
            self.event_files(record),
            {f"{MEMORIES}/events/event-from-extraction-1.md": "extraction 1"},
            f"{scenario}: {record['extractions']} extraction(s)\n"
            f"{record['stderr_tail']}",
        )
        self.assertEqual(record["extractions"], 1)
        self.assertIsNone(self.marker(record, ".done"))
        failed = self.marker(record, ".failed.json")
        self.assertIsNotNone(failed, record["stderr_tail"])
        self.assertEqual(failed["stage"], "memory_extraction")
        self.assertEqual(
            [(o["event"], o["failure_class"]) for o in record["outcomes"]],
            [("failed", failure_class)],
        )
        return record, failed

    def test_a_timeout_after_an_append_only_write_is_not_retried(self):
        # Upstream's _retry_limit retries a builtin TimeoutError as a model error
        # without asking the classifier, with or without a message.
        for scenario in ("timeout_empty_after_append", "timeout_storage_after_append"):
            with self.subTest(scenario=scenario):
                self.assert_stopped_after_append(scenario, "timeout")

    def test_a_model_error_after_an_append_only_write_is_not_retried(self):
        for scenario, text in (
            ("model_429_after_append", "429"),
            ("model_503_after_append", "503"),
        ):
            with self.subTest(scenario=scenario):
                _, failed = self.assert_stopped_after_append(scenario, "other")
                self.assertIn(text, failed["error"])

    def test_a_timeout_before_any_write_keeps_stock_retry(self):
        record = _run_harness("timeout_before_write")
        self.assertEqual(
            self.event_files(record),
            {f"{MEMORIES}/events/event-from-extraction-2.md": "extraction 2"},
            record["stderr_tail"],
        )
        self.assertIsNotNone(self.marker(record, ".done"))
        self.assertEqual(
            [(o["event"], o.get("failure_class")) for o in record["outcomes"]],
            [("retry", "timeout"), ("recovered", None)],
        )

    def test_a_lock_wait_before_any_write_is_still_retried(self):
        record = _run_harness("lock_before_write")
        self.assertEqual(
            self.event_files(record),
            {f"{MEMORIES}/events/event-from-extraction-2.md": "extraction 2"},
        )
        self.assertIsNotNone(self.marker(record, ".done"), record["stderr_tail"])
        self.assertIsNone(self.marker(record, ".failed.json"))
        self.assertEqual(
            [o["event"] for o in record["outcomes"]], ["retry", "recovered"]
        )
        self.assertEqual(record["outcomes"][0]["failure_class"], "lock_wait")

    def test_b_parse_exhaustion_ends_in_failed_json_not_done(self):
        record = _run_harness("parse_exhaustion")
        self.assertIsNone(self.marker(record, ".done"))
        failed = self.marker(record, ".failed.json")
        self.assertIsNotNone(failed, record["stderr_tail"])
        self.assertEqual(failed["stage"], "memory_extraction")
        self.assertIn("could not be parsed as operations", failed["error"])
        self.assertIn("failure_kind=empty_response", failed["error"])
        self.assertEqual(self.event_files(record), {})

    def test_b_parse_exhaustion_is_retried_as_degraded_before_failing(self):
        # BUG-1176's remaining value on v0.5.1.1: a saturated VLM gets the widened
        # Phase 2 retries instead of failing the archive on its first exhausted loop.
        record = _run_harness("parse_exhaustion")
        self.assertEqual(record["extractions"], 1 + ep.DEFAULT_RETRIES)
        self.assertEqual(
            [(o["event"], o["failure_class"]) for o in record["outcomes"]],
            [("retry", "degraded")] * ep.DEFAULT_RETRIES + [("failed", "degraded")],
        )
        self.assertEqual(
            self.marker(record, ".failed.json")["stage"], "memory_extraction"
        )


if __name__ == "__main__":
    unittest.main()
