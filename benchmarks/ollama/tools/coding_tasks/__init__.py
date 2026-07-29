"""Graded agentic-coding task fixtures (PROJ-1003, TASK-1134 lineage).

Each task is a self-contained sandbox repo plus a HIDDEN verifier the model never
sees. Success is not "the answer looks right" — it is `python3 verify.py`
exiting 0 in the sandbox after the agent has finished editing it.

Tiers escalate along the axes that actually break local models in agent loops:

  T1  one file, one bug, one edit. Can the model call a tool with a valid schema
      and write a correct patch at all?
  T2  several files, and the bug is not in the file the symptom names. Requires
      reading before writing — the model must gather context across files.
  T3  a feature to build against an existing suite, with a run_tests tool
      available. Requires a multi-step loop: edit, test, read failure, re-edit.

Verifiers use only the standard library (no pytest) so a run never depends on a
network fetch or a virtualenv.
"""

from dataclasses import dataclass, field


@dataclass
class CodingTask:
    task_id: str
    tier: int
    title: str
    prompt: str
    files: dict[str, str]
    verifier: str
    # Files the agent is expected to change. Used for reporting only — a task is
    # scored by its verifier, never by which paths were touched.
    target_files: list[str] = field(default_factory=list)
    max_turns: int = 12


# ---------------------------------------------------------------------------
# T1 — single file, single bug
# ---------------------------------------------------------------------------

_T1A_SRC = '''\
"""Rolling window statistics for pipeline run durations."""


def moving_average(values, window):
    """Return the moving average of `values` over `window` samples.

    The result has one entry per full window, so len(result) ==
    len(values) - window + 1 for a non-empty input.
    """
    if window <= 0:
        raise ValueError("window must be positive")
    out = []
    for i in range(len(values) - window):
        chunk = values[i : i + window]
        out.append(sum(chunk) / window)
    return out
'''

_T1A_VERIFY = """\
from stats import moving_average

assert moving_average([1, 2, 3, 4], 2) == [1.5, 2.5, 3.5], moving_average([1, 2, 3, 4], 2)
assert moving_average([1, 2, 3], 3) == [2.0], moving_average([1, 2, 3], 3)
assert moving_average([5], 1) == [5.0], moving_average([5], 1)
assert moving_average([], 1) == [], moving_average([], 1)

try:
    moving_average([1, 2], 0)
except ValueError:
    pass
else:
    raise AssertionError("window=0 must raise ValueError")

print("OK")
"""

_T1B_SRC = '''\
"""Retry-budget accounting for the ingest scheduler."""


class RetryBudget:
    """Tracks how many retries remain for a job.

    `consume` spends one retry and returns True when the spend was allowed.
    Once the budget is exhausted, further calls return False and must not push
    `remaining` below zero.
    """

    def __init__(self, limit):
        self.limit = limit
        self.used = 0

    @property
    def remaining(self):
        return self.limit - self.used

    def consume(self):
        self.used += 1
        return self.remaining >= 0

    def reset(self):
        self.used = 0
'''

_T1B_VERIFY = """\
from budget import RetryBudget

b = RetryBudget(2)
assert b.remaining == 2, b.remaining
assert b.consume() is True
assert b.remaining == 1, b.remaining
assert b.consume() is True
assert b.remaining == 0, b.remaining
# Budget is spent: further consumes are refused and must not go negative.
assert b.consume() is False
assert b.remaining == 0, "remaining must clamp at 0, got %r" % (b.remaining,)
assert b.consume() is False
assert b.remaining == 0, b.remaining

b.reset()
assert b.remaining == 2, b.remaining
assert b.consume() is True

z = RetryBudget(0)
assert z.consume() is False
assert z.remaining == 0, z.remaining

print("OK")
"""


# ---------------------------------------------------------------------------
# T2 — multi-file, symptom and cause in different files
# ---------------------------------------------------------------------------

_T2A_PARSER = '''\
"""Parse pipeline manifest lines into records."""

from normalize import normalize_name


def parse_line(line):
    """Parse "name:count" into a record dict, or None for blank/comment lines."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    name, _, count = line.partition(":")
    return {"name": normalize_name(name), "count": int(count)}


def parse_manifest(text):
    records = []
    for line in text.splitlines():
        record = parse_line(line)
        if record is not None:
            records.append(record)
    return records
'''

_T2A_NORMALIZE = '''\
"""Name normalization shared by the parser and the reporter."""


def normalize_name(name):
    """Normalize a pipeline name for comparison.

    Names are case-insensitive and may be padded with whitespace. Internal
    separators are normalized to underscores so "daily load", "daily-load" and
    "Daily_Load" all compare equal.
    """
    return name.strip().lower().replace("-", "_")
'''

_T2A_REPORT = '''\
"""Aggregate parsed records for reporting."""

from collections import defaultdict

from normalize import normalize_name


def totals_by_name(records):
    """Sum counts per normalized name."""
    totals = defaultdict(int)
    for record in records:
        totals[normalize_name(record["name"])] += record["count"]
    return dict(totals)
'''

_T2A_VERIFY = '''\
from report import totals_by_name
from parser import parse_manifest
from normalize import normalize_name

# The reported symptom: these three spellings are one pipeline, so the report
# must collapse them into a single row.
manifest = """
# nightly manifest
daily load: 3
daily-load: 4
Daily_Load: 5
other job: 1
"""

totals = totals_by_name(parse_manifest(manifest))
assert totals == {"daily_load": 12, "other_job": 1}, totals

# normalize_name must be idempotent -- normalizing twice cannot change the value.
for raw in ["daily load", "daily-load", "Daily_Load", "  MIXED case-name  "]:
    once = normalize_name(raw)
    assert normalize_name(once) == once, (raw, once, normalize_name(once))

# And it must still handle the cases it already handled.
assert normalize_name("  Daily-Load ") == "daily_load", normalize_name("  Daily-Load ")

print("OK")
'''

_T2B_QUEUE = '''\
"""In-memory job queue with priority ordering."""

import heapq
import itertools


class JobQueue:
    """A priority queue of jobs.

    Lower `priority` values are dequeued first. Jobs pushed with equal priority
    come out in the order they were pushed (FIFO within a priority).
    """

    def __init__(self):
        self._heap = []
        self._counter = itertools.count()

    def push(self, job, priority=5):
        heapq.heappush(self._heap, (priority, job))

    def pop(self):
        if not self._heap:
            return None
        _, job = heapq.heappop(self._heap)
        return job

    def __len__(self):
        return len(self._heap)
'''

_T2B_SCHEDULER = '''\
"""Scheduler that drains the queue in priority order."""

from queue_impl import JobQueue


class Job:
    def __init__(self, name, retries=0):
        self.name = name
        self.retries = retries

    def __repr__(self):
        return "Job(%r)" % (self.name,)


def drain(jobs):
    """Push (job, priority) pairs and return the names in dequeue order."""
    queue = JobQueue()
    for job, priority in jobs:
        queue.push(job, priority)
    order = []
    while len(queue):
        order.append(queue.pop().name)
    return order
'''

_T2B_VERIFY = """\
from scheduler import Job, drain
from queue_impl import JobQueue

# Priority ordering still holds.
assert drain([(Job("c"), 9), (Job("a"), 1), (Job("b"), 5)]) == ["a", "b", "c"]

# The reported symptom: equal priorities must stay FIFO, and must not raise.
names = drain([(Job("first"), 5), (Job("second"), 5), (Job("third"), 5)])
assert names == ["first", "second", "third"], names

# ADVERSARIAL: the cheapest wrong "fix" for the TypeError is to make Job
# orderable (e.g. __lt__ comparing names), which silently sorts equal-priority
# jobs instead of preserving insertion order. Every same-priority group below is
# pushed in REVERSE lexical order, so a name-sorting implementation returns them
# backwards and fails here.
reverse = drain([(Job("zulu"), 5), (Job("mike"), 5), (Job("alpha"), 5)])
assert reverse == ["zulu", "mike", "alpha"], (
    "equal priorities must keep insertion order, not sort by name: %r" % (reverse,)
)

mixed = drain([
    (Job("p9-zulu"), 9),
    (Job("p1-zulu"), 1),
    (Job("p9-alpha"), 9),
    (Job("p1-alpha"), 1),
])
assert mixed == ["p1-zulu", "p1-alpha", "p9-zulu", "p9-alpha"], mixed

# Exercise the queue directly, not only through drain(), and with a payload that
# has no ordering of its own -- a tuple-comparison fallback must never be reached.
q = JobQueue()
assert q.pop() is None
assert len(q) == 0

payloads = [object() for _ in range(4)]
for payload in payloads:
    q.push(payload, 5)
assert len(q) == 4, len(q)
popped = [q.pop() for _ in range(4)]
assert popped == payloads, "JobQueue must return equal-priority items FIFO"
assert q.pop() is None

# Interleaved priorities, pushed worst-first within each group.
q2 = JobQueue()
q2.push("late-low", 1)
q2.push("early-high", 9)
q2.push("later-low", 1)
assert [q2.pop(), q2.pop(), q2.pop()] == ["late-low", "later-low", "early-high"]

print("OK")
"""


# ---------------------------------------------------------------------------
# T3 — build a feature against an existing suite, with a test tool available
# ---------------------------------------------------------------------------

_T3A_CACHE = '''\
"""A tiny TTL cache used by the metrics collector."""


class TTLCache:
    """Mapping with per-entry expiry.

    `clock` is a zero-argument callable returning a monotonic float, injected so
    tests can control time.
    """

    def __init__(self, ttl, clock):
        self.ttl = ttl
        self.clock = clock
        self._data = {}

    def set(self, key, value):
        self._data[key] = (value, self.clock())

    def get(self, key, default=None):
        entry = self._data.get(key)
        if entry is None:
            return default
        value, stamp = entry
        if self.clock() - stamp >= self.ttl:
            del self._data[key]
            return default
        return value

    def __len__(self):
        return len(self._data)
'''

_T3A_EXISTING_TESTS = '''\
"""The suite that already passes. It must keep passing."""

from cache import TTLCache


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def test_set_get():
    clock = FakeClock()
    cache = TTLCache(ttl=10, clock=clock)
    cache.set("a", 1)
    assert cache.get("a") == 1


def test_expiry():
    clock = FakeClock()
    cache = TTLCache(ttl=10, clock=clock)
    cache.set("a", 1)
    clock.advance(10)
    assert cache.get("a") is None


def test_missing_default():
    clock = FakeClock()
    cache = TTLCache(ttl=10, clock=clock)
    assert cache.get("nope", "fallback") == "fallback"


def run():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("existing suite OK")


if __name__ == "__main__":
    run()
'''

_T3A_VERIFY = """\
import test_cache
from cache import TTLCache


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


# The pre-existing suite must still pass -- no regressions allowed.
test_cache.run()

clock = FakeClock()
cache = TTLCache(ttl=10, clock=clock, max_size=2)

# Capacity is enforced.
cache.set("a", 1)
cache.set("b", 2)
cache.set("c", 3)
assert len(cache) == 2, len(cache)
# "a" was least recently used, so it is the one evicted.
assert cache.get("a") is None, "expected 'a' to be evicted"
assert cache.get("b") == 2
assert cache.get("c") == 3

# A `get` counts as a use: it protects an entry from the next eviction.
clock2 = FakeClock()
c2 = TTLCache(ttl=100, clock=clock2, max_size=2)
c2.set("x", 1)
c2.set("y", 2)
assert c2.get("x") == 1        # x is now the most recently used
c2.set("z", 3)                 # evicts y, not x
assert c2.get("y") is None, "get() must count as a use, so 'y' should evict"
assert c2.get("x") == 1
assert c2.get("z") == 3

# Overwriting an existing key updates it in place rather than growing the cache.
c3 = TTLCache(ttl=100, clock=FakeClock(), max_size=2)
c3.set("k", 1)
c3.set("k", 2)
assert len(c3) == 1, len(c3)
assert c3.get("k") == 2

# max_size stays optional: omitting it keeps the old unbounded behaviour.
c4 = TTLCache(ttl=100, clock=FakeClock())
for i in range(50):
    c4.set(str(i), i)
assert len(c4) == 50, len(c4)

# Expiry still wins over capacity.
clock5 = FakeClock()
c5 = TTLCache(ttl=10, clock=clock5, max_size=5)
c5.set("old", 1)
clock5.advance(10)
assert c5.get("old") is None

print("OK")
"""

_T3B_ROUTER = '''\
"""Minimal path router for the internal status service."""


class Router:
    """Maps URL paths to handlers.

    Routes are registered with `add`, and `resolve` returns (handler, params)
    for a path or (None, {}) when nothing matches.
    """

    def __init__(self):
        self._routes = []

    def add(self, path, handler):
        self._routes.append((path, handler))

    def resolve(self, path):
        for route, handler in self._routes:
            if route == path:
                return handler, {}
        return None, {}
'''

_T3B_EXISTING_TESTS = '''\
"""The suite that already passes. It must keep passing."""

from router import Router


def test_static_match():
    r = Router()
    r.add("/health", "health_handler")
    handler, params = r.resolve("/health")
    assert handler == "health_handler"
    assert params == {}


def test_no_match():
    r = Router()
    r.add("/health", "health_handler")
    handler, params = r.resolve("/missing")
    assert handler is None
    assert params == {}


def test_first_registered_wins():
    r = Router()
    r.add("/a", "first")
    r.add("/a", "second")
    handler, _ = r.resolve("/a")
    assert handler == "first"


def run():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("existing suite OK")


if __name__ == "__main__":
    run()
'''

_T3B_VERIFY = """\
import test_router
from router import Router

# No regressions in the pre-existing suite.
test_router.run()

r = Router()
r.add("/jobs/<job_id>", "job_detail")
r.add("/jobs/<job_id>/runs/<run_id>", "run_detail")
r.add("/jobs/active", "active_jobs")
r.add("/health", "health")

# Single parameter capture.
handler, params = r.resolve("/jobs/42")
assert handler == "job_detail", handler
assert params == {"job_id": "42"}, params

# Multiple parameter capture.
handler, params = r.resolve("/jobs/42/runs/7")
assert handler == "run_detail", handler
assert params == {"job_id": "42", "run_id": "7"}, params

# A static route must beat a dynamic one that also matches, regardless of
# registration order.
handler, params = r.resolve("/jobs/active")
assert handler == "active_jobs", "static route must win over /jobs/<job_id>"
assert params == {}, params

# Static routes and misses still behave.
handler, params = r.resolve("/health")
assert handler == "health"
handler, params = r.resolve("/nope")
assert handler is None, handler
assert params == {}, params

# Segment counts must match exactly -- a one-segment pattern cannot swallow two.
handler, params = r.resolve("/jobs/42/runs")
assert handler is None, (handler, params)

# Empty segments do not match a parameter.
handler, params = r.resolve("/jobs/")
assert handler is None, (handler, params)

print("OK")
"""


TASKS: list[CodingTask] = [
    CodingTask(
        task_id="t1a-moving-average",
        tier=1,
        title="Off-by-one in a moving average",
        prompt=(
            "The `moving_average` function in stats.py drops the last window: "
            "`moving_average([1, 2, 3, 4], 2)` returns `[1.5, 2.5]` when it should "
            "return `[1.5, 2.5, 3.5]`. Read the file, fix the bug, and write the "
            "corrected file back. Keep the docstring's contract intact."
        ),
        files={"stats.py": _T1A_SRC},
        verifier=_T1A_VERIFY,
        target_files=["stats.py"],
        max_turns=8,
    ),
    CodingTask(
        task_id="t1b-retry-budget",
        tier=1,
        title="Retry budget goes negative",
        prompt=(
            "`RetryBudget` in budget.py is supposed to refuse spends once the "
            "budget is exhausted without letting `remaining` go negative. Today a "
            "third `consume()` on a budget of 2 returns False but leaves "
            "`remaining` at -1. Read the file, fix it so `remaining` never drops "
            "below zero, and write the corrected file back."
        ),
        files={"budget.py": _T1B_SRC},
        verifier=_T1B_VERIFY,
        target_files=["budget.py"],
        max_turns=8,
    ),
    CodingTask(
        task_id="t2a-normalize-idempotent",
        tier=2,
        title="Report double-counts a pipeline (cause is not in report.py)",
        prompt=(
            "Our nightly report shows 'daily load', 'daily-load' and 'Daily_Load' "
            "as separate rows even though they are one pipeline, so counts are "
            "split instead of summed. The symptom shows up in report.py's "
            "`totals_by_name`. Investigate across the files, find the actual "
            "cause, and fix it. `normalize_name` must be idempotent — normalizing "
            "an already-normalized name must not change it."
        ),
        files={
            "parser.py": _T2A_PARSER,
            "normalize.py": _T2A_NORMALIZE,
            "report.py": _T2A_REPORT,
        },
        verifier=_T2A_VERIFY,
        target_files=["normalize.py"],
        max_turns=14,
    ),
    CodingTask(
        task_id="t2b-queue-fifo",
        tier=2,
        title="Job queue crashes on equal priorities",
        prompt=(
            "Draining the scheduler raises `TypeError: '<' not supported between "
            "instances of 'Job' and 'Job'` whenever two jobs share a priority. "
            "The traceback points at scheduler.py's `drain`. Investigate across "
            "the files, fix the real cause, and make sure jobs with equal "
            "priority come out in the order they were pushed."
        ),
        files={
            "queue_impl.py": _T2B_QUEUE,
            "scheduler.py": _T2B_SCHEDULER,
        },
        verifier=_T2B_VERIFY,
        target_files=["queue_impl.py"],
        max_turns=14,
    ),
    CodingTask(
        task_id="t3a-ttl-cache-lru",
        tier=3,
        title="Add bounded LRU eviction to the TTL cache",
        prompt=(
            "Add an optional `max_size` argument to `TTLCache` in cache.py. When "
            "set, the cache holds at most that many entries and evicts the least "
            "recently used one to make room. A `get` counts as a use. Overwriting "
            "an existing key must update it in place rather than growing the "
            "cache. Omitting `max_size` must keep today's unbounded behaviour, "
            "and expiry must keep working exactly as it does now. The existing "
            "suite in test_cache.py must keep passing — run it with the run_tests "
            "tool as you work."
        ),
        files={"cache.py": _T3A_CACHE, "test_cache.py": _T3A_EXISTING_TESTS},
        verifier=_T3A_VERIFY,
        target_files=["cache.py"],
        max_turns=20,
    ),
    CodingTask(
        task_id="t3b-router-params",
        tier=3,
        title="Add parameterized routes to the router",
        prompt=(
            "Extend `Router` in router.py to support parameterized path segments "
            "written as `<name>`, e.g. `/jobs/<job_id>/runs/<run_id>`. `resolve` "
            "must return the handler plus a dict of captured segment values. A "
            "static route must win over a dynamic route that also matches, "
            "regardless of registration order. Segment counts must match exactly, "
            "and an empty segment must not satisfy a parameter. The existing "
            "suite in test_router.py must keep passing — run it with the "
            "run_tests tool as you work."
        ),
        files={"router.py": _T3B_ROUTER, "test_router.py": _T3B_EXISTING_TESTS},
        verifier=_T3B_VERIFY,
        target_files=["router.py"],
        max_turns=20,
    ),
]


def tasks_for_tiers(tiers: list[int]) -> list[CodingTask]:
    return [t for t in TASKS if t.tier in tiers]
