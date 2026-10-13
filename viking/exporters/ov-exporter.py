#!/usr/bin/env python3
"""Prometheus exporter for the OpenViking stack.

Polls `ov status --output json` for queue + vectordb + VLM telemetry, plus
`find /app/data/viking -name '.path.ovlock'` for stale lock count. Exposes
Prometheus text format on :9210.

Designed to run as a sidecar in the openviking Deployment using the OV
image itself (the `ov` CLI is already on PATH). No external dependencies
beyond Python stdlib.

Auth model: `ov status` is an admin/status endpoint; it accepts the root
API key via `OPENVIKING_KEY` env var. User-scoped keys + multi-tenant
identity headers are only required for tenant data APIs (mkdir, ls,
add-resource, etc.) which this exporter does not call.

Env:
    OPENVIKING_URL     OV server URL (default: http://localhost:1933)
    OPENVIKING_KEY     OV root API key (required for `ov status`)
    AGFS_PATH          Path to AGFS data root for lock count
                       (default: /app/data/viking)
    POLL_INTERVAL      Seconds between polls (default: 15)
    PORT               HTTP port for /metrics (default: 9210)

S3 lock polling (IMPR-1095) — enabled when OVLOCK_S3_BUCKET is set. Under
`agfs.backend: s3` the `.ovlock` files live in the Garage bucket, not on the
PVC, so the find-based openviking_lock_path_ovlock_count above is blind there
(the BUG-1039 269-stale-file drift went unseen). Uses the stdlib s3lite
module mounted alongside this script; no SDK exists in the OV image.

    OVLOCK_S3_BUCKET               Bucket to scan (e.g. openviking-agfs)
    OVLOCK_S3_ENDPOINT             S3 endpoint URL
                                   (default: http://garage.garage.svc.cluster.local:3900)
    OVLOCK_S3_REGION               SigV4 region (default: garage)
    AWS_ACCESS_KEY_ID              S3 credentials (openviking-s3-credentials)
    AWS_SECRET_ACCESS_KEY          S3 credentials
    OVLOCK_LIVE_WINDOW_SECONDS     mtime age at/below which a lock counts as
                                   live-refreshing (default: 120 = 4x the 30s
                                   lease refresh cadence)
    OVLOCK_DEAD_THRESHOLD_SECONDS  mtime age above which a lock counts as a
                                   dead lease (default: 1800)
    OVLOCK_LABEL_TOP_N             how many of the longest-present live locks
                                   get a per-lock labelled series (default: 3)
    OVLOCK_LABEL_MIN_AGE_SECONDS   presence age a live lock needs before it
                                   gets one (default: 900)

Per-lock wedge series (BUG-1220): openviking_s3_ovlock_live_age_seconds
carries key, uri, owner and lock_type labels so OpenVikingWedgedLockFile can
say WHICH lock is wedged. Cardinality is bounded twice over: only the top N
live locks by presence age, and only once they have been present for the
minimum age, so a sync burst's short-lived locks never mint series. One GET
per labelled lock per poll reads the owner from the lock body.

The top-N bound is also a coverage bound: with more than N locks wedged at
once, the extras get no labelled series, so the alert names only N of them
(the unlabelled fallback arm stays muted while any labelled instance fires)
and the janitor reports the rest as `presence_age=not-exported`.

Presence age measures one holder's tenure. When a labelled lock's body
names a different owner or lock type than the last read (released and
re-acquired between two polls, so the key never left the bucket), its first
sighting restarts, so the series never pairs a new holder with the old
holder's age. Locks are read only once labelled, so a hand-off before the
minimum age still counts from the first sighting (at most min_age extra).
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import s3lite  # mounted/committed alongside this script

# ── Metrics State ────────────────────────────────────────────────────────────

_lock = threading.Lock()
_metrics: dict[str, object] = {
    "openviking_up": 0,  # 1 if ov_status + lock count both succeeded last poll
    "openviking_last_poll_unix": 0.0,
    # Per-queue gauges, keyed by queue name
    "openviking_queue_pending": {},  # {queue: int}
    "openviking_queue_in_progress": {},  # {queue: int}
    "openviking_queue_processed_total": {},  # {queue: int}
    "openviking_queue_requeued_total": {},  # {queue: int}
    "openviking_queue_errors_total": {},  # {queue: int}
    # VLM/embedder per-model counters from `ov status` models section
    "openviking_model_calls_total": {},  # {model: int}
    "openviking_model_prompt_tokens_total": {},  # {model: int}
    "openviking_model_completion_tokens_total": {},  # {model: int}
    # VikingDB
    "openviking_vectordb_index_count": {},  # {collection: int}
    "openviking_vectordb_vector_count": {},  # {collection: int}
    # AGFS lock count + lifecycle lock count
    "openviking_lock_path_ovlock_count": 0,
    "openviking_lifecycle_lock_active": 0,
    # S3 bucket-level .ovlock classification (IMPR-1095); values hold their
    # last good reading when a poll is skipped/fails (poll_ok flags it).
    "openviking_s3_ovlock_total": 0,
    "openviking_s3_ovlock_dead": 0,
    "openviking_s3_ovlock_live_refreshing": 0,
    "openviking_s3_ovlock_oldest_live_age_seconds": 0.0,
    "openviking_s3_ovlock_poll_ok": 0,
    # BUG-1220: [(labels, presence_age_seconds)] for the top-N longest-present
    # live locks — names the wedged lock for the alert.
    "openviking_s3_ovlock_live_age_seconds": [],
}

POLL_TIMEOUT_S = 10


# ── Poll Sources ─────────────────────────────────────────────────────────────


def _run(cmd: list[str], timeout: int = POLL_TIMEOUT_S) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _parse_tables(text: str) -> list[list[dict[str, str]]]:
    """Parse OV's ASCII pipe-rendered tables out of a `status` string.

    Returns a list of tables; each table is a list of row dicts keyed by the
    column header. Separator lines (made of + and -) are skipped. Multiple
    consecutive tables in the same string are returned as separate tables.
    """
    tables: list[list[dict[str, str]]] = []
    header: list[str] | None = None
    current: list[dict[str, str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            # Truly blank line: close current table
            if header is not None and current:
                tables.append(current)
                current = []
            header = None
            continue
        if stripped[0] == "+":
            # Separator line: ignore (don't touch header/current state)
            continue
        if stripped[0] != "|":
            # Heading / non-table line (e.g. "Embedding Models:"): close current table
            if header is not None and current:
                tables.append(current)
                current = []
            header = None
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if header is None:
            header = cells
            continue
        current.append(dict(zip(header, cells)))
    if header is not None and current:
        tables.append(current)
    return tables


def _to_int(s: str | None) -> int | None:
    if s is None:
        return None
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def poll_ov_status() -> bool:
    """Poll `ov status --output json` and update queue/vectordb/model/lock metrics.

    OV's JSON output wraps the ASCII pipe tables under
    `result.components.{queue,vikingdb,models,lock}.status`; the structured
    metric extraction happens here by parsing those tables.
    """
    try:
        result = _run(["ov", "status", "--output", "json"])
        if result.returncode != 0:
            return False
        data = json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return False

    components = (data.get("result") or {}).get("components") or {}

    # Queue: one table with columns Queue | Pending | In Progress | Processed | Requeued | Errors | Total
    new_pending: dict[str, int] = {}
    new_in_progress: dict[str, int] = {}
    new_processed: dict[str, int] = {}
    new_requeued: dict[str, int] = {}
    new_errors: dict[str, int] = {}
    queue_status = (components.get("queue") or {}).get("status") or ""
    for table in _parse_tables(queue_status):
        for row in table:
            name = row.get("Queue", "").lower().replace("-", "_").replace(" ", "_")
            if not name:
                continue
            p = _to_int(row.get("Pending"))
            i = _to_int(row.get("In Progress"))
            pr = _to_int(row.get("Processed"))
            rq = _to_int(row.get("Requeued"))
            er = _to_int(row.get("Errors"))
            if p is not None:
                new_pending[name] = p
            if i is not None:
                new_in_progress[name] = i
            if pr is not None:
                new_processed[name] = pr
            if rq is not None:
                new_requeued[name] = rq
            if er is not None:
                new_errors[name] = er

    # VikingDB: one table with columns Collection | Index Count | Vector Count | Status
    new_index_count: dict[str, int] = {}
    new_vector_count: dict[str, int] = {}
    vdb_status = (components.get("vikingdb") or {}).get("status") or ""
    for table in _parse_tables(vdb_status):
        for row in table:
            coll = row.get("Collection", "").lower()
            if not coll:
                continue
            ic = _to_int(row.get("Index Count"))
            vc = _to_int(row.get("Vector Count"))
            if ic is not None:
                new_index_count[coll] = ic
            if vc is not None:
                new_vector_count[coll] = vc

    # Models: multiple tables (Embedding Models, VLM Models) each with
    # columns Model | Provider | Calls | Prompt | Completion | Total | Last Updated
    new_calls: dict[str, int] = {}
    new_prompt_tokens: dict[str, int] = {}
    new_completion_tokens: dict[str, int] = {}
    models_status = (components.get("models") or {}).get("status") or ""
    for table in _parse_tables(models_status):
        for row in table:
            model = row.get("Model", "")
            if not model:
                continue
            c = _to_int(row.get("Calls"))
            pt = _to_int(row.get("Prompt"))
            ct = _to_int(row.get("Completion"))
            if c is not None:
                new_calls[model] = c
            if pt is not None:
                new_prompt_tokens[model] = pt
            if ct is not None:
                new_completion_tokens[model] = ct

    # Lifecycle locks: TOTAL row shows active lock count.
    # Columns: Handle ID | Locks | Duration | Idle | Created
    new_lock_count = 0
    lock_status = (components.get("lock") or {}).get("status") or ""
    for table in _parse_tables(lock_status):
        for row in table:
            handle = row.get("Handle ID", "")
            if handle.startswith("TOTAL"):
                v = _to_int(row.get("Locks"))
                if v is not None:
                    new_lock_count = v
                break

    with _lock:
        _metrics["openviking_queue_pending"] = new_pending
        _metrics["openviking_queue_in_progress"] = new_in_progress
        _metrics["openviking_queue_processed_total"] = new_processed
        _metrics["openviking_queue_requeued_total"] = new_requeued
        _metrics["openviking_queue_errors_total"] = new_errors
        _metrics["openviking_vectordb_index_count"] = new_index_count
        _metrics["openviking_vectordb_vector_count"] = new_vector_count
        _metrics["openviking_model_calls_total"] = new_calls
        _metrics["openviking_model_prompt_tokens_total"] = new_prompt_tokens
        _metrics["openviking_model_completion_tokens_total"] = new_completion_tokens
        _metrics["openviking_lifecycle_lock_active"] = new_lock_count

    return True


# Wall-clock timestamp of the first sighting of each live-refreshing lock key,
# for the oldest_live_age gauge (a wedge's mtime never ages, so its *presence
# duration* is the only signal that it has outlived plausible work — BUG-1039).
# Reset on exporter restart: the wedge alert just re-accumulates from zero.
# Also reset when a labelled lock changes holder (BUG-1220, holder_changed).
_s3_first_seen: dict[str, float] = {}

# Last successfully parsed lock token per live key (BUG-1220). Used when a
# GET of a labelled lock fails, so a transient read error does not flip the
# series' owner label (which would resolve and re-fire the alert), and to
# detect a holder change on a key that never left the bucket.
_s3_token_cache: dict[str, dict] = {}

UNKNOWN = "unknown"


def select_label_candidates(
    first_seen: dict[str, float], now: float, top_n: int, min_age: float
) -> list[tuple[str, float]]:
    """The live locks that get a labelled series: [(key, presence_age)].

    Longest-present first (key as tiebreak, for a stable order), at most
    top_n, and only those present for at least min_age seconds.
    """
    if top_n <= 0:
        return []
    aged = [(key, now - t) for key, t in first_seen.items() if now - t >= min_age]
    aged.sort(key=lambda kv: (-kv[1], kv[0]))
    return aged[:top_n]


def lock_labels(key: str, token: dict | None) -> dict[str, str]:
    """Label set for one lock's wedge series. `token` from parse_lock_token."""
    return {
        "key": key,
        "uri": s3lite.lock_target(key) or UNKNOWN,
        "owner": (token or {}).get("owner") or UNKNOWN,
        "lock_type": (token or {}).get("lock_type") or UNKNOWN,
    }


def holder_changed(previous: dict | None, current: dict | None) -> bool:
    """True when two parsed tokens of one key name different holders.

    A holder is (owner, lock_type). Either side missing (never read, or
    unreadable with no cached fallback) is not evidence of a change.
    """
    if not previous or not current:
        return False
    return (previous.get("owner"), previous.get("lock_type")) != (
        current.get("owner"),
        current.get("lock_type"),
    )


def _read_token(client, bucket: str, key: str) -> dict | None:
    """Parsed token for key, falling back to the last good read; None if never."""
    try:
        token = s3lite.parse_lock_token(client.get_object(bucket, key))
    except Exception as e:  # noqa: BLE001 - label falls back, poll carries on
        print(f"s3 lock token read failed for {key}: {e}", flush=True)
        token = None
    if token is not None:
        _s3_token_cache[key] = token
        return token
    return _s3_token_cache.get(key)


def poll_s3_locks(
    client,
    bucket: str,
    live_window: float,
    dead_threshold: float,
    ov_up: bool,
    top_n: int = 3,
    min_age: float = 900.0,
) -> bool:
    """Classify .ovlock objects in the S3 AGFS bucket by mtime age.

    age = now - LastModified. The lease refresh loop rewrites the object every
    ~30s while its owner lives, so: age <= live_window -> live-refreshing;
    age > dead_threshold -> dead lease (refresh loop gone); in between ->
    fresh/pending (left uncounted in either bucket). Skips classification
    entirely (poll_ok=0, gauges hold) when OV is down: every lock looks
    frozen during a restart, and v0.4.10 may be about to resume that work.
    """
    if not ov_up:
        with _lock:
            _metrics["openviking_s3_ovlock_poll_ok"] = 0
        return False
    try:
        objects = client.list_objects(bucket)
    except Exception as e:  # noqa: BLE001 - any S3 failure just flags the poll
        print(f"s3 lock poll failed: {e}", flush=True)
        with _lock:
            _metrics["openviking_s3_ovlock_poll_ok"] = 0
        return False

    now = time.time()
    total = dead = live = 0
    live_keys: set[str] = set()
    for obj in objects:
        if not s3lite.is_lock_key(obj["Key"]):
            continue
        total += 1
        age = now - obj["LastModified"].timestamp()
        if age <= live_window:
            live += 1
            live_keys.add(obj["Key"])
            _s3_first_seen.setdefault(obj["Key"], now)
        elif age > dead_threshold:
            dead += 1
    for key in list(_s3_first_seen):
        if key not in live_keys:
            del _s3_first_seen[key]
    for key in list(_s3_token_cache):
        if key not in live_keys:
            del _s3_token_cache[key]

    labelled = []
    for key, age in select_label_candidates(_s3_first_seen, now, top_n, min_age):
        previous = _s3_token_cache.get(key)
        token = _read_token(client, bucket, key)
        if holder_changed(previous, token):
            # Same key, new holder: restart presence so the series (and the
            # alert instance) never carries the old holder's age. The slot
            # refills from the next-oldest lock on the next poll.
            print(
                f"s3 lock {key} changed holder "
                f"{previous['owner']}:{previous['lock_type']} -> "
                f"{token['owner']}:{token['lock_type']}; presence restarts",
                flush=True,
            )
            _s3_first_seen[key] = now
            continue
        labelled.append((lock_labels(key, token), round(age, 1)))
    oldest_live_age = max((now - t for t in _s3_first_seen.values()), default=0.0)

    with _lock:
        _metrics["openviking_s3_ovlock_live_age_seconds"] = labelled
        _metrics["openviking_s3_ovlock_total"] = total
        _metrics["openviking_s3_ovlock_dead"] = dead
        _metrics["openviking_s3_ovlock_live_refreshing"] = live
        _metrics["openviking_s3_ovlock_oldest_live_age_seconds"] = round(
            oldest_live_age, 1
        )
        _metrics["openviking_s3_ovlock_poll_ok"] = 1
    return True


def poll_lock_count(agfs_path: str) -> bool:
    """Count .path.ovlock files under AGFS data root."""
    try:
        result = _run(
            ["find", agfs_path, "-name", ".path.ovlock", "-type", "f"], timeout=5
        )
        if result.returncode != 0:
            return False
        count = sum(1 for line in result.stdout.splitlines() if line.strip())
    except (subprocess.TimeoutExpired, OSError):
        return False

    with _lock:
        _metrics["openviking_lock_path_ovlock_count"] = count
    return True


# ── Metrics Format ───────────────────────────────────────────────────────────


def escape_label_value(value: str) -> str:
    """Prometheus text-format label escaping: backslash, quote, newline."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def format_labels(labels: dict[str, str]) -> str:
    """`{a="x",b="y"}` in key order, values escaped; "" for no labels."""
    if not labels:
        return ""
    inner = ",".join(f'{k}="{escape_label_value(v)}"' for k, v in labels.items())
    return "{" + inner + "}"


def format_metrics() -> str:
    lines: list[str] = []

    def gauge(name: str, help_text: str, value) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} gauge")
        if isinstance(value, dict):
            for label, v in sorted(value.items()):
                safe = label.replace('"', '\\"')
                lines.append(f'{name}{{queue="{safe}"}} {v}')
        else:
            lines.append(f"{name} {value}")

    def labeled_gauge(name: str, help_text: str, label_name: str, value) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} gauge")
        if isinstance(value, dict):
            for label, v in sorted(value.items()):
                safe = label.replace('"', '\\"')
                lines.append(f'{name}{{{label_name}="{safe}"}} {v}')

    def multi_labeled_gauge(name: str, help_text: str, samples) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} gauge")
        for labels, v in samples:
            lines.append(f"{name}{format_labels(labels)} {v}")

    def counter(name: str, help_text: str, label_name: str, value) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} counter")
        if isinstance(value, dict):
            for label, v in sorted(value.items()):
                safe = label.replace('"', '\\"')
                lines.append(f'{name}{{{label_name}="{safe}"}} {v}')

    with _lock:
        gauge(
            "openviking_up",
            "1 if last poll cycle succeeded, 0 otherwise.",
            _metrics["openviking_up"],
        )
        gauge(
            "openviking_last_poll_unix",
            "Unix timestamp of last poll cycle.",
            _metrics["openviking_last_poll_unix"],
        )
        labeled_gauge(
            "openviking_queue_pending",
            "Pending items per OV queue.",
            "queue",
            _metrics["openviking_queue_pending"],
        )
        labeled_gauge(
            "openviking_queue_in_progress",
            "In-progress items per OV queue.",
            "queue",
            _metrics["openviking_queue_in_progress"],
        )
        counter(
            "openviking_queue_processed_total",
            "Lifetime processed items per OV queue.",
            "queue",
            _metrics["openviking_queue_processed_total"],
        )
        counter(
            "openviking_queue_requeued_total",
            "Lifetime requeued items per OV queue.",
            "queue",
            _metrics["openviking_queue_requeued_total"],
        )
        counter(
            "openviking_queue_errors_total",
            "Lifetime errored items per OV queue.",
            "queue",
            _metrics["openviking_queue_errors_total"],
        )
        labeled_gauge(
            "openviking_vectordb_index_count",
            "VikingDB index count per collection.",
            "collection",
            _metrics["openviking_vectordb_index_count"],
        )
        labeled_gauge(
            "openviking_vectordb_vector_count",
            "VikingDB vector count per collection.",
            "collection",
            _metrics["openviking_vectordb_vector_count"],
        )
        counter(
            "openviking_model_calls_total",
            "Lifetime model invocations per VLM/embedding model.",
            "model",
            _metrics["openviking_model_calls_total"],
        )
        counter(
            "openviking_model_prompt_tokens_total",
            "Lifetime prompt tokens per model.",
            "model",
            _metrics["openviking_model_prompt_tokens_total"],
        )
        counter(
            "openviking_model_completion_tokens_total",
            "Lifetime completion tokens per model.",
            "model",
            _metrics["openviking_model_completion_tokens_total"],
        )
        gauge(
            "openviking_lock_path_ovlock_count",
            "Count of stale .path.ovlock files in AGFS data root.",
            _metrics["openviking_lock_path_ovlock_count"],
        )
        gauge(
            "openviking_lifecycle_lock_active",
            "Count of active lifecycle locks from ov status lock component.",
            _metrics["openviking_lifecycle_lock_active"],
        )
        gauge(
            "openviking_s3_ovlock_total",
            "Count of .ovlock objects in the S3 AGFS bucket.",
            _metrics["openviking_s3_ovlock_total"],
        )
        gauge(
            "openviking_s3_ovlock_dead",
            "S3 .ovlock objects whose mtime is older than the dead threshold (lease refresh gone).",
            _metrics["openviking_s3_ovlock_dead"],
        )
        gauge(
            "openviking_s3_ovlock_live_refreshing",
            "S3 .ovlock objects with mtime inside the live window (lease actively refreshed).",
            _metrics["openviking_s3_ovlock_live_refreshing"],
        )
        gauge(
            "openviking_s3_ovlock_oldest_live_age_seconds",
            "Longest continuous presence of any live-refreshing lock (wedge signal, BUG-1039).",
            _metrics["openviking_s3_ovlock_oldest_live_age_seconds"],
        )
        multi_labeled_gauge(
            "openviking_s3_ovlock_live_age_seconds",
            "Presence age of the longest-present live-refreshing locks, labelled "
            "with key/uri/owner/lock_type (top N, past a minimum age; BUG-1220).",
            _metrics["openviking_s3_ovlock_live_age_seconds"],
        )
        gauge(
            "openviking_s3_ovlock_poll_ok",
            "1 if the last S3 lock poll ran with OV healthy, 0 if skipped or failed.",
            _metrics["openviking_s3_ovlock_poll_ok"],
        )

    return "\n".join(lines) + "\n"


# ── HTTP Handler ─────────────────────────────────────────────────────────────


class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metrics":
            body = format_metrics().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok\n")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # suppress access logs to stderr


# ── Poll Loop ────────────────────────────────────────────────────────────────


def poll_loop(agfs_path: str, interval: int, s3_cfg: dict | None) -> None:
    while True:
        status_ok = poll_ov_status()
        lock_ok = poll_lock_count(agfs_path)
        if s3_cfg is not None:
            # ov status succeeding is the OV-up gate: it talks to the live
            # server, so a restart window (frozen mtimes everywhere) reads
            # as not-up and the poll holds its previous classification.
            poll_s3_locks(
                s3_cfg["client"],
                s3_cfg["bucket"],
                s3_cfg["live_window"],
                s3_cfg["dead_threshold"],
                ov_up=status_ok,
                top_n=s3_cfg["label_top_n"],
                min_age=s3_cfg["label_min_age"],
            )
        with _lock:
            _metrics["openviking_up"] = 1 if (status_ok and lock_ok) else 0
            _metrics["openviking_last_poll_unix"] = time.time()
        time.sleep(interval)


# ── Main ─────────────────────────────────────────────────────────────────────


def main() -> None:
    agfs_path = os.environ.get("AGFS_PATH", "/app/data/viking")
    interval = int(os.environ.get("POLL_INTERVAL", "15"))
    port = int(os.environ.get("PORT", "9210"))

    s3_cfg: dict | None = None
    bucket = os.environ.get("OVLOCK_S3_BUCKET", "")
    if bucket:
        s3_cfg = {
            "client": s3lite.S3Client(
                endpoint=os.environ.get(
                    "OVLOCK_S3_ENDPOINT", "http://garage.garage.svc.cluster.local:3900"
                ),
                region=os.environ.get("OVLOCK_S3_REGION", "garage"),
                access_key=os.environ["AWS_ACCESS_KEY_ID"],
                secret_key=os.environ["AWS_SECRET_ACCESS_KEY"],
            ),
            "bucket": bucket,
            "live_window": float(os.environ.get("OVLOCK_LIVE_WINDOW_SECONDS", "120")),
            "dead_threshold": float(
                os.environ.get("OVLOCK_DEAD_THRESHOLD_SECONDS", "1800")
            ),
            "label_top_n": int(os.environ.get("OVLOCK_LABEL_TOP_N", "3")),
            "label_min_age": float(
                os.environ.get("OVLOCK_LABEL_MIN_AGE_SECONDS", "900")
            ),
        }

    print(
        f"ov-exporter starting agfs={agfs_path} interval={interval}s port={port} "
        f"s3_bucket={bucket or '(disabled)'}"
    )

    poller = threading.Thread(
        target=poll_loop, args=(agfs_path, interval, s3_cfg), daemon=True
    )
    poller.start()

    server = ThreadingHTTPServer(("0.0.0.0", port), MetricsHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
