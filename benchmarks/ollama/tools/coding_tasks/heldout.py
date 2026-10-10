"""Held-out tasks for checking whether agent instructions generalize."""

from coding_tasks import CodingTask

_H1A = '''\
"""Formatting helpers for report labels."""


def display_label(value):
    """Return a trimmed, title-cased label."""
    return " ".join(part.capitalize() for part in value.split(" "))
'''

# The shipped suite covers the whole request, so passing it is enough here.
_H1A_TESTS = """\
from labels import display_label


def test_collapses_spaces():
    assert display_label("  nightly   ingest ") == "Nightly Ingest"


def test_title_case_kept():
    assert display_label("API status") == "Api Status"


def test_empty():
    assert display_label("") == ""


def run():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("existing suite OK")


if __name__ == "__main__":
    run()
"""

_H1A_VERIFY = """\
from labels import display_label

assert display_label("  nightly   ingest ") == "Nightly Ingest"
assert display_label("API status") == "Api Status"
assert display_label("") == ""
print("OK")
"""

_H1B = '''\
"""Small path helpers used by the export command."""


def normalize_relative_path(path):
    """Collapse separators and dot segments without escaping the root."""
    parts = []
    for part in path.replace("\\\\", "/").split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            parts.pop()
        else:
            parts.append(part)
    return "/".join(parts)
'''

_H1B_VERIFY = """\
from paths import normalize_relative_path

assert normalize_relative_path("reports/./daily.csv") == "reports/daily.csv"
assert normalize_relative_path("reports\\\\daily.csv") == "reports/daily.csv"
assert normalize_relative_path("a/b/../c") == "a/c"
assert normalize_relative_path("../../a") == "a"
assert normalize_relative_path("") == ""
print("OK")
"""

_H1C = '''\
"""Parse a compact duration string into whole seconds."""


def parse_duration(value):
    """Parse values like '2h', '15m', or '30s'."""
    unit = value[-1]
    amount = int(value[:-1])
    scale = {"s": 1, "m": 60, "h": 3600}[unit]
    return amount * scale
'''

_H1C_VERIFY = """\
from durations import parse_duration

assert parse_duration("2h") == 7200
assert parse_duration("15m") == 900
assert parse_duration("30s") == 30
try:
    parse_duration("3d")
except ValueError:
    pass
else:
    raise AssertionError("unsupported units must raise ValueError")
print("OK")
"""

_H2A_KEYS = '''\
"""Shared cache-key construction."""


def cache_key(tenant, name):
    return f"{tenant}{name}".lower()
'''

_H2A_LOOKUP = """\
from cache_keys import cache_key


class MemoryStore:
    def __init__(self):
        self._items = {}

    def put(self, tenant, name, value):
        self._items[cache_key(tenant, name)] = value

    def get(self, tenant, name):
        return self._items.get(cache_key(tenant, name))
"""

_H2A_VERIFY = """\
from store import MemoryStore
from cache_keys import cache_key

s = MemoryStore()
s.put("team-a", "Report", 1)
s.put("team-b", "report", 2)
assert s.get("team-a", "report") == 1
assert s.get("team-b", "REPORT") == 2
assert cache_key("team-a", "Report") != cache_key("team-b", "Report")
s.put("a", ":b", 3)
s.put("a:", "b", 4)
assert s.get("a", ":b") == 3
assert s.get("a:", "b") == 4
print("OK")
"""

_H2B_SOURCE = '''\
"""Configuration decoding for the command line."""


def decode_settings(raw):
    result = {}
    for line in raw.splitlines():
        if line.strip():
            key, value = line.split("=", 1)
            result.setdefault(key.strip(), value.strip())
    return result
'''

_H2B_VERIFY = """\
from settings import decode_settings

assert decode_settings("mode = fast\\n empty =  ") == {"mode": "fast", "empty": ""}
assert decode_settings("mode=one\\nmode=two") == {"mode": "two"}
try:
    decode_settings("broken")
except ValueError:
    pass
else:
    raise AssertionError("malformed lines must raise ValueError")
print("OK")
"""

_H2C_SOURCE = '''\
"""CSV field helpers for the archive writer."""


def decode_field(value):
    return value.replace("\\\\n", "\\n")
'''

_H2C_VERIFY = """\
import importlib
import types
import unittest
from pathlib import Path

from fields import decode_field

assert decode_field(r"line1\\nline2") == r"line1\\nline2"
test_file = Path("test_fields_regression.py")
assert test_file.is_file(), "add the requested regression test"
suite = unittest.defaultTestLoader.loadTestsFromName("test_fields_regression")
result = unittest.TextTestRunner().run(suite)
assert result.wasSuccessful(), "the submitted regression test must pass"
test_module = importlib.import_module(test_file.stem)
original_module = types.ModuleType("fields")
original_module.decode_field = lambda value: value.replace(chr(92) + "n", "\\n")
test_module.decode_field = original_module.decode_field
original_result = unittest.TextTestRunner().run(
    unittest.defaultTestLoader.loadTestsFromModule(test_module)
)
assert not original_result.wasSuccessful(), "regression test must fail against the original bug"
print("OK")
"""

_H3A_LEDGER = '''\
"""Append-only in-memory audit ledger."""


class Ledger:
    def __init__(self):
        self._entries = []

    def append(self, event):
        self._entries.append(event)

    def entries(self):
        return list(self._entries)
'''

_H3A_TESTS = """\
from ledger import Ledger


def test_entries_are_copied():
    ledger = Ledger()
    ledger.append("created")
    assert ledger.entries() == ["created"]


def run():
    test_entries_are_copied()
    print("existing suite OK")


if __name__ == "__main__":
    run()
"""

_H3A_VERIFY = """\
import test_ledger
from ledger import Ledger

test_ledger.run()
ledger = Ledger()
ledger.append("a")
ledger.append("b")
first = ledger.entries()
first.append("forged")
assert ledger.entries() == ["a", "b"]
assert ledger.entries() == ["a", "b"]
assert ledger.entries(since=1) == ["b"]
assert ledger.entries(limit=1) == ["a"]
assert ledger.entries(since=1, limit=0) == []
for kwargs in ({"since": -1}, {"limit": -1}):
    try:
        ledger.entries(**kwargs)
    except ValueError:
        pass
    else:
        raise AssertionError("negative indices and limits must fail")
print("OK")
"""

_H3B_SOURCE = '''\
"""Helpers for collecting environment settings."""


def collect_settings(environment, prefix="APP_"):
    return {key[len(prefix):].lower(): value
            for key, value in environment.items() if key.startswith(prefix)}
'''

_H3B_TESTS = """\
from env_settings import collect_settings


def test_prefixed_values():
    assert collect_settings({"APP_MODE": "fast", "OTHER": "x"}) == {"mode": "fast"}


def run():
    test_prefixed_values()
    print("existing suite OK")


if __name__ == "__main__":
    run()
"""

_H3B_VERIFY = """\
import test_env_settings
from env_settings import collect_settings

test_env_settings.run()
env = {"APP_MODE": "fast", "APP_empty": "", "OTHER": "skip"}
assert collect_settings(env) == {"mode": "fast", "empty": ""}
assert collect_settings(env, prefix="OTHER") == {"": "skip"}
assert collect_settings(env, allowlist={"MODE"}) == {"mode": "fast"}
assert collect_settings(env, transform=str.upper) == {"mode": "FAST", "empty": ""}
assert collect_settings({}, prefix="MISSING_") == {}
print("OK")
"""

_H3C_SOURCE = '''\
"""Plan tasks in stable priority order."""


def plan(items):
    return sorted(items, key=lambda item: item["priority"])
'''

_H3C_TESTS = """\
from planner import plan


def test_priority():
    assert [item["id"] for item in plan([
        {"id": "low", "priority": 9}, {"id": "high", "priority": 1}
    ])] == ["high", "low"]


def run():
    test_priority()
    print("existing suite OK")


if __name__ == "__main__":
    run()
"""

_H3C_VERIFY = """\
import test_planner
from planner import plan

test_planner.run()
items = [
    {"id": "b", "priority": 2}, {"id": "a", "priority": 1},
    {"id": "c", "priority": 2},
]
assert [x["id"] for x in plan(items)] == ["a", "b", "c"]
assert [x["id"] for x in plan(items, reverse=True)] == ["b", "c", "a"]
assert [x["id"] for x in plan(items, reverse=True, limit=2)] == ["b", "c"]
assert [x["id"] for x in plan(items, limit=0)] == []
print("OK")
"""


HELDOUT_TASKS: list[CodingTask] = [
    CodingTask(
        "h1a-label-spacing",
        1,
        "Collapse repeated label spaces",
        "`display_label` in labels.py leaves awkward gaps when a label contains repeated or leading spaces. Make it return one space between words while preserving its current title casing.",
        {"labels.py": _H1A, "test_labels.py": _H1A_TESTS},
        _H1A_VERIFY,
        ["labels.py"],
        8,
    ),
    CodingTask(
        "h1b-relative-path",
        1,
        "Normalize relative paths",
        "Fix `normalize_relative_path` in paths.py: it should collapse dot segments and accept either slash style. Keep normalized paths relative to their starting root.",
        {"paths.py": _H1B},
        _H1B_VERIFY,
        ["paths.py"],
        8,
    ),
    CodingTask(
        "h1c-duration-unit",
        1,
        "Reject unknown duration units cleanly",
        "`parse_duration` in durations.py crashes with a KeyError for an unsupported unit. Raise ValueError for unsupported units, while continuing to parse seconds, minutes, and hours.",
        {"durations.py": _H1C},
        _H1C_VERIFY,
        ["durations.py"],
        8,
    ),
    CodingTask(
        "h2a-tenant-key-collision",
        2,
        "Keep tenant cache entries isolated",
        "A report stored for one tenant can show up for another tenant when their names differ only by tenant. The lookup symptom is in store.py. Trace how keys are built and fix the collision without making names case-sensitive.",
        {"store.py": _H2A_LOOKUP, "cache_keys.py": _H2A_KEYS},
        _H2A_VERIFY,
        ["cache_keys.py"],
        14,
    ),
    CodingTask(
        "h2b-settings-parser",
        2,
        "Decode settings lines robustly",
        "Our CLI mishandles blank values and duplicate settings. Inspect settings.py and correct the decoder: trim keys and values, allow an empty value, let the last duplicate win, and reject malformed nonblank lines with ValueError. Add any helper module you need.",
        {"settings.py": _H2B_SOURCE},
        _H2B_VERIFY,
        ["settings.py", "settings_parser.py"],
        14,
    ),
    CodingTask(
        "h2c-field-regression-test",
        2,
        "Preserve escaped newlines in fields",
        "`decode_field` in fields.py turns the two characters backslash-n into an actual newline, corrupting exported data. Fix the behavior and add a new regression test file named `test_fields_regression.py`; the test must import `decode_field` and demonstrate the original bug.",
        {"fields.py": _H2C_SOURCE},
        _H2C_VERIFY,
        ["fields.py", "test_fields_regression.py"],
        14,
    ),
    CodingTask(
        "h3a-ledger-snapshots",
        3,
        "Add safe ledger snapshots",
        "Extend `Ledger` in ledger.py with `entries(since=0, limit=None)`. Return an independent list snapshot, start at the requested zero-based index, and cap its length when a limit is supplied. Negative `since` and negative `limit` should raise ValueError; existing tests in test_ledger.py must continue to pass.",
        {"ledger.py": _H3A_LEDGER, "test_ledger.py": _H3A_TESTS},
        _H3A_VERIFY,
        ["ledger.py"],
        20,
    ),
    CodingTask(
        "h3b-env-filtering",
        3,
        "Add safe environment setting filters",
        "Extend `collect_settings` in env_settings.py with optional `allowlist` and `transform` arguments. Keep only keys matching the prefix, return lowercase suffix keys, optionally restrict suffixes to the allowlist, and apply the transform callable to each retained value. Keep empty values and the existing default behavior; the test_env_settings.py suite must still pass.",
        {"env_settings.py": _H3B_SOURCE, "test_env_settings.py": _H3B_TESTS},
        _H3B_VERIFY,
        ["env_settings.py"],
        20,
    ),
    CodingTask(
        "h3c-stable-task-planner",
        3,
        "Add direction and result limits to planning",
        "Extend `plan` in planner.py with `reverse=False` and `limit=None`. Sort by priority, preserve input order among equal priorities in either direction, then apply the limit. A limit of zero returns no items, and omitting both arguments keeps today's behavior. The test_planner.py suite must remain green.",
        {"planner.py": _H3C_SOURCE, "test_planner.py": _H3C_TESTS},
        _H3C_VERIFY,
        ["planner.py"],
        20,
    ),
]
