"""Tests for ov_extract_patch. Pure (fakes only) — runs anywhere:

    python3 test_ov_extract_patch.py

The version/source-hash guard is exercised against the INSTALLED openviking by piping
ov_extract_patch.py into a throwaway interpreter in the pod (see the BUG-1176 PR);
this file covers the wrapper semantics.
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
import io
import json
import os
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


class WrapRunTests(unittest.TestCase):
    def run_wrapped(self, result, kind=None, loop_cls=Loop):
        loop = loop_cls()
        loop._last_llm_failure_kind = kind
        return asyncio.run(make(result)(loop))

    def test_merge_loop_errors_only_is_returned_not_raised(self):
        ops = Ops(errors=["merge program unparseable"])
        with self.assertLogs("ov_extract_patch", level="WARNING") as logs:
            got, _ = self.run_wrapped((ops, []), kind="parse_error", loop_cls=MergeLoop)
        self.assertIs(got, ops)
        self.assertTrue(
            any(
                "PatchMergeContextProvider" in m and "duplicate" in m
                for m in logs.output
            )
        )

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
):
    """Upstream v0.4.20 ``retry_async`` control flow, without the sleep."""
    attempt = 0
    while True:
        try:
            return await func()
        except Exception as e:
            if max_retries <= 0 or attempt >= max_retries or not is_retryable(e):
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
    """The slice of stock ``is_retryable_api_error`` these tests need: text "timeout"."""
    return "timeout" in str(error).lower()


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
        step, calls = scripted(before_write(lock_error()))
        _, exc, records = self.run_step(step)
        self.assertIsInstance(exc, LockAcquisitionError)
        self.assertEqual(len(calls), 4)  # 1 + DEFAULT_LOCK_RETRIES
        self.assertEqual(records[-1]["failure_class"], "lock_wait")

    def test_untagged_lock_wait_keeps_stock_behaviour(self):
        # A lock wait after the attempt wrote: a retry would re-extract with a new
        # source_extraction_id and duplicate the written events.
        step, calls = scripted(lock_error())
        _, exc, records = self.run_step(step)
        self.assertIsInstance(exc, LockAcquisitionError)
        self.assertEqual(len(calls), 1)
        self.assertEqual([r["event"] for r in records], ["failed"])

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
    """The v0.4.20 ``StreamingMemoryUpdater.submit`` write order, with scripted leases.

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
        """Run one Phase 2 step: session extraction, then each ``submit`` in order."""
        calls = []
        run = ep.wrap_run(lambda self: _ops_result())

        async def step():
            calls.append(1)
            if extract:
                await run(Loop())
            for kwargs in submits:
                await updater.submit(**kwargs)
            return "ok"

        wrapped = ep.wrap_retry_async(fake_retry_async, lambda e: False)
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

        async def _acquire_stable_operation_lease(operations, viking_fs, ctx):
            return None

        self.cls = Updater
        self.mod = types.SimpleNamespace(
            StreamingMemoryUpdater=Updater,
            _acquire_stable_operation_lease=_acquire_stable_operation_lease,
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
            self.mod._acquire_stable_operation_lease
        )

    def test_hash_mismatch_installs_nothing(self):
        orig = self.mod._acquire_stable_operation_lease
        self.assertFalse(ep.apply_streaming(self.mod))
        self.assertIs(self.mod._acquire_stable_operation_lease, orig)
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
            self.mod._acquire_stable_operation_lease,
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


if __name__ == "__main__":
    unittest.main()
