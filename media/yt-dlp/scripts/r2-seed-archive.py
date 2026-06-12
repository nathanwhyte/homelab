#!/usr/bin/env -S uv run --with botocore python3
"""
Stage a yt-dlp-format archive file (one `youtube <id>` line per video) on
the `media` PVC for a given playlist, so the corresponding CronJob can do
a delta-only pass on its next run.

Why this exists: the IDEA-019 revival uses `--download-archive` to skip
already-downloaded videos. The R2 backup at
`backups/cluster/homelab-k3s/volumes/archive/media/YouTube/<name>/` holds
the prior-architecture media, but the R2 keys are named by video title,
not by video ID, so they can't be re-derived from the R2 listing alone.

The ID source is supplied by the operator — typically the output of:

    yt-dlp --flat-playlist --print id <playlist_url>

either pasted into a file (`--ids-file`) or streamed on stdin
(`--ids-stdin`). The script's only job is to take that ID list, format
it as `youtube <id>` lines, and stage it on the PVC.

R2 enumeration is supported as a sanity check (`--check-r2`) that prints
"R2 has N .mp4 files under <prefix>" but does NOT contribute IDs. Use it
to confirm the R2 prefix has roughly the expected media count before
seeding.

Usage:
    # 1. Get the playlist IDs (e.g. via yt-dlp flat-playlist):
    yt-dlp --flat-playlist --print id '<playlist_url>' > /tmp/playlist-3.ids

    # 2. Dry-run:
    uv run media/yt-dlp/scripts/r2-seed-archive.py \
        --playlist 3 --ids-file /tmp/playlist-3.ids

    # 3. Apply (writes the archive file to the media PVC):
    uv run media/yt-dlp/scripts/r2-seed-archive.py \
        --playlist 3 --ids-file /tmp/playlist-3.ids --apply

    # All 5 remaining playlists (1, 3, 4, 5, 6) in one go:
    uv run media/yt-dlp/scripts/r2-seed-archive.py \
        --all --ids-dir /tmp/yt-dlp-archives --apply

Safety:
    - Default mode is DRY-RUN (prints the plan, makes zero writes).
    - Pass --apply to actually mutate.
    - Idempotent: re-running without --force is a no-op if the archive
      file already exists on the PVC.
    - Apply path uses `kubectl run --rm -i` with a wemby-pinned alpine
      pod that reads the archive on stdin and writes to /yt-dlp-archive/.
"""

import argparse
import datetime
import hashlib
import hmac
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

# ---------- credentials (inlined; matches r2-consolidate.py) ----------

AK = os.environ.get("R2_AK", "252fd0b63874b32278a38ae39cff877b")
SK = os.environ.get(
    "R2_SK", "4999254a6b3cb663b35238b256a9f39153cb44501832f3c03378c2ef7ddfeeea"
)
SESSION_TOKEN = os.environ.get("R2_SESSION_TOKEN", "")
ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "e9f17cd063113cea9c2e73c302971066")
BUCKET = os.environ.get("R2_BUCKET", "homelab")

# Virtual-hosted-style host: bucket is in the hostname, not the path.
# R2 requires this for object-level ops. See:
# https://developers.cloudflare.com/r2/api/s3/api/
HOST = f"{BUCKET}.{ACCOUNT_ID}.r2.cloudflarestorage.com"
NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}

MEDIA_BASE = "backups/cluster/homelab-k3s/volumes/archive/media/YouTube/"

# (playlist_id, r2_prefix_relative_to_MEDIA_BASE, archive_filename, display_name)
# Must match yt-dlp-playlists.yaml:30-52 and r2-consolidate.py:357-369.
PLAYLISTS = [
    (1, "Essential/", "archive-playlist-1.txt", "Essential"),
    (
        2,
        "Gotta Keep These Somewhere/",
        "archive-playlist-2.txt",
        "Gotta Keep These Somewhere",
    ),
    (3, "Nice/", "archive-playlist-3.txt", "Nice"),
    (4, "Study/", "archive-playlist-4.txt", "Study"),
    (5, "Wow/", "archive-playlist-5.txt", "Wow"),
    (6, "Ugh/", "archive-playlist-6.txt", "Ugh"),
]

# YouTube IDs are 11 chars: [A-Za-z0-9_-]{11}. The bare filename may also
# have a [height] suffix or no extension; this regex pulls the first
# 11-char run that looks like an ID. The deny-list guards against
# directory-name collisions (e.g. a "shorts/" subdir).
ID_RE = re.compile(r"([A-Za-z0-9_-]{11})")
ID_DENY = {"youtube", "playlist", "watch", "shorts", "downloads"}


# ---------- SigV4 + rate limit (borrowed from r2-consolidate.py) ----------

_RPS = 32
_TOKEN_LOCK = threading.Lock()
_TOKENS = _RPS
_LAST = time.monotonic()


def _take_token():
    global _TOKENS, _LAST
    while True:
        with _TOKEN_LOCK:
            now = time.monotonic()
            elapsed = now - _LAST
            _TOKENS = min(_RPS, _TOKENS + elapsed * _RPS)
            _LAST = now
            if _TOKENS >= 1:
                _TOKENS -= 1
                return
        time.sleep(1.0 / _RPS)


def canonical_query_string(params):
    return "&".join(
        f"{k}={urllib.parse.quote(v, safe='')}" for k, v in sorted(params.items())
    )


def sigv4_for_method(method, canonical_uri, canonical_query, extra_signed_headers=None):
    now = datetime.datetime.now(datetime.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = amz_date[:8]

    headers = [
        ("host", HOST),
        ("x-amz-content-sha256", "UNSIGNED-PAYLOAD"),
        ("x-amz-date", amz_date),
    ]
    if SESSION_TOKEN:
        headers.append(("x-amz-security-token", SESSION_TOKEN))
    if extra_signed_headers:
        for name, value in extra_signed_headers.items():
            headers.append((name.lower(), value))
    headers.sort(key=lambda kv: kv[0])

    canonical_headers = "".join(f"{name}:{value}\n" for name, value in headers)
    signed_headers_names = ";".join(name for name, _ in headers)

    canonical_request = "\n".join(
        [
            method,
            canonical_uri,
            canonical_query,
            canonical_headers,
            signed_headers_names,
            "UNSIGNED-PAYLOAD",
        ]
    )
    scope = f"{date_stamp}/auto/s3/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ]
    )

    def h(k, d):
        return hmac.new(k, d.encode("utf-8"), hashlib.sha256).digest()

    k_date = h(("AWS4" + SK).encode(), date_stamp)
    k_region = h(k_date, "auto")
    k_service = h(k_region, "s3")
    k_signing = h(k_service, "aws4_request")
    sig = hmac.new(k_signing, string_to_sign.encode(), hashlib.sha256).hexdigest()
    return (
        f"AWS4-HMAC-SHA256 Credential={AK}/{scope}, "
        f"SignedHeaders={signed_headers_names}, Signature={sig}",
        amz_date,
    )


def request(method, key, params=None):
    _take_token()
    params = params or {}
    canonical_query = canonical_query_string(params)
    canonical_uri = "/" + urllib.parse.quote(key, safe="/")
    auth, amz_date = sigv4_for_method(method, canonical_uri, canonical_query)
    url = f"https://{HOST}{canonical_uri}"
    if canonical_query:
        url += "?" + canonical_query
    headers_out = {
        "Authorization": auth,
        "x-amz-date": amz_date,
        "x-amz-content-sha256": "UNSIGNED-PAYLOAD",
        "Host": HOST,
    }
    if SESSION_TOKEN:
        headers_out["x-amz-security-token"] = SESSION_TOKEN
    req = urllib.request.Request(url, headers=headers_out, method=method)
    return urllib.request.urlopen(req)


def list_v2(prefix, with_delim=False, continuation_token=None):
    params = {"list-type": "2", "max-keys": "1000"}
    if with_delim:
        params["delimiter"] = "/"
    if prefix:
        params["prefix"] = prefix
    if continuation_token:
        params["continuation-token"] = continuation_token
    with request("GET", "", params) as r:
        return ET.fromstring(r.read().decode())


def drain_keys(prefix):
    """Paged to completion. Returns list of (key, size)."""
    out = []
    token = None
    while True:
        root = list_v2(prefix, with_delim=False, continuation_token=token)
        for c in root.findall("s3:Contents", NS):
            out.append((c.find("s3:Key", NS).text, int(c.find("s3:Size", NS).text)))
        is_truncated = (
            (root.findtext("s3:IsTruncated", "false", NS) or "false").strip().lower()
        )
        if is_truncated == "true":
            token_el = root.find("s3:NextContinuationToken", NS)
            token = token_el.text if token_el is not None else None
            if not token:
                raise RuntimeError(
                    f"ListObjectsV2 reported IsTruncated=true but no NextContinuationToken for prefix {prefix!r}"
                )
        else:
            return out


# ---------- ID extraction ----------


def extract_id(key: str) -> str | None:
    """Extract a YouTube video ID from an R2 object key.

    Filenames in the R2 backup are typically `<title>.<ext>`, not `<id>.<ext>`.
    This function is kept for the optional R2 sanity check; in practice it
    will return None for most R2 keys. Use `--ids-file` or `--ids-stdin`
    to provide the actual ID list.
    """
    basename = os.path.basename(key)
    stem = basename.rsplit(".", 1)[0] if "." in basename else basename
    m = ID_RE.search(stem)
    if not m:
        return None
    candidate = m.group(1)
    if candidate.lower() in ID_DENY:
        return None
    return candidate


def parse_ids_from_text(text: str) -> tuple[list[str], list[tuple[int, str]]]:
    """Parse a list of YouTube IDs from a text blob.

    Accepts one ID per line, or whitespace/comma-separated. Each candidate
    is checked against the 11-char `[A-Za-z0-9_-]{11}` pattern. Returns
    (valid_sorted_unique, rejected_lines).
    """
    valid: list[str] = []
    seen: set[str] = set()
    rejected: list[tuple[int, str]] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Tolerate `youtube <id>` lines (full archive format) by stripping
        # the extractor prefix if present.
        if line.startswith("youtube "):
            line = line[len("youtube ") :].strip()
        # Tolerate URL forms: https://youtu.be/<id>, watch?v=<id>
        for prefix in (
            "https://youtu.be/",
            "http://youtu.be/",
            "https://www.youtube.com/watch?v=",
            "http://www.youtube.com/watch?v=",
            "https://youtube.com/watch?v=",
            "youtube.com/watch?v=",
        ):
            if line.startswith(prefix):
                line = line[len(prefix) :].split("&", 1)[0]
                break
        if not ID_RE.fullmatch(line):
            rejected.append((lineno, raw))
            continue
        if line in seen:
            continue
        seen.add(line)
        valid.append(line)
    valid.sort()
    return valid, rejected


def r2_sanity_check(playlist_id) -> tuple[int, int]:
    """Enumerate the R2 prefix and return (key_count, total_bytes).

    R2 keys are named by video title, not ID — this is a sanity check,
    not an ID source. Use `--ids-file` or `--ids-stdin` to provide IDs.
    """
    pid, rel, _fname, _name = next(p for p in PLAYLISTS if p[0] == playlist_id)
    r2_prefix = MEDIA_BASE + rel
    keys = drain_keys(r2_prefix)
    total = sum(size for _, size in keys)
    return len(keys), total


def load_ids(playlist_id, args) -> list[str]:
    """Resolve the ID list from the operator-supplied source(s)."""
    pid, _rel, _fname, _name = next(p for p in PLAYLISTS if p[0] == playlist_id)

    # --ids-stdin takes priority (any of --ids-file/--ids-dir/--ids-stdin
    # for one playlist — only one is meaningful per --playlist invocation).
    if args.ids_stdin:
        text = sys.stdin.read()
        ids, rejected = parse_ids_from_text(text)
        if rejected:
            preview = ", ".join(f"L{n}:{raw!r}" for n, raw in rejected[:3])
            print(
                f"  warning: {len(rejected)} unparseable line(s) ignored "
                f"(e.g. {preview})",
                file=sys.stderr,
            )
        return ids

    if args.ids_file:
        with open(args.ids_file, "r", encoding="utf-8") as f:
            text = f.read()
        ids, rejected = parse_ids_from_text(text)
        if rejected:
            preview = ", ".join(f"L{n}:{raw!r}" for n, raw in rejected[:3])
            print(
                f"  warning: {len(rejected)} unparseable line(s) in "
                f"{args.ids_file} (e.g. {preview})",
                file=sys.stderr,
            )
        return ids

    if args.ids_dir:
        # --ids-dir expects one file per playlist, named
        # archive-playlist-N.txt (already in yt-dlp archive format) OR
        # a plain ID list. Detect format by the first non-blank line.
        path = os.path.join(args.ids_dir, f"archive-playlist-{pid}.txt")
        if not os.path.exists(path):
            raise SystemExit(
                f"error: {path} not found (--ids-dir expects "
                f"archive-playlist-N.txt files)"
            )
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        ids, rejected = parse_ids_from_text(text)
        if rejected:
            preview = ", ".join(f"L{n}:{raw!r}" for n, raw in rejected[:3])
            print(
                f"  warning: {len(rejected)} unparseable line(s) in "
                f"{path} (e.g. {preview})",
                file=sys.stderr,
            )
        return ids

    raise SystemExit(
        f"error: no ID source for playlist {pid}. Pass --ids-file, "
        f"--ids-stdin, or --ids-dir."
    )


def build_archive(playlist_id, args):
    """Return (ids_sorted, r2_key_count, r2_total_size)."""
    ids = load_ids(playlist_id, args)
    r2_keys, r2_total = 0, 0
    if args.check_r2:
        r2_keys, r2_total = r2_sanity_check(playlist_id)
    return ids, r2_keys, r2_total


def format_archive(ids):
    """yt-dlp archive format: one line per record, `<extractor> <id>`."""
    return "".join(f"youtube {vid}\n" for vid in ids).encode("utf-8")


# ---------- apply to PVC ----------


def archive_exists(playlist_id) -> bool:
    """Check whether archive-playlist-N.txt already exists on the PVC."""
    pid, _rel, fname, _name = next(p for p in PLAYLISTS if p[0] == playlist_id)
    pod_overrides = (
        '{"spec":{"nodeName":"wemby","restartPolicy":"Never",'
        '"containers":[{"name":"check","image":"alpine:3.20",'
        '"command":["sh","-c",'
        f'"test -f /yt-dlp-archive/{fname} && echo EXISTS || echo MISSING"'
        "],"
        '"volumeMounts":[{"name":"media","mountPath":"/yt-dlp-archive"}]}],'
        '"volumes":[{"name":"media",'
        '"persistentVolumeClaim":{"claimName":"media"}}]}}'
    )
    proc = subprocess.run(
        [
            "kubectl",
            "run",
            "yt-dlp-archive-check",
            "-n",
            "yt-dlp",
            "--rm",
            "-i",
            "--image=alpine:3.20",
            "--overrides",
            pod_overrides,
            "--restart=Never",
        ],
        input=b"",
        capture_output=True,
        check=False,
    )
    return "EXISTS" in proc.stdout.decode()


def apply_to_pvc(playlist_id, archive_bytes):
    """Write the archive file to the PVC via a wemby-pinned alpine pod.

    The content is shipped base64-encoded as a shell argument and decoded
    on the pod side, then chowned to 1000:1000 and chmodded to 0644. The
    fsGroup on the CronJob template (cronjobs.yaml:65) expects this
    ownership.

    Note: an earlier version piped the bytes via `cat > file` reading
    subprocess.run stdin, but `kubectl run --rm -i` has a startup race
    where stdin can be lost before the pod is ready — observed as a
    0-byte file on the PVC. base64-in-arg sidesteps that race entirely.
    """
    import base64

    pid, _rel, fname, _name = next(p for p in PLAYLISTS if p[0] == playlist_id)
    b64 = base64.b64encode(archive_bytes).decode("ascii")
    sh_cmd = (
        f"echo {b64} | base64 -d > /yt-dlp-archive/{fname} && "
        f"chown 1000:1000 /yt-dlp-archive/{fname} && "
        f"chmod 0644 /yt-dlp-archive/{fname} && "
        f"echo applied && "
        f"wc -c /yt-dlp-archive/{fname} && "
        f"ls -la /yt-dlp-archive/{fname}"
    )
    pod_overrides = (
        '{"spec":{"nodeName":"wemby","restartPolicy":"Never",'
        '"securityContext":{"fsGroup":1000},'
        '"containers":[{"name":"write","image":"alpine:3.20",'
        '"command":["sh","-c",'
        f'"{sh_cmd}"'
        "],"
        '"volumeMounts":[{"name":"media","mountPath":"/yt-dlp-archive"}]}],'
        '"volumes":[{"name":"media",'
        '"persistentVolumeClaim":{"claimName":"media"}}]}}'
    )
    print(f"  applying {fname} ({len(archive_bytes)} bytes / {len(b64)} b64) to PVC...")
    proc = subprocess.run(
        [
            "kubectl",
            "run",
            "yt-dlp-archive-seed",
            "-n",
            "yt-dlp",
            "--rm",
            "--image=alpine:3.20",
            "--overrides",
            pod_overrides,
            "--restart=Never",
            "--attach",
        ],
        input=b"",
        capture_output=True,
        check=False,
    )
    sys.stdout.write(proc.stdout.decode())
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr.decode())
        raise SystemExit(f"kubectl run failed with exit code {proc.returncode}")


# ---------- CLI ----------


def print_dry_run(playlist_id, ids, r2_key_count, r2_total_size, id_source_label):
    pid, rel, fname, name = next(p for p in PLAYLISTS if p[0] == playlist_id)
    print(f"[playlist {pid} / {name}/]")
    if r2_key_count:
        print(f"  R2 prefix: {MEDIA_BASE}{rel}")
        print(
            f"  R2 keys:   {r2_key_count} ({r2_total_size / 1e9:.3f} GB)  -- "
            f"sanity check only (R2 keys are title-named, not ID-named)"
        )
    print(f"  ID source: {id_source_label}")
    print(f"  IDs found: {len(ids)}")
    archive = format_archive(ids)
    print(f"  Archive:   {fname}  ({len(ids)} lines, {len(archive)} bytes)")
    if ids:
        print(f"  First IDs: {ids[:3]}")
        print(f"  Last IDs:  {ids[-3:]}")
    print(f"  PVC target: /yt-dlp-archive/{fname}")
    print("  DRY-RUN: pass --apply to write.")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--playlist",
        type=int,
        choices=[1, 2, 3, 4, 5, 6],
        help="Single playlist to seed (1..6)",
    )
    ap.add_argument(
        "--all",
        action="store_true",
        help="Seed all 5 remaining playlists (1, 3, 4, 5, 6) in one go",
    )
    ap.add_argument(
        "--include-playlist-2",
        action="store_true",
        help="Include playlist 2 (default --all skips it because it is already validated)",
    )
    ap.add_argument(
        "--ids-file",
        type=str,
        help="Path to a file containing YouTube IDs (one per line, or yt-dlp archive format)",
    )
    ap.add_argument(
        "--ids-stdin",
        action="store_true",
        help="Read YouTube IDs from stdin (pipe `yt-dlp --flat-playlist --print id ...`)",
    )
    ap.add_argument(
        "--ids-dir",
        type=str,
        help="Directory containing archive-playlist-N.txt files (one per playlist). "
        "Used with --all to read multiple playlists from one path.",
    )
    ap.add_argument(
        "--check-r2",
        action="store_true",
        help="Also enumerate the R2 prefix for the playlist and print the count. "
        "Sanity check only — R2 keys are title-named and don't carry IDs.",
    )
    ap.add_argument(
        "--apply", action="store_true", help="Write to the PVC. Default is dry-run."
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing archive file on the PVC.",
    )
    args = ap.parse_args()

    if not args.playlist and not args.all:
        ap.error("must pass --playlist N or --all")
    if args.playlist and args.all:
        ap.error("--playlist and --all are mutually exclusive")

    # Validate ID-source flags. --ids-stdin is mutually exclusive with
    # the others. --ids-dir implies --all.
    id_sources = sum(bool(x) for x in (args.ids_file, args.ids_stdin, args.ids_dir))
    if id_sources == 0:
        ap.error("must pass --ids-file, --ids-stdin, or --ids-dir")
    if id_sources > 1:
        ap.error("--ids-file, --ids-stdin, and --ids-dir are mutually exclusive")
    if args.ids_dir and not args.all:
        ap.error("--ids-dir requires --all (it provides files for multiple playlists)")
    if (args.ids_file or args.ids_stdin) and args.all:
        ap.error(
            "--ids-file and --ids-stdin apply to a single playlist; use --playlist N "
            "or switch to --ids-dir for --all"
        )

    if args.all:
        target_ids = [1, 3, 4, 5, 6]
        if args.include_playlist_2:
            target_ids = sorted(target_ids + [2])
    else:
        target_ids = [args.playlist]

    if args.apply and args.all:
        ans = input(
            f"About to seed {len(target_ids)} archive files to the media PVC. Continue? [y/N] "
        )
        if ans.strip().lower() not in ("y", "yes"):
            print("aborted.")
            sys.exit(0)

    for pid in target_ids:
        ids, r2_key_count, r2_total_size = build_archive(pid, args)
        if args.ids_file:
            label = f"--ids-file {args.ids_file}"
        elif args.ids_stdin:
            label = "--ids-stdin"
        else:
            label = f"--ids-dir {args.ids_dir}/archive-playlist-{pid}.txt"
        print_dry_run(pid, ids, r2_key_count, r2_total_size, label)

        if not args.apply:
            print()
            continue

        # Apply path: check for existing file, then write.
        if archive_exists(pid):
            if not args.force:
                print(
                    "  ERROR: archive file already exists on PVC. Re-run with --force to overwrite."
                )
                print()
                continue

        archive_bytes = format_archive(ids)
        apply_to_pvc(pid, archive_bytes)
        print()


if __name__ == "__main__":
    main()
