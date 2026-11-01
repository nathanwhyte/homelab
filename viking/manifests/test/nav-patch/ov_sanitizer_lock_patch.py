"""Make the privacy sanitizer's cache lock safe across event loops (BUG-1285).

v0.5.1.1 builds one ``PrivacySanitizer`` and hands it to ``VikingFS``
(``service/core.py:530-532``), so one ``_cache_lock = asyncio.Lock()``
(``privacy/sanitizer.py:72``) serves every string write, from uvicorn's loop and from
each queue worker's own thread and loop (``storage/queuefs/queue_manager.py:327``).
``_matcher`` takes it on a matcher cache miss and holds it across
``await self._build_matcher(ctx)`` (``:138``). ``asyncio.Lock`` supports one loop:

  * its first contended wait binds it to that loop for the life of the process
    (``asyncio/mixins.py``), and a contended wait from any other loop then raises
    ``RuntimeError: ... is bound to a different event loop``. On 2026-10-31 that failed
    a ``long_term`` Phase 2 step in ``_merge_archive_meta`` and left the archive without
    ``.done``.
  * a first contender on another loop binds it there and is woken by a ``release`` on
    the holder's thread, which does not wake its loop, so it stalls with no error until
    that loop next wakes (2.95 s against a 0.3 s build on ``ov-test``).

``PrivacySanitizer.__init__`` is wrapped to replace the lock with ``CrossLoopLock``, a
``threading.Lock`` taken without blocking and retried on the caller's own loop. It keeps
what the stock lock gives on one loop, one holder at a time, across all of them, which
``_matcher`` needs: it iterates and then mutates ``_cache`` under the lock. A lock per
loop would drop that, leaving two threads free to mutate and iterate the same dict.

A waiter polls every ``POLL_SECONDS``, so its loop stays free and it enters at most that
much after the release. Contention is rare: the lock is taken only on a cache miss, and
a matcher lives an hour unless the catalog changes.

The guard hashes ``__init__`` and ``_matcher``, the two methods that hold this contract.
Upstream ``main`` still has the single ``asyncio.Lock``. Only this lock is covered; other
``asyncio.Lock`` objects on shared services have not been audited for the same pattern.

Version/source guarded; ``OV_SANITIZER_LOCK_PATCH=0`` rolls back.
"""

import asyncio
import functools
import hashlib
import inspect
import logging
import os
import threading

EXPECTED_VERSION = "v0.5.1.1"
EXPECTED_SHA256 = "3523620de76e3d74a84d1352084d6068c9e44dd508cc4600169b6ab9391f57d7"
POLL_SECONDS = 0.01
logger = logging.getLogger("ov_sanitizer_lock_patch")


class CrossLoopLock:
    """``async with`` mutual exclusion across event loops and their threads."""

    def __init__(self):
        self._lock = threading.Lock()

    async def __aenter__(self):
        while not self._lock.acquire(blocking=False):
            await asyncio.sleep(POLL_SECONDS)

    async def __aexit__(self, *exc_info):
        self._lock.release()


def source_digest(cls):
    try:
        source = inspect.getsource(cls.__init__) + inspect.getsource(cls._matcher)
    except (AttributeError, OSError, TypeError):
        return None
    return hashlib.sha256(source.encode()).hexdigest()


def cross_loop_init(original):
    @functools.wraps(original)
    def __init__(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self._cache_lock = CrossLoopLock()

    __init__._ov_sanitizer_lock_patch = True
    return __init__


def apply(module):
    if os.environ.get("OV_SANITIZER_LOCK_PATCH", "1") == "0":
        logger.warning("ov-sanitizer-lock: disabled by OV_SANITIZER_LOCK_PATCH=0")
        return False
    import openviking

    cls = getattr(module, "PrivacySanitizer", None)
    original = getattr(cls, "__init__", None)
    if getattr(original, "_ov_sanitizer_lock_patch", False):
        return True
    digest = source_digest(cls)
    version = getattr(openviking, "__version__", None)
    if version != EXPECTED_VERSION or digest != EXPECTED_SHA256:
        logger.warning(
            "ov-sanitizer-lock: NOT applied (version=%r sha256=%s)", version, digest
        )
        return False
    cls.__init__ = cross_loop_init(original)
    logger.warning("ov-sanitizer-lock: applied (pid %d)", os.getpid())
    return True
