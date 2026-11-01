"""BUG-1285: the privacy sanitizer's cache lock works when two event loops contend.

``CrossLoopLockTests`` need no OpenViking. ``RealTargetTests`` need the installed
openviking: they drive the real ``PrivacySanitizer._matcher`` from two threads with a
stubbed catalog, in the two shapes the stock lock fails, so they exercise the real lock
use without a server. Each step waits on the state the next one needs, not on a delay.
The stock-behaviour cases pin the bug itself, so a release that fixes it fails here and
says the patch can go.
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
import selectors
import threading
import time
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ov_sanitizer_lock_patch as sl

HAVE_OPENVIKING = importlib.util.find_spec("openviking") is not None
TICK_SECONDS = 0.001
JOIN_SECONDS = 10
# How long the stock pin watches a loop that nothing can wake.
STALL_SECONDS = 0.3
CTX = types.SimpleNamespace(account_id="a", user=types.SimpleNamespace(user_id="u"))


class StubPrivacy:
    """Holds each build until ``gate`` opens; counts builds, and how many ran at once."""

    def __init__(self):
        self.guard = threading.Lock()
        self.building = threading.Event()
        self.gate = threading.Event()
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
        self.building.set()
        while not self.gate.is_set():
            await asyncio.sleep(TICK_SECONDS)
        with self.guard:
            self.running -= 1
        return []


class IdleSelector(selectors.DefaultSelector):
    """Reports a loop with nothing left to run: only a real wake moves it after this."""

    def __init__(self):
        super().__init__()
        self.idle = threading.Event()

    def select(self, timeout=None):
        if timeout is None:
            self.idle.set()
        return super().select(timeout)


def on_thread(factory, selector=None):
    """Run ``factory()`` on a new thread's own event loop."""
    box = {}

    async def main():
        box["loop"] = asyncio.get_running_loop()
        box["result"] = await factory()

    def run():
        loop_factory = selector and (lambda: asyncio.SelectorEventLoop(selector))
        with asyncio.Runner(loop_factory=loop_factory or None) as runner:
            runner.run(main())

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, box


def wait_until(predicate):
    deadline = time.monotonic() + JOIN_SECONDS
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out on {predicate}")
        time.sleep(TICK_SECONDS)


def waiting(lock):
    """How many callers are queued, on this patch's lock or on ``asyncio.Lock``."""
    return len(getattr(lock, "_tickets", None) or getattr(lock, "_waiters", None) or ())


async def wait_for_event(event):
    while not event.is_set():
        await asyncio.sleep(TICK_SECONDS)


async def matcher_error(sanitizer):
    try:
        await sanitizer._matcher(CTX)
    except RuntimeError as exc:
        return str(exc)
    return None


def bind_then_cross(sanitizer, privacy):
    """A contended wait on loop A binds the lock, then loop B contends."""
    lock = sanitizer._cache_lock

    async def loop_a():
        holder = asyncio.create_task(matcher_error(sanitizer))
        await wait_for_event(privacy.building)
        return await asyncio.gather(holder, matcher_error(sanitizer))

    thread_a, box_a = on_thread(loop_a)
    wait_until(lambda: waiting(lock) == 1)
    thread_b, box_b = on_thread(lambda: matcher_error(sanitizer))
    wait_until(lambda: waiting(lock) == 2 or not thread_b.is_alive())
    privacy.gate.set()
    thread_a.join(JOIN_SECONDS)
    thread_b.join(JOIN_SECONDS)
    return box_a["result"], box_b["result"]


def first_contender_cross(sanitizer, privacy, selector=None):
    """Loop B contends first while loop A holds, so the lock binds to B; A then releases.

    With ``selector``, A releases only once B's loop has blocked with no timeout.
    """
    thread_a, _ = on_thread(lambda: matcher_error(sanitizer))
    wait_until(privacy.building.is_set)
    thread_b, box_b = on_thread(lambda: matcher_error(sanitizer), selector)
    wait_until(lambda: waiting(sanitizer._cache_lock) == 1)
    if selector:
        wait_until(selector.idle.is_set)
    privacy.gate.set()
    thread_a.join(JOIN_SECONDS)
    return thread_b, box_b


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
        release = threading.Event()
        order = []

        async def holder():
            async with lock:
                entered.set()
                await wait_for_event(release)
                order.append("released")

        async def waiter():
            async with lock:
                order.append("entered")

        thread_a, _ = on_thread(holder)
        self.assertTrue(entered.wait(JOIN_SECONDS))
        thread_b, _ = on_thread(waiter)
        wait_until(lambda: waiting(lock) == 1)
        self.assertEqual(order, [])
        release.set()
        thread_a.join(JOIN_SECONDS)
        thread_b.join(JOIN_SECONDS)
        self.assertEqual(order, ["released", "entered"])

    def test_a_later_caller_does_not_overtake_a_waiter(self):
        lock = sl.CrossLoopLock()
        order = []

        async def caller(name):
            async with lock:
                order.append(name)

        async def main():
            await lock.__aenter__()
            waiter = asyncio.create_task(caller("waiter"))
            await asyncio.sleep(0)
            await lock.__aexit__(None, None, None)
            await caller("later")
            await waiter

        asyncio.run(main())
        self.assertEqual(order, ["waiter", "later"])

    def test_waiters_on_different_loops_enter_in_arrival_order(self):
        lock = sl.CrossLoopLock()
        order = []
        entered = threading.Event()
        release = threading.Event()

        async def holder():
            async with lock:
                entered.set()
                await wait_for_event(release)

        async def waiter(name):
            async with lock:
                order.append(name)

        thread_a, _ = on_thread(holder)
        self.assertTrue(entered.wait(JOIN_SECONDS))
        thread_b, _ = on_thread(lambda: waiter("first"))
        wait_until(lambda: waiting(lock) == 1)
        thread_c, _ = on_thread(lambda: waiter("second"))
        wait_until(lambda: waiting(lock) == 2)
        release.set()
        for thread in (thread_a, thread_b, thread_c):
            thread.join(JOIN_SECONDS)
        self.assertEqual(order, ["first", "second"])

    def test_a_waiter_leaves_its_loop_free(self):
        lock = sl.CrossLoopLock()
        entered = threading.Event()
        release = threading.Event()
        ticks = 0

        async def holder():
            async with lock:
                entered.set()
                await wait_for_event(release)

        async def waiter():
            async def tick():
                nonlocal ticks
                while True:
                    ticks += 1
                    await asyncio.sleep(TICK_SECONDS)

            ticker = asyncio.create_task(tick())
            async with lock:
                pass
            ticker.cancel()

        thread_a, _ = on_thread(holder)
        self.assertTrue(entered.wait(JOIN_SECONDS))
        thread_b, _ = on_thread(waiter)
        wait_until(lambda: ticks > 3)
        release.set()
        thread_a.join(JOIN_SECONDS)
        thread_b.join(JOIN_SECONDS)
        self.assertFalse(thread_b.is_alive())

    def test_an_error_inside_releases_the_lock(self):
        lock = sl.CrossLoopLock()

        async def main():
            with self.assertRaises(ValueError):
                async with lock:
                    raise ValueError("boom")
            async with asyncio.timeout(JOIN_SECONDS):
                async with lock:
                    return "reacquired"

        self.assertEqual(asyncio.run(main()), "reacquired")

    def test_a_cancelled_waiter_takes_nothing(self):
        lock = sl.CrossLoopLock()

        async def main():
            async with lock:
                waiter = asyncio.create_task(lock.__aenter__())
                await asyncio.sleep(0)
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)
            async with asyncio.timeout(JOIN_SECONDS):
                async with lock:
                    return "reacquired"

        self.assertEqual(asyncio.run(main()), "reacquired")

    def test_a_cancelled_waiter_leaves_the_queue_behind_it_moving(self):
        lock = sl.CrossLoopLock()
        order = []

        async def waiter(name):
            async with lock:
                order.append(name)

        async def main():
            await lock.__aenter__()
            first = asyncio.create_task(waiter("first"))
            await asyncio.sleep(0)
            second = asyncio.create_task(waiter("second"))
            await asyncio.sleep(0)
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
            await lock.__aexit__(None, None, None)
            async with asyncio.timeout(JOIN_SECONDS):
                await second

        asyncio.run(main())
        self.assertEqual(order, ["second"])

    def test_a_cancelled_holder_releases_the_lock(self):
        lock = sl.CrossLoopLock()

        async def main():
            entered = asyncio.Event()

            async def holder():
                async with lock:
                    entered.set()
                    await asyncio.Event().wait()

            task = asyncio.create_task(holder())
            await entered.wait()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            async with asyncio.timeout(JOIN_SECONDS):
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

    def stock(self, privacy):
        # The suite's sitecustomize tests may already have wrapped the class.
        cls = self.module.PrivacySanitizer
        sanitizer = cls.__new__(cls)
        inspect.unwrap(cls.__init__)(sanitizer, privacy)
        return sanitizer

    def test_guard_matches_the_installed_source(self):
        self.assertEqual(
            sl.source_digest(self.module.PrivacySanitizer), sl.EXPECTED_SHA256
        )

    def test_stock_lock_raises_across_loops(self):
        privacy = StubPrivacy()
        _, error = bind_then_cross(self.stock(privacy), privacy)
        self.assertIn("bound to a different event loop", error or "")

    def test_stock_lock_stalls_a_first_contender(self):
        privacy = StubPrivacy()
        selector = IdleSelector()
        thread_b, box_b = first_contender_cross(self.stock(privacy), privacy, selector)
        thread_b.join(STALL_SECONDS)
        self.assertTrue(thread_b.is_alive(), "the release woke the other loop")
        box_b["loop"].call_soon_threadsafe(lambda: None)
        thread_b.join(JOIN_SECONDS)
        self.assertFalse(thread_b.is_alive())
        self.assertIsNone(box_b["result"])

    def test_patched_lock_does_not_raise_across_loops(self):
        privacy = StubPrivacy()
        loop_a_errors, loop_b_error = bind_then_cross(self.patched(privacy), privacy)
        self.assertEqual(loop_a_errors, [None, None])
        self.assertIsNone(loop_b_error)
        self.assertEqual(privacy.builds, 1)

    def test_patched_lock_does_not_stall_a_first_contender(self):
        privacy = StubPrivacy()
        thread_b, box_b = first_contender_cross(self.patched(privacy), privacy)
        thread_b.join(JOIN_SECONDS)
        self.assertFalse(thread_b.is_alive(), "the waiter never woke")
        self.assertIsNone(box_b["result"])
        self.assertEqual(privacy.builds, 1)

    def test_patched_lock_runs_one_build_at_a_time_across_loops(self):
        privacy = StubPrivacy()
        sanitizer = self.patched(privacy)
        other_user = types.SimpleNamespace(
            account_id="a", user=types.SimpleNamespace(user_id="v")
        )

        thread_a, _ = on_thread(lambda: sanitizer._matcher(CTX))
        wait_until(privacy.building.is_set)
        thread_b, _ = on_thread(lambda: sanitizer._matcher(other_user))
        wait_until(lambda: waiting(sanitizer._cache_lock) == 1 or privacy.running == 2)
        privacy.gate.set()
        thread_a.join(JOIN_SECONDS)
        thread_b.join(JOIN_SECONDS)
        self.assertEqual(privacy.builds, 2)
        self.assertEqual(privacy.most_running, 1)


if __name__ == "__main__":
    unittest.main()
