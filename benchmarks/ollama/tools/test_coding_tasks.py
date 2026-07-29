#!/usr/bin/env python3
"""Fixture self-test for the graded agentic-coding benchmark.

A benchmark is only as good as its verifiers, and there are three ways a verifier
can silently invalidate the whole comparison:

  1. It passes against the UNFIXED repo -> every model scores a free point.
  2. It fails against a CORRECT fix -> every model is marked wrong.
  3. It passes against a PLAUSIBLE WRONG fix -> the benchmark rewards the cheap
     hack over the real fix, which is the worst outcome of the three because the
     scores still look reasonable.

So each task is run against the repo as shipped (must fail), against a reference
solution (must pass), and against every known cheap-hack solution (must fail).
None of these solutions is ever exposed to a model under test.

Case 3 is not hypothetical: the first version of the t2b verifier used only
same-priority job names that happened to be in lexical order, so "fix the
TypeError by giving Job a __lt__ that compares names" passed while silently
sorting equal-priority jobs instead of preserving FIFO. The WRONG_SOLUTIONS table
below is what keeps that hole closed.

Run: python3 test_coding_tasks.py
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from coding_tasks import TASKS

REFERENCE_SOLUTIONS: dict[str, dict[str, str]] = {
    "t1a-moving-average": {
        "stats.py": '''\
"""Rolling window statistics for pipeline run durations."""


def moving_average(values, window):
    """Return the moving average of `values` over `window` samples."""
    if window <= 0:
        raise ValueError("window must be positive")
    out = []
    for i in range(len(values) - window + 1):
        chunk = values[i : i + window]
        out.append(sum(chunk) / window)
    return out
'''
    },
    "t1b-retry-budget": {
        "budget.py": '''\
"""Retry-budget accounting for the ingest scheduler."""


class RetryBudget:
    def __init__(self, limit):
        self.limit = limit
        self.used = 0

    @property
    def remaining(self):
        return self.limit - self.used

    def consume(self):
        if self.used >= self.limit:
            return False
        self.used += 1
        return True

    def reset(self):
        self.used = 0
'''
    },
    "t2a-normalize-idempotent": {
        "normalize.py": '''\
"""Name normalization shared by the parser and the reporter."""


def normalize_name(name):
    """Normalize a pipeline name for comparison."""
    return "_".join(name.strip().lower().replace("-", "_").split())
'''
    },
    "t2b-queue-fifo": {
        "queue_impl.py": '''\
"""In-memory job queue with priority ordering."""

import heapq
import itertools


class JobQueue:
    def __init__(self):
        self._heap = []
        self._counter = itertools.count()

    def push(self, job, priority=5):
        heapq.heappush(self._heap, (priority, next(self._counter), job))

    def pop(self):
        if not self._heap:
            return None
        _, _, job = heapq.heappop(self._heap)
        return job

    def __len__(self):
        return len(self._heap)
'''
    },
    "t3a-ttl-cache-lru": {
        "cache.py": '''\
"""A tiny TTL cache used by the metrics collector."""

from collections import OrderedDict


class TTLCache:
    def __init__(self, ttl, clock, max_size=None):
        self.ttl = ttl
        self.clock = clock
        self.max_size = max_size
        self._data = OrderedDict()

    def set(self, key, value):
        if key in self._data:
            del self._data[key]
        self._data[key] = (value, self.clock())
        if self.max_size is not None:
            while len(self._data) > self.max_size:
                self._data.popitem(last=False)

    def get(self, key, default=None):
        entry = self._data.get(key)
        if entry is None:
            return default
        value, stamp = entry
        if self.clock() - stamp >= self.ttl:
            del self._data[key]
            return default
        self._data.move_to_end(key)
        return value

    def __len__(self):
        return len(self._data)
'''
    },
    "t3b-router-params": {
        "router.py": '''\
"""Minimal path router for the internal status service."""


class Router:
    def __init__(self):
        self._routes = []

    def add(self, path, handler):
        self._routes.append((path, handler))

    def resolve(self, path):
        for route, handler in self._routes:
            if route == path:
                return handler, {}
        parts = path.split("/")
        for route, handler in self._routes:
            route_parts = route.split("/")
            if len(route_parts) != len(parts):
                continue
            params = {}
            matched = True
            for expected, actual in zip(route_parts, parts):
                if expected.startswith("<") and expected.endswith(">"):
                    if not actual:
                        matched = False
                        break
                    params[expected[1:-1]] = actual
                elif expected != actual:
                    matched = False
                    break
            if matched:
                return handler, params
        return None, {}
'''
    },
}


# Plausible-but-wrong fixes. Each entry is (label, files) and MUST fail its
# task's verifier. These are the shortcuts a model actually reaches for.
WRONG_SOLUTIONS: dict[str, list[tuple[str, dict[str, str]]]] = {
    "t1b-retry-budget": [
        (
            "clamp the property but keep incrementing",
            {
                "budget.py": """\
class RetryBudget:
    def __init__(self, limit):
        self.limit = limit
        self.used = 0

    @property
    def remaining(self):
        return max(0, self.limit - self.used)

    def consume(self):
        self.used += 1
        return self.remaining >= 0

    def reset(self):
        self.used = 0
"""
            },
        )
    ],
    "t2b-queue-fifo": [
        (
            "make Job orderable by name (sorts instead of FIFO)",
            {
                "scheduler.py": """\
from queue_impl import JobQueue


class Job:
    def __init__(self, name, retries=0):
        self.name = name
        self.retries = retries

    def __lt__(self, other):
        return self.name < other.name

    def __repr__(self):
        return "Job(%r)" % (self.name,)


def drain(jobs):
    queue = JobQueue()
    for job, priority in jobs:
        queue.push(job, priority)
    order = []
    while len(queue):
        order.append(queue.pop().name)
    return order
"""
            },
        )
    ],
    "t3a-ttl-cache-lru": [
        (
            "insertion-order eviction: get() does not count as a use",
            {
                "cache.py": """\
from collections import OrderedDict


class TTLCache:
    def __init__(self, ttl, clock, max_size=None):
        self.ttl = ttl
        self.clock = clock
        self.max_size = max_size
        self._data = OrderedDict()

    def set(self, key, value):
        if key in self._data:
            del self._data[key]
        self._data[key] = (value, self.clock())
        if self.max_size is not None:
            while len(self._data) > self.max_size:
                self._data.popitem(last=False)

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
"""
            },
        )
    ],
    "t3b-router-params": [
        (
            "dynamic routes matched before static ones",
            {
                "router.py": """\
class Router:
    def __init__(self):
        self._routes = []

    def add(self, path, handler):
        self._routes.append((path, handler))

    def resolve(self, path):
        parts = path.split("/")
        for route, handler in self._routes:
            route_parts = route.split("/")
            if len(route_parts) != len(parts):
                continue
            params = {}
            matched = True
            for expected, actual in zip(route_parts, parts):
                if expected.startswith("<") and expected.endswith(">"):
                    params[expected[1:-1]] = actual
                elif expected != actual:
                    matched = False
                    break
            if matched:
                return handler, params
        return None, {}
"""
            },
        ),
        (
            "no segment-count check (prefix match swallows extra segments)",
            {
                "router.py": """\
class Router:
    def __init__(self):
        self._routes = []

    def add(self, path, handler):
        self._routes.append((path, handler))

    def resolve(self, path):
        for route, handler in self._routes:
            if route == path:
                return handler, {}
        parts = [p for p in path.split("/") if p]
        for route, handler in self._routes:
            route_parts = [p for p in route.split("/") if p]
            params = {}
            matched = bool(route_parts)
            for index, expected in enumerate(route_parts):
                if index >= len(parts):
                    matched = False
                    break
                actual = parts[index]
                if expected.startswith("<") and expected.endswith(">"):
                    params[expected[1:-1]] = actual
                elif expected != actual:
                    matched = False
                    break
            if matched:
                return handler, params
        return None, {}
"""
            },
        ),
    ],
}


def _load_bench():
    """Import the harness module despite its hyphenated filename.

    The self-test deliberately reuses the harness's own `run_python` rather than
    shelling out itself. When these were two implementations they drifted: the
    harness ran verifiers under `python3 -I`, which strips the script directory
    from sys.path, so every verifier died with ModuleNotFoundError while this
    file — using a plain subprocess — reported all six fixtures healthy. Sharing
    the runner is what makes a green self-test mean the harness works.
    """
    path = Path(__file__).resolve().parent / "agentic-coding-bench.py"
    spec = importlib.util.spec_from_file_location("agentic_coding_bench", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["agentic_coding_bench"] = module
    spec.loader.exec_module(module)
    return module


_BENCH = _load_bench()


def run_verifier(files: dict[str, str], verifier: str) -> tuple[int, str]:
    sandbox = Path(tempfile.mkdtemp(prefix="fixture-check-"))
    try:
        for name, content in files.items():
            (sandbox / name).write_text(content)
        solved, out = _BENCH.run_verifier(sandbox, verifier)
        return (0 if solved else 1), out
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


def check_early_exit_bypass() -> int:
    """Every task must reject a target module that exits 0 before asserting.

    `raise SystemExit(0)` in the file under test makes the verifier's first
    import exit cleanly, so a returncode-only check scores all six tasks as
    solved without executing a single assertion. This is generic — it needs no
    knowledge of any task — so it is checked against every fixture rather than
    listed per-task in WRONG_SOLUTIONS.
    """
    failures = 0
    for task in TASKS:
        sandbox = Path(tempfile.mkdtemp(prefix="early-exit-"))
        try:
            for name, content in task.files.items():
                (sandbox / name).write_text(content)
            for name in task.target_files:
                (sandbox / name).write_text("raise SystemExit(0)\n")
            solved, _ = _BENCH.run_verifier(sandbox, task.verifier)
            if solved:
                print(f"[{task.task_id}] BYPASS: SystemExit(0) scored as solved")
                failures += 1
            else:
                print(f"[{task.task_id}] early-exit bypass rejected")
        finally:
            shutil.rmtree(sandbox, ignore_errors=True)
    return failures


def main() -> int:
    failures = 0
    for task in TASKS:
        reference = REFERENCE_SOLUTIONS.get(task.task_id)
        if reference is None:
            print(f"[{task.task_id}] NO REFERENCE SOLUTION — cannot validate")
            failures += 1
            continue

        code, out = run_verifier(task.files, task.verifier)
        if code == 0:
            print(
                f"[{task.task_id}] BROKEN FIXTURE: verifier passes on the unfixed repo"
            )
            failures += 1
        else:
            first = out.splitlines()[-1][:90] if out else "(no output)"
            print(f"[{task.task_id}] unfixed -> fails as expected ({first})")

        fixed = dict(task.files)
        fixed.update(reference)
        code, out = run_verifier(fixed, task.verifier)
        if code != 0:
            print(
                f"[{task.task_id}] BROKEN VERIFIER: reference solution fails\n{out[-600:]}"
            )
            failures += 1
        else:
            print(f"[{task.task_id}] reference -> passes")

        for label, wrong_files in WRONG_SOLUTIONS.get(task.task_id, []):
            hacked = dict(task.files)
            hacked.update(wrong_files)
            code, out = run_verifier(hacked, task.verifier)
            if code == 0:
                print(f"[{task.task_id}] FALSE POSITIVE: wrong fix passes — {label}")
                failures += 1
            else:
                print(f"[{task.task_id}] wrong fix rejected ({label})")

    print()
    failures += check_early_exit_bypass()

    covered = sum(len(v) for v in WRONG_SOLUTIONS.values())
    print(
        f"\n{len(TASKS)} tasks checked, {covered} adversarial fix(es), {failures} problem(s)"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
