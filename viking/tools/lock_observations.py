"""Decode ov-exporter evidence without choosing reporting or recovery policy.

This is the exporter's text format, not a general Prometheus client. A valid
lock selection can be empty, but is always limited to the producer's top N
locks past its minimum age. Absence never proves a lock does not exist.
Transport, freshness thresholds, S3 checks and restart decisions stay in callers.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

LOCK_METRIC = "openviking_s3_ovlock_live_age_seconds"
_SCALARS = {
    "openviking_up": "up",
    "openviking_last_poll_unix": "last_poll",
    "openviking_s3_ovlock_poll_ok": "lock_poll_ok",
    "openviking_s3_ovlock_oldest_live_age_seconds": "oldest_age",
}
_QUEUES = {
    "openviking_queue_pending": "pending",
    "openviking_queue_in_progress": "in_progress",
}
_SAMPLE = re.compile(r"^([A-Za-z_:][A-Za-z0-9_:]*)(?:\{(.*)\})?\s+(\S+)$")
_NAME = re.compile(r"[A-Za-z_:][A-Za-z0-9_:]*")
_LABEL = re.compile(
    r'\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"((?:[^"\\\n]|\\["\\n])*)"\s*(?:,|$)'
)
_UNESCAPE = {"\\\\": "\\", '\\"': '"', "\\n": "\n"}


def _parse_labels(block: str) -> dict[str, str]:
    labels = {}
    pos = 0
    while pos < len(block):
        match = _LABEL.match(block, pos)
        if match is None:
            raise ValueError("malformed label block or unsupported escape")
        name, raw = match.groups()
        if name in labels:
            raise ValueError(f"duplicate label {name!r}")
        labels[name] = re.sub(r"\\.", lambda m: _UNESCAPE[m[0]], raw)
        pos = match.end()
    return labels


@dataclass(frozen=True)
class Diagnostic:
    field: str
    line: int | None
    reason: str

    def __str__(self) -> str:
        location = f"line {self.line}: " if self.line is not None else ""
        return f"{location}{self.field}: {self.reason}"


@dataclass(frozen=True)
class LockSample:
    key: str
    uri: str
    owner: str
    lock_type: str
    age: float


@dataclass(frozen=True)
class Observation:
    source: str
    observed_at: float | None
    # valid / missing / malformed; None never means an empty selection.
    lock_status: str
    locks: tuple[LockSample, ...] | None
    diagnostics: tuple[Diagnostic, ...]
    up: bool | None
    lock_poll_ok: bool | None
    last_poll: float | None
    oldest_age: float | None
    pending: dict[str, float]
    in_progress: dict[str, float]
    coverage: str = "exporter-selected: top N, past a minimum age"

    def require_locks(self) -> tuple[LockSample, ...]:
        if self.locks is None:
            details = "; ".join(str(d) for d in self.diagnostics if d.field == "locks")
            raise ValueError(f"{self.source}: {details}")
        return self.locks


def observe_exporter(
    text: str, *, source: str = "exporter", observed_at: float | None = None
) -> Observation:
    """One scrape's facts, with all lock rows discarded if any row is invalid.

    Keep diagnostics for each affected field. Bad queue/scalar evidence does
    not invalidate independent lock rows; callers choose which fields they
    require. Producer health and poll time are facts, not freshness policy.
    """
    diagnostics = []
    scalars = {}
    queues = {"pending": {}, "in_progress": {}}
    locks = []
    keys = set()
    declared = False

    # Exposition records end at LF. splitlines() would also split valid UTF-8
    # label characters such as U+0085 and U+2028 into invented records.
    for number, raw in enumerate(text.split("\n"), 1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            parts = line.split()
            if len(parts) >= 3 and parts[:3] == ["#", "TYPE", LOCK_METRIC]:
                if declared or parts != ["#", "TYPE", LOCK_METRIC, "gauge"]:
                    diagnostics.append(
                        Diagnostic(
                            "locks", number, "invalid or duplicate family declaration"
                        )
                    )
                declared = True
            continue
        name_match = _NAME.match(line)
        name = name_match[0] if name_match else ""
        field = (
            "locks" if name == LOCK_METRIC else _SCALARS.get(name) or _QUEUES.get(name)
        )
        if field is None:
            continue
        try:
            match = _SAMPLE.fullmatch(line)
            if match is None:
                raise ValueError("malformed sample")
            labels = _parse_labels(match[2]) if match[2] is not None else {}
            try:
                value = float(match[3])
            except ValueError as e:
                raise ValueError("non-numeric value") from e
            if not math.isfinite(value):
                raise ValueError("non-finite value")
            if field == "locks":
                key = labels.get("key", "")
                if not key:
                    raise ValueError("missing or empty key label")
                if key in keys:
                    raise ValueError(f"duplicate lock key {key!r}")
                if value < 0:
                    raise ValueError("negative presence age")
                keys.add(key)
                locks.append(
                    LockSample(
                        key,
                        labels.get("uri", "unknown"),
                        labels.get("owner", "unknown"),
                        labels.get("lock_type", "unknown"),
                        value,
                    )
                )
            elif field in queues:
                queue = labels.get("queue", "")
                if not queue:
                    raise ValueError("missing or empty queue label")
                if queue in queues[field]:
                    raise ValueError(f"duplicate queue {queue!r}")
                queues[field][queue] = value
            else:
                if labels or field in scalars:
                    raise ValueError("labelled or duplicate scalar")
                scalars[field] = value
        except ValueError as e:
            diagnostics.append(Diagnostic(field, number, f"{e}; sample={line[:160]!r}"))

    if not declared:
        diagnostics.append(
            Diagnostic(
                "locks",
                None,
                "missing lock family declaration (exporter predates homelab#205 or scrape is incomplete)",
            )
        )
    lock_errors = [d for d in diagnostics if d.field == "locks"]
    status = (
        "malformed"
        if any(d.line is not None for d in lock_errors)
        else "missing"
        if not declared
        else "valid"
    )
    # A later good row must not silently repair an ambiguous scalar or queue.
    for diagnostic in diagnostics:
        scalars.pop(diagnostic.field, None)
        if diagnostic.field in queues:
            queues[diagnostic.field].clear()
    return Observation(
        source=source,
        observed_at=observed_at,
        lock_status=status,
        locks=None if lock_errors else tuple(locks),
        diagnostics=tuple(diagnostics),
        up=None if "up" not in scalars else scalars["up"] == 1,
        lock_poll_ok=None
        if "lock_poll_ok" not in scalars
        else scalars["lock_poll_ok"] == 1,
        last_poll=scalars.get("last_poll"),
        oldest_age=scalars.get("oldest_age"),
        pending=queues["pending"],
        in_progress=queues["in_progress"],
    )
