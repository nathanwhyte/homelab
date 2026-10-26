"""Defer managed signals across resource ownership; expose interruptible work.

Factories mask before handing a context to ``with``. Masking only inside
__enter__/__exit__ is too late: Python can raise before either method starts.
Short critical contexts retain the mask throughout; long-lived descriptor
owners permit explicit interruptible calls while keeping transitions masked.
"""

from contextlib import contextmanager
from functools import wraps
import os
import signal
import threading


MANAGED_SIGNALS = frozenset((signal.SIGTERM, signal.SIGINT))
_state = threading.local()


def deferred_depth():
    return getattr(_state, "depth", 0)


def retain_signals_blocked():
    """The adopting CLI's terminal state survives every enclosing scope exit."""
    signal.pthread_sigmask(signal.SIG_BLOCK, MANAGED_SIGNALS)
    _state.retained = True


def _restore(previous):
    if getattr(_state, "retained", False):
        previous = set(previous) | MANAGED_SIGNALS
    signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def _begin(critical):
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, MANAGED_SIGNALS)
    _state.depth = deferred_depth() + 1
    _state.critical = getattr(_state, "critical", 0) + int(critical)
    return previous


def _finish(previous, critical):
    _state.depth -= 1
    _state.critical -= int(critical)
    # All descriptors owned by this scope have already been released. Delivery
    # here may raise; callers must treat that as failure of the whole operation.
    _restore(previous)


def deferred(function):
    """Mask a short complete resource operation, including its function entry.

    Do not return a newly acquired resource: use owned_descriptor instead. If
    storing ownership on an existing object, its caller must also handle an
    exception delivered when this wrapper restores the prior mask.
    """

    @wraps(function)
    def run(*args, **kwargs):
        previous = _begin(True)
        try:
            return function(*args, **kwargs)
        finally:
            _finish(previous, True)

    return run


class _DeferredContext:
    def __init__(self, manager, previous, critical):
        self.manager = manager
        self.previous = previous
        self.critical = critical

    @property
    def gen(self):
        # Preserve generator introspection for diagnostics/fault injection.
        return getattr(self.manager, "gen", None)

    def __enter__(self):
        try:
            return self.manager.__enter__()
        except BaseException:
            _finish(self.previous, self.critical)
            raise

    def __exit__(self, *exc):
        try:
            return self.manager.__exit__(*exc)
        finally:
            _finish(self.previous, self.critical)


def deferred_context(function=None, *, critical=True):
    """Protect a generator's acquisition, ownership and both context exits."""
    if function is None:
        return lambda function: deferred_context(function, critical=critical)
    factory = contextmanager(function)

    @wraps(function)
    def make(*args, **kwargs):
        previous = _begin(critical)
        try:
            return _DeferredContext(factory(*args, **kwargs), previous, critical)
        except BaseException:
            _finish(previous, critical)
            raise

    return make


@deferred_context(critical=False)
def owned_descriptor(opener, *args, closer=os.close, **kwargs):
    """Own a descriptor (or socket) across explicitly interruptible operations."""
    resource = opener(*args, **kwargs)
    try:
        yield resource
    finally:
        closer(resource)


def interruptible(function, *args, **kwargs):
    """Permit delivery only during explicit work outside short critical scopes.

    The managed handler must preserve an already-active exception while cleanup
    unwinds. Callers still check sticky/pending cancellation at operation
    boundaries; enabling delivery is not an acceptance/completion decision.
    """
    if (
        not deferred_depth()
        or getattr(_state, "critical", 0)
        or getattr(_state, "retained", False)
    ):
        return function(*args, **kwargs)
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, set())
    try:
        signal.pthread_sigmask(signal.SIG_UNBLOCK, MANAGED_SIGNALS)
        return function(*args, **kwargs)
    finally:
        _restore(previous)
