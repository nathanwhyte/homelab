"""BUG-1285: the privacy sanitizer's cache lock works when two event loops contend.

``CrossLoopLockTests`` need no OpenViking. ``RealTargetTests`` need the installed
openviking: they drive the real ``PrivacySanitizer._matcher`` from two threads with a
stubbed catalog, in the two shapes the stock lock fails, so they exercise the real lock
use without a server. The stock-behaviour cases pin the bug itself, so a release that
fixes it fails here and says the patch can go.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "_scripts" / "lib"))
from git_env import scrub_environ  # noqa: E402

scrub_environ()

import asyncio
import importlib
import importlib.util
import inspect
import os
import threading
import time
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ov_sanitizer_lock_patch as sl

HAVE_OPENVIKING = importlib.util.find_spec("openviking") is not None
BUILD_SECONDS = 0.2
# Loop B's only other wake-up in the first-contender case, well past a clean build.
NEXT_WAKE_SECONDS = 1.5
JOIN_SECONDS = 10
CTX = types.SimpleNamespace(account_id="a", user=types.SimpleNamespace(user_id="u"))


class StubPrivacy:
    """Counts builds, and how many ran at once across every loop."""

    def __init__(self):
        self.guard = threading.Lock()
        self.running = 0
        self.most_running = 0
        self.builds = 0

    def catalog_revision(self, ctx):
        return 0

    async def list_categories(self, ctx):
        with self.guard:
            self.builds += 1
            self.running += 1
            self.most_running = max(self.most_running, self.running)
        await asyncio.sleep(BUILD_SECONDS)
        with self.guard:
            self.running -= 1
        return []


def on_thread(factory):
    """Run ``factory()`` on a new thread's own event loop."""
    box = {}

    def run():
        box["result"] = asyncio.run(factory())

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, box


async def timed_matcher(sanitizer, delay):
    await asyncio.sleep(delay)
    started = time.monotonic()
    try:
        await sanitizer._matcher(CTX)
    except RuntimeError as exc:
        return str(exc), time.monotonic() - started
    return None, time.monotonic() - started


def bind_then_cross(sanitizer):
    """A contended wait on loop A binds the lock, then loop B contends."""

    async def loop_a():
        return await asyncio.gather(
            timed_matcher(sanitizer, 0), timed_matcher(sanitizer, 0.05)
        )

    thread_a, box_a = on_thread(loop_a)
    thread_b, box_b = on_thread(lambda: timed_matcher(sanitizer, 0.1))
    thread_a.join(JOIN_SECONDS)
    thread_b.join(JOIN_SECONDS)
    return box_a["result"], box_b["result"]


def first_contender_cross(sanitizer):
    """Loop B contends first while loop A holds, so the lock binds to B."""

    async def loop_b():
        waiter = asyncio.create_task(timed_matcher(sanitizer, 0.05))
        await asyncio.sleep(NEXT_WAKE_SECONDS)
        return await waiter

    thread_a, box_a = on_thread(lambda: timed_matcher(sanitizer, 0))
    thread_b, box_b = on_thread(loop_b)
    thread_a.join(JOIN_SECONDS)
    thread_b.join(JOIN_SECONDS)
    return box_a["result"], box_b["result"]


class CrossLoopLockTests(unittest.TestCase):
    def test_one_holder_at_a_time_within_a_loop(self):
        lock = sl.CrossLoopLock()
        held = 0
        most = 0

        async def worker():
            nonlocal held, most
            async with lock:
                held += 1
                most = max(most, held)
                await asyncio.sleep(0.01)
                held -= 1

        async def main():
            await asyncio.gather(*(worker() for _ in range(5)))

        asyncio.run(main())
        self.assertEqual(most, 1)

    def test_a_waiter_on_another_loop_enters_only_after_the_release(self):
        lock = sl.CrossLoopLock()
        entered = threading.Event()
        order = []

        async def holder():
            async with lock:
                entered.set()
                await asyncio.sleep(0.1)
                order.append("released")

        async def waiter():
            async with lock:
                order.append("entered")

        thread_a, _ = on_thread(holder)
        self.assertTrue(entered.wait(JOIN_SECONDS))
        thread_b, _ = on_thread(waiter)
        thread_a.join(JOIN_SECONDS)
        thread_b.join(JOIN_SECONDS)
        self.assertEqual(order, ["released", "entered"])

    def test_a_waiter_leaves_its_loop_free(self):
        lock = sl.CrossLoopLock()
        entered = threading.Event()

        async def holder():
            async with lock:
                entered.set()
                await asyncio.sleep(0.2)

        async def waiter():
            ticks = 0

            async def tick():
                nonlocal ticks
                while True:
                    ticks += 1
                    await asyncio.sleep(0.01)

            ticker = asyncio.create_task(tick())
            async with lock:
                pass
            ticker.cancel()
            return ticks

        thread_a, _ = on_thread(holder)
        self.assertTrue(entered.wait(JOIN_SECONDS))
        thread_b, box_b = on_thread(waiter)
        thread_a.join(JOIN_SECONDS)
        thread_b.join(JOIN_SECONDS)
        self.assertGreater(box_b["result"], 3)

    def test_an_error_inside_releases_the_lock(self):
        lock = sl.CrossLoopLock()

        async def main():
            with self.assertRaises(ValueError):
                async with lock:
                    raise ValueError("boom")
            async with asyncio.timeout(1):
                async with lock:
                    return "reacquired"

        self.assertEqual(asyncio.run(main()), "reacquired")

    def test_a_cancelled_waiter_takes_nothing(self):
        lock = sl.CrossLoopLock()

        async def main():
            async with lock:
                waiter = asyncio.create_task(lock.__aenter__())
                await asyncio.sleep(0.03)
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)
            async with asyncio.timeout(1):
                async with lock:
                    return "reacquired"

        self.assertEqual(asyncio.run(main()), "reacquired")

    def test_a_cancelled_holder_releases_the_lock(self):
        lock = sl.CrossLoopLock()

        async def holder():
            async with lock:
                await asyncio.sleep(10)

        async def main():
            task = asyncio.create_task(holder())
            await asyncio.sleep(0.01)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            async with asyncio.timeout(1):
                async with lock:
                    return "reacquired"

        self.assertEqual(asyncio.run(main()), "reacquired")


class ApplyGuardTests(unittest.TestCase):
    def fake_module(self):
        class PrivacySanitizer:
            def __init__(self, privacy):
                self._cache_lock = None

            async def _matcher(self, ctx):
                return None

        return types.SimpleNamespace(PrivacySanitizer=PrivacySanitizer)

    def apply(self, module, version="v0.5.1.1", digest=sl.EXPECTED_SHA256):
        fake_openviking = types.SimpleNamespace(__version__=version)
        with (
            patch.dict(sys.modules, {"openviking": fake_openviking}),
            patch.object(sl, "source_digest", return_value=digest),
        ):
            return sl.apply(module)

    def test_applied_on_the_expected_source(self):
        module = self.fake_module()
        self.assertTrue(self.apply(module))
        self.assertIsInstance(
            module.PrivacySanitizer(object())._cache_lock, sl.CrossLoopLock
        )

    def test_apply_twice_wraps_once(self):
        module = self.fake_module()
        self.apply(module)
        wrapped = module.PrivacySanitizer.__init__
        self.assertTrue(self.apply(module))
        self.assertIs(module.PrivacySanitizer.__init__, wrapped)

    def test_refused_on_a_changed_source(self):
        module = self.fake_module()
        self.assertFalse(self.apply(module, digest="0" * 64))
        self.assertIsNone(module.PrivacySanitizer(object())._cache_lock)

    def test_refused_on_another_version(self):
        module = self.fake_module()
        self.assertFalse(self.apply(module, version="v0.5.2.1"))
        self.assertIsNone(module.PrivacySanitizer(object())._cache_lock)

    def test_env_switch_turns_it_off(self):
        module = self.fake_module()
        with patch.dict(os.environ, {"OV_SANITIZER_LOCK_PATCH": "0"}):
            self.assertFalse(self.apply(module))
        self.assertIsNone(module.PrivacySanitizer(object())._cache_lock)


@unittest.skipUnless(HAVE_OPENVIKING, "needs the installed openviking")
class RealTargetTests(unittest.TestCase):
    def setUp(self):
        self.module = importlib.import_module("openviking.privacy.sanitizer")

    def patched(self, privacy):
        cls = self.module.PrivacySanitizer
        with patch.object(cls, "__init__", cls.__init__):
            self.assertTrue(sl.apply(self.module))
            return cls(privacy)

    def stock(self):
        # The suite's sitecustomize tests may already have wrapped the class.
        cls = self.module.PrivacySanitizer
        sanitizer = cls.__new__(cls)
        inspect.unwrap(cls.__init__)(sanitizer, StubPrivacy())
        return sanitizer

    def test_guard_matches_the_installed_source(self):
        self.assertEqual(
            sl.source_digest(self.module.PrivacySanitizer), sl.EXPECTED_SHA256
        )

    def test_stock_lock_raises_across_loops(self):
        _, (error, _took) = bind_then_cross(self.stock())
        self.assertIn("bound to a different event loop", error or "")

    def test_stock_lock_stalls_a_first_contender(self):
        _, (error, took) = first_contender_cross(self.stock())
        self.assertIsNone(error)
        self.assertGreater(took, NEXT_WAKE_SECONDS - 0.2)

    def test_patched_lock_does_not_raise_across_loops(self):
        privacy = StubPrivacy()
        (first, second), (error, took) = bind_then_cross(self.patched(privacy))
        self.assertIsNone(first[0])
        self.assertIsNone(second[0])
        self.assertIsNone(error)
        self.assertLess(took, NEXT_WAKE_SECONDS)
        self.assertEqual(privacy.builds, 1)

    def test_patched_lock_does_not_stall_a_first_contender(self):
        privacy = StubPrivacy()
        _, (error, took) = first_contender_cross(self.patched(privacy))
        self.assertIsNone(error)
        self.assertLess(took, NEXT_WAKE_SECONDS - 0.5)
        self.assertEqual(privacy.builds, 1)

    def test_patched_lock_runs_one_build_at_a_time_across_loops(self):
        privacy = StubPrivacy()
        sanitizer = self.patched(privacy)
        other_user = types.SimpleNamespace(
            account_id="a", user=types.SimpleNamespace(user_id="v")
        )

        async def build(ctx):
            await sanitizer._matcher(ctx)

        thread_a, _ = on_thread(lambda: build(CTX))
        thread_b, _ = on_thread(lambda: build(other_user))
        thread_a.join(JOIN_SECONDS)
        thread_b.join(JOIN_SECONDS)
        self.assertEqual(privacy.builds, 2)
        self.assertEqual(privacy.most_running, 1)


if __name__ == "__main__":
    unittest.main()
