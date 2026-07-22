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

Report-only for restarts by design: this tool never touches the Kubernetes
API and never restarts deploy/openviking (single-restart-controller rule,
IMPR-1062 plan review P1).

Dry-run by default; pass --apply to actually delete (repo convention).

Env (flags override): OVLOCK_S3_BUCKET, OVLOCK_S3_ENDPOINT, OVLOCK_S3_REGION,
AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, OV_HEALTH_URL.

Exit codes: 0 = clean run (including "nothing to do" and "skipped: OV
unhealthy"); 1 = operational error (S3/HTTP failure).
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "exporters"))
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


def run(args: argparse.Namespace, client: s3lite.S3Client, sleep_fn=time.sleep) -> int:
    if not ov_healthy(args.health_url):
        print("skipped: OV unhealthy at pass 1 — every lock looks frozen during a restart")
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

    for key in live:
        print(f"live-refreshing (heal/operator territory, never deleted): {key} "
              f"token={read_token(client, args.bucket, key)}")
    for key in fresh:
        age = int(now - pass2[key].timestamp())
        print(f"fresh (frozen {age}s < threshold {int(args.dead_threshold)}s): {key}")

    if not dead:
        print("clean: no dead leases")
        return 0

    candidates = dead[: args.max_delete]
    overflow = dead[args.max_delete :]
    failures = 0
    for key in candidates:
        age = int(now - pass2[key].timestamp())
        token = read_token(client, args.bucket, key)
        if args.apply:
            try:
                client.delete_object(args.bucket, key)
            except s3lite.S3Error as e:
                print(f"delete FAILED: {key} ({e})")
                failures += 1
                continue
            print(f"deleted dead lease: {key} age={age}s token={token}")
        else:
            print(f"would delete (dry-run): {key} age={age}s token={token}")
    if overflow:
        print(f"deferred {len(overflow)} candidate(s) beyond --max-delete "
              f"{args.max_delete}; the next run drains them")
    verb = "deleted" if args.apply else "would delete"
    print(f"summary: {len(pass2)} locks | {len(live)} live | {len(fresh)} fresh | "
          f"{len(dead)} dead ({verb} {len(candidates) - failures}, "
          f"failed {failures}, deferred {len(overflow)})")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true",
                        help="Actually delete dead leases. Default is dry-run.")
    parser.add_argument("--bucket", default=os.environ.get("OVLOCK_S3_BUCKET", "openviking-agfs"))
    parser.add_argument("--endpoint", default=os.environ.get(
        "OVLOCK_S3_ENDPOINT", "http://garage.garage.svc.cluster.local:3900"))
    parser.add_argument("--region", default=os.environ.get("OVLOCK_S3_REGION", "garage"))
    parser.add_argument("--health-url", default=os.environ.get(
        "OV_HEALTH_URL", "http://openviking.viking.svc.cluster.local:1933/health"))
    parser.add_argument("--pass-interval", type=float, default=120.0,
                        help="Seconds between list passes (default 120 = 4x refresh cadence)")
    parser.add_argument("--dead-threshold", type=float, default=1800.0,
                        help="mtime age (s) above which a frozen lock is a dead lease")
    parser.add_argument("--max-delete", type=int, default=100,
                        help="Deletion cap per run; excess deferred to the next run")
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
