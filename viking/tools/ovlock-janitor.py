#!/usr/bin/env python3
"""Resume-safe janitor for stale OpenViking .ovlock files in S3 (IMPR-1095).

Sweeps the AGFS bucket for lock objects whose lease refresh loop is dead and
deletes them — nothing else. Replaces the blind 300s sidecar IMPR-1007 Phase 3
removed, without repeating its failure modes:

- Not blind-by-time: a lock is deleted only when its mtime stayed frozen
  across two list passes >= 4 refresh intervals apart AND its age exceeds the
  dead threshold (default 30 min = 30x the deployed lock_expire) AND OpenViking
  answered /health at both passes. Live-refreshing locks (mtime advancing —
  the BUG-1039 wedge signature) are logged and left for the IMPR-1062 Phase 4
  in-Job heal / operator; a file delete cannot kill a server-side refresher.
- Not resume-unsafe: v0.4.10 resumes interrupted work after a restart
  (IMPR-1063 removed a wipe-on-start for exactly this reason). During a
  restart every mtime freezes, so both health gates skip the run outright;
  once OV is healthy, resumed work re-acquires within minutes — far inside
  the 30-minute threshold.
- Not delete-racy: Garage has no conditional (If-Match) delete, so each
  candidate is re-GET'd immediately before deletion and skipped unless its
  Last-Modified still matches pass 2 — any reacquisition rewrites the object
  and moves mtime by ~the full dead threshold, so second-precision
  comparison is unambiguous. Residual window is the GET->DELETE round trip
  (milliseconds); even a lost race self-heals in <=30s (the new holder's
  refresh loop rewrites the lock file) unless a second acquirer also lands
  inside that same sub-second gap.

Report-only for restarts by design: this tool never touches the Kubernetes
API and never restarts deploy/openviking (single-restart-controller rule,
IMPR-1062 plan review P1).

Wedge report (BUG-1220): every live-refreshing lock is reported with its
viking:// target, owner handle and lock type, and with how long it has been
continuously present. A two-pass run cannot see presence itself (a live
lock's mtime is always fresh, and the token's time_ns is the last refresh,
not the acquisition), so the age comes from ov-exporter's
openviking_s3_ovlock_live_age_seconds series, which tracks first sighting
every 15s. Locks present at least --wedge-threshold (default 2700s, the
OpenVikingWedgedLockFile threshold) are reported as WEDGED. They are never
deleted: their holder still believes it owns them, and recovery is an OV
restart, which belongs to the BUG-1181 in-Job heal or the operator. An
unreachable or misbehaving exporter degrades the report to "age unknown"; it
never changes what is deleted. The exporter labels only its top N
longest-present locks, so with more than N wedged at once the extras print
as presence_age=not-exported and are not counted as WEDGED.

Dry-run by default; pass --apply to actually delete (repo convention).

Env (flags override): OVLOCK_S3_BUCKET, OVLOCK_S3_ENDPOINT, OVLOCK_S3_REGION,
AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, OV_HEALTH_URL,
OV_EXPORTER_METRICS_URL, OVLOCK_WEDGE_THRESHOLD_SECONDS.

Exit codes: 0 = clean run (including "nothing to do" and "skipped: OV
unhealthy"); 1 = operational error (S3/HTTP failure).
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "exporters")
)
import s3lite  # noqa: E402 - same dir in-pod, ../exporters in-repo


def ov_healthy(health_url: str, timeout: float = 10.0) -> bool:
    try:
        with urllib.request.urlopen(
            urllib.request.Request(health_url), timeout=timeout
        ) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


def list_locks(client: s3lite.S3Client, bucket: str) -> dict[str, datetime.datetime]:
    """{key: LastModified} for every lock object in the bucket."""
    return {
        o["Key"]: o["LastModified"]
        for o in client.list_objects(bucket)
        if s3lite.is_lock_key(o["Key"])
    }


def read_token(client: s3lite.S3Client, bucket: str, key: str) -> str:
    """Lock body `{handle_id}:{time_ns}:{E|T}`, or a placeholder if unreadable."""
    try:
        body = client.get_object(bucket, key).decode("utf-8", "replace").strip()
        return body if body.count(":") == 2 else f"(unexpected body: {body[:60]!r})"
    except s3lite.S3Error as e:
        return f"(unreadable: {e})"


def _mtime_matches(
    current: datetime.datetime, expected: datetime.datetime, tolerance: float = 1.5
) -> bool:
    """Second-precision equality: the Last-Modified header has no sub-second
    part, while ListObjectsV2 timestamps do. A reacquisition moves mtime by
    ~the dead threshold (30 min), so 1.5s tolerance cannot mask one."""
    return abs((current - expected).total_seconds()) <= tolerance


PRESENCE_METRIC = "openviking_s3_ovlock_live_age_seconds"
_LABEL_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)="((?:[^"\\]|\\.)*)"')
_UNESCAPE = {"\\\\": "\\", '\\"': '"', "\\n": "\n"}


def _parse_labels(block: str) -> dict[str, str]:
    """`a="x",b="y\\"z"` -> {"a": "x", "b": 'y"z'} (Prometheus text escaping)."""
    return {
        name: re.sub(r"\\.", lambda m: _UNESCAPE.get(m.group(0), m.group(0)), raw)
        for name, raw in _LABEL_RE.findall(block)
    }


def parse_presence_ages(text: str) -> dict[str, float]:
    """{lock key: presence age (s)} from ov-exporter's /metrics text.

    Reads only the per-lock series ov-exporter publishes for its top-N
    longest-present live locks (BUG-1220). A key absent here was either
    present for less than the exporter's minimum age or outside its top N.
    """
    ages: dict[str, float] = {}
    prefix = PRESENCE_METRIC + "{"
    for line in text.splitlines():
        if not line.startswith(prefix):
            continue
        block, sep, value = line[len(prefix) :].rpartition("} ")
        if not sep:
            continue
        key = _parse_labels(block).get("key")
        try:
            age = float(value.split()[0])
        except (IndexError, ValueError):
            continue
        if key:
            ages[key] = age
    return ages


def fetch_presence_ages(url: str, timeout: float = 10.0) -> dict[str, float] | None:
    """parse_presence_ages over a live scrape, or None on any failure.

    Catches every Exception, not just URLError/OSError: a malformed or
    truncated response raises http.client.HTTPException (BadStatusLine,
    IncompleteRead), which is neither, and the wedge report must never abort
    the run before its deletion pass.
    """
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url), timeout=timeout
        ) as resp:
            return parse_presence_ages(resp.read().decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001 - report degrades, run carries on
        print(f"presence ages unavailable from ov-exporter ({url}): {e!r}")
        return None


NOT_EXPORTED = "not-exported"


def describe_live(
    key: str,
    token: str,
    presence_age: float | None,
    wedge_threshold: float,
    exporter_reachable: bool = False,
) -> tuple[str, bool]:
    """One report line for a live-refreshing lock, and whether it is wedged.

    `token` is read_token()'s output (raw body or a placeholder). Wedged means
    present for at least wedge_threshold seconds. Either way the line says
    the lock is never deleted. With no age, the line says why: `unknown`
    when the exporter could not be read, `not-exported` when it was read but
    has no series for this key (present under its minimum age, or outside
    its top N — so a wedged lock beyond the top N shows up here, uncounted).
    """
    parsed = s3lite.parse_lock_token(token)
    owner = parsed["owner"] if parsed else "unknown"
    lock_type = parsed["lock_type"] if parsed else "unknown"
    if presence_age is not None:
        age = f"{int(presence_age)}s"
    else:
        age = NOT_EXPORTED if exporter_reachable else "unknown"
    # `<prefix>: <key> k=v ...` in both shapes; the live-refreshing prefix and
    # leading key are the pre-BUG-1220 format, kept for existing log searches.
    fields = (
        f"{key} uri={s3lite.lock_target(key) or 'unknown'} owner={owner} "
        f"lock_type={lock_type} presence_age={age} token={token}"
    )
    wedged = presence_age is not None and presence_age >= wedge_threshold
    if wedged:
        line = (
            f"WEDGED live lock, present >= {int(wedge_threshold)}s (never deleted: "
            f"recovery is an OV restart by the BUG-1181 heal or the operator): "
            f"{fields}"
        )
        return line, True
    return (
        f"live-refreshing (heal/operator territory, never deleted): {fields}",
        False,
    )


def classify(
    pass1: dict[str, datetime.datetime],
    pass2: dict[str, datetime.datetime],
    now: float,
    dead_threshold: float,
) -> tuple[list[str], list[str], list[str]]:
    """Split keys seen in both passes into (dead, live, fresh).

    dead:  mtime identical in both passes and older than dead_threshold
    live:  mtime advanced between passes (lease actively refreshed)
    fresh: mtime identical but younger than the threshold — left alone
    Keys present in only one pass are normal acquire/release churn: ignored.
    """
    dead: list[str] = []
    live: list[str] = []
    fresh: list[str] = []
    for key, mtime1 in pass1.items():
        mtime2 = pass2.get(key)
        if mtime2 is None:
            continue
        if mtime2 > mtime1:
            live.append(key)
        elif now - mtime2.timestamp() > dead_threshold:
            dead.append(key)
        else:
            fresh.append(key)
    return sorted(dead), sorted(live), sorted(fresh)


def run(
    args: argparse.Namespace,
    client: s3lite.S3Client,
    sleep_fn=time.sleep,
    presence_fn=fetch_presence_ages,
) -> int:
    if not ov_healthy(args.health_url):
        print(
            "skipped: OV unhealthy at pass 1 — every lock looks frozen during a restart"
        )
        return 0
    pass1 = list_locks(client, args.bucket)
    print(f"pass 1: {len(pass1)} lock object(s)")
    if not pass1:
        print("clean: no lock objects in the bucket")
        return 0

    sleep_fn(args.pass_interval)

    if not ov_healthy(args.health_url):
        print("skipped: OV unhealthy at pass 2 — deleting nothing")
        return 0
    pass2 = list_locks(client, args.bucket)
    print(f"pass 2: {len(pass2)} lock object(s)")

    now = time.time()
    dead, live, fresh = classify(pass1, pass2, now, args.dead_threshold)

    # Report-only: nothing in this block feeds the deletion path below, and
    # no failure in it may stop that path from running.
    if live:
        try:
            presence = presence_fn(args.exporter_url)
        except Exception as e:  # noqa: BLE001 - report degrades, run carries on
            print(f"presence ages unavailable from ov-exporter: {e!r}")
            presence = None
        reachable = presence is not None
        presence = presence or {}
        wedged = not_exported = 0
        for key in live:
            line, is_wedged = describe_live(
                key,
                read_token(client, args.bucket, key),
                presence.get(key),
                args.wedge_threshold,
                exporter_reachable=reachable,
            )
            print(line)
            wedged += is_wedged
            not_exported += reachable and key not in presence
        print(
            f"wedge report: {wedged} of {len(live)} live lock(s) present >= "
            f"{int(args.wedge_threshold)}s (never deleted; BUG-1220)"
        )
        if not_exported:
            print(
                f"wedge report: {not_exported} live lock(s) have no ov-exporter "
                "series (present under its minimum age, or outside its top N; "
                "a wedged lock beyond the top N is not counted above)"
            )
    for key in fresh:
        age = int(now - pass2[key].timestamp())
        print(f"fresh (frozen {age}s < threshold {int(args.dead_threshold)}s): {key}")

    if not dead:
        print("clean: no dead leases")
        return 0

    candidates = dead[: args.max_delete]
    overflow = dead[args.max_delete :]
    failures = 0
    reacquired = 0
    for key in candidates:
        age = int(now - pass2[key].timestamp())
        if args.apply:
            try:
                body, current_mtime = client.get_object_meta(args.bucket, key)
            except s3lite.S3Error as e:
                # Vanished (already released/cleaned) or unreadable: never
                # delete on unverified state.
                print(f"skipped (pre-delete verify failed): {key} ({e})")
                reacquired += 1
                continue
            token = body.decode("utf-8", "replace").strip()
            if current_mtime is None or not _mtime_matches(current_mtime, pass2[key]):
                print(
                    f"skipped (reacquired between pass 2 and delete): {key} "
                    f"mtime now {current_mtime} token={token[:80]!r}"
                )
                reacquired += 1
                continue
            try:
                client.delete_object(args.bucket, key)
            except s3lite.S3Error as e:
                print(f"delete FAILED: {key} ({e})")
                failures += 1
                continue
            print(f"deleted dead lease: {key} age={age}s token={token}")
        else:
            token = read_token(client, args.bucket, key)
            print(f"would delete (dry-run): {key} age={age}s token={token}")
    if overflow:
        print(
            f"deferred {len(overflow)} candidate(s) beyond --max-delete "
            f"{args.max_delete}; the next run drains them"
        )
    verb = "deleted" if args.apply else "would delete"
    print(
        f"summary: {len(pass2)} locks | {len(live)} live | {len(fresh)} fresh | "
        f"{len(dead)} dead ({verb} {len(candidates) - failures - reacquired}, "
        f"reacquired/skipped {reacquired}, failed {failures}, "
        f"deferred {len(overflow)})"
    )
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete dead leases. Default is dry-run.",
    )
    parser.add_argument(
        "--bucket", default=os.environ.get("OVLOCK_S3_BUCKET", "openviking-agfs")
    )
    parser.add_argument(
        "--endpoint",
        default=os.environ.get(
            "OVLOCK_S3_ENDPOINT", "http://garage.garage.svc.cluster.local:3900"
        ),
    )
    parser.add_argument(
        "--region", default=os.environ.get("OVLOCK_S3_REGION", "garage")
    )
    parser.add_argument(
        "--health-url",
        default=os.environ.get(
            "OV_HEALTH_URL", "http://openviking.viking.svc.cluster.local:1933/health"
        ),
    )
    parser.add_argument(
        "--pass-interval",
        type=float,
        default=120.0,
        help="Seconds between list passes (default 120 = 4x refresh cadence)",
    )
    parser.add_argument(
        "--dead-threshold",
        type=float,
        default=1800.0,
        help="mtime age (s) above which a frozen lock is a dead lease",
    )
    parser.add_argument(
        "--exporter-url",
        default=os.environ.get(
            "OV_EXPORTER_METRICS_URL",
            "http://openviking.viking.svc.cluster.local:9210/metrics",
        ),
        help="ov-exporter /metrics, the source of live-lock presence ages",
    )
    parser.add_argument(
        "--wedge-threshold",
        type=float,
        default=float(os.environ.get("OVLOCK_WEDGE_THRESHOLD_SECONDS", "2700")),
        help="Presence age (s) at which a live lock is reported as WEDGED "
        "(default 2700, the OpenVikingWedgedLockFile threshold). Report only.",
    )
    parser.add_argument(
        "--max-delete",
        type=int,
        default=100,
        help="Deletion cap per run; excess deferred to the next run",
    )
    args = parser.parse_args()

    client = s3lite.S3Client(
        endpoint=args.endpoint,
        region=args.region,
        access_key=os.environ["AWS_ACCESS_KEY_ID"],
        secret_key=os.environ["AWS_SECRET_ACCESS_KEY"],
    )
    try:
        return run(args, client)
    except s3lite.S3Error as e:
        print(f"operational error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
