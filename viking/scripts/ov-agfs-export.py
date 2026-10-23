"""Key-exact export, import and verify for an AGFS Garage bucket (IMPR-1412).

GUIDE-1085 Phase 1.4 exports `openviking-agfs` with `aws s3 sync` to a directory.
IMPR-1152 found both halves of that lossy: `default/_system` is an object and a
key prefix at once, so keys cannot all be paths, and `aws s3 sync` dropped 281 of
282 zero-byte objects (`backend_meta.json` among them) with exit 0 and an identical
byte total. This tool keeps the key as data instead: `objects.tar` holds each
object's bytes under a numbered member name, and `manifest.json` maps every key to
its member, size, ETag, md5 and sha256. Reconcile by key set and per-key digest,
never bytes.

  export  --bucket B --out DIR [--base PREV]  bucket -> DIR/objects.tar + manifest.json;
                                              DIR must not exist and appears only when
                                              complete; with --base, a key whose size
                                              and ETag match PREV's entry reuses PREV's
                                              bytes once they check against PREV's
                                              manifest
  import  --bucket B --from DIR [--delete]    DIR -> bucket, PUTting only keys absent
                                              or different there; --delete removes keys
                                              the manifest lacks, so the bucket equals
                                              it; then verify
  verify  --bucket B --from DIR [--deep]      exit 0 when DIR's archive matches its
                                              manifest and the bucket holds exactly the
                                              manifest's keys; each key's listed ETag
                                              must equal its md5 (--deep: GET every
                                              object and compare sha256 instead)

Through the port-forward a GET runs at about 140 objects/s whatever the worker
count, so a full export of the 71k-object production bucket takes about 8 minutes.
Inside an outage, export with `--base` on a copy taken while the server ran: only
keys written since are fetched. A multipart ETag (one with `-`) is not an md5, so
`--base` refetches such keys and `verify` falls back to a GET for them.

It reaches Garage through a kubectl port-forward to svc/garage in `garage`, with the
`openviking-s3-credentials` Secret in `viking` (the key both stacks use). Nothing
is written to disk but DIR.

Usage: uv run --no-project --with boto3 python ov-agfs-export.py export --bucket openviking-agfs --out "$E/agfs-pre-v0.5.1.1"
"""

import argparse
import base64
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tarfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

MANIFEST = "manifest.json"
TAR = "objects.tar"


def list_keys(s3, bucket: str) -> dict[str, tuple[int, str]]:
    """{key: (size, ETag without quotes)}."""
    keys = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        for o in page.get("Contents", []):
            keys[o["Key"]] = (o["Size"], o["ETag"].strip('"'))
    return keys


def get(s3, bucket: str, key: str) -> bytes | None:
    """The object's bytes, or None when it was deleted after the listing."""
    try:
        return s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    except s3.exceptions.NoSuchKey:
        return None


def digest(body: bytes | None) -> str | None:
    """sha256 of an object's bytes; None for one deleted after the listing."""
    return None if body is None else hashlib.sha256(body).hexdigest()


def pmap(fn, items, workers: int):
    """Order-preserving map over a thread pool (boto3 clients are thread-safe)."""
    with ThreadPoolExecutor(max_workers=workers) as ex:
        yield from ex.map(fn, items)


def load(src: Path) -> tuple[dict, dict[str, bytes]]:
    manifest = json.loads((src / MANIFEST).read_text())
    with tarfile.open(src / TAR) as tar:
        bodies = {m.name: tar.extractfile(m).read() for m in tar.getmembers()}
    return manifest, bodies


def intact(e: dict, bodies: dict[str, bytes]) -> bool:
    """The archive holds this entry's member with the manifest's size and sha256."""
    body = bodies.get(e["member"])
    return (
        body is not None
        and len(body) == e["size"]
        and hashlib.sha256(body).hexdigest() == e["sha256"]
    )


def check_archive(src: Path) -> tuple[dict, dict[str, bytes], list[str]]:
    """Load an export and list the keys whose archived bytes disagree with the manifest.

    An unreadable or truncated tar counts every key as bad rather than raising, so a
    verify of a damaged export reports it instead of passing on the manifest alone.
    """
    manifest = json.loads((src / MANIFEST).read_text())
    try:
        _, bodies = load(src)
    except (tarfile.TarError, OSError, EOFError):
        bodies = {}
    bad = [e["key"] for e in manifest["entries"] if not intact(e, bodies)]
    return manifest, bodies, bad


def export(
    s3, bucket: str, out: Path, workers: int = 1, base: Path | None = None
) -> dict:
    """Export into OUT, which must not exist yet; it appears only once complete."""
    if out.exists():
        raise SystemExit(f"{out} exists; export into a new directory")
    partial = out.with_name(f".{out.name}.partial")
    shutil.rmtree(partial, ignore_errors=True)
    partial.mkdir(parents=True)
    listed = list_keys(s3, bucket)
    keys = sorted(listed)
    reuse: dict[str, bytes] = {}
    base_rejected = 0
    if base is not None:
        bmanifest, bbodies, _ = check_archive(base)
        for e in bmanifest["entries"]:
            # md5 == etag: the bytes PREV holds are the ones its listing described,
            # not a rewrite that landed between PREV's list and its GET. intact():
            # PREV's archive still holds exactly those bytes.
            if listed.get(e["key"]) != (e["size"], e["etag"]) or e["md5"] != e["etag"]:
                continue
            if intact(e, bbodies):
                reuse[e["key"]] = bbodies[e["member"]]
            else:
                base_rejected += 1
    fetch = [k for k in keys if k not in reuse]
    fetched = dict(zip(fetch, pmap(lambda k: get(s3, bucket, k), fetch, workers)))
    vanished = sorted(k for k in fetch if fetched[k] is None)
    keys = [k for k in keys if k not in vanished]
    entries = []
    with tarfile.open(partial / TAR, "w") as tar:
        for i, key in enumerate(keys):
            body = reuse[key] if key in reuse else fetched[key]
            member = f"obj/{i:06d}"
            info = tarfile.TarInfo(member)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
            entries.append(
                {
                    "key": key,
                    "member": member,
                    "size": len(body),
                    "etag": listed[key][1],
                    "md5": hashlib.md5(body).hexdigest(),
                    "sha256": hashlib.sha256(body).hexdigest(),
                }
            )
    manifest = {
        "bucket": bucket,
        "objects": len(entries),
        "bytes": sum(e["size"] for e in entries),
        "zero_byte": sum(1 for e in entries if e["size"] == 0),
        "fetched": len(fetch),
        "reused": len(reuse),
        "base_rejected": base_rejected,
        "vanished": len(vanished),
        "vanished_keys": vanished,
        "entries": entries,
    }
    (partial / MANIFEST).write_text(json.dumps(manifest, indent=1) + "\n")
    partial.rename(out)
    return manifest


def import_(s3, bucket: str, src: Path, delete: bool, workers: int = 1) -> dict:
    """Make BUCKET hold the export's keys: PUT only keys absent or different there."""
    manifest, bodies, bad = check_archive(src)
    if bad:
        raise SystemExit(
            f"{len(bad)} archived objects disagree with the manifest, e.g. {bad[:3]}"
        )
    wanted = {e["key"] for e in manifest["entries"]}
    have = list_keys(s3, bucket)
    todo = [
        e for e in manifest["entries"] if have.get(e["key"]) != (e["size"], e["md5"])
    ]
    put = lambda e: s3.put_object(Bucket=bucket, Key=e["key"], Body=bodies[e["member"]])  # noqa: E731
    list(pmap(put, todo, workers))
    stale = sorted(set(have) - wanted) if delete else []
    list(pmap(lambda k: s3.delete_object(Bucket=bucket, Key=k), stale, workers))
    return {
        "put": len(todo),
        "unchanged": len(wanted) - len(todo),
        "deleted": len(stale),
    }


def verify(s3, bucket: str, src: Path, workers: int = 1, deep: bool = False) -> dict:
    manifest, _, archive_bad = check_archive(src)
    want = {e["key"]: e for e in manifest["entries"]}
    have = list_keys(s3, bucket)
    missing = sorted(set(want) - set(have))
    extra = sorted(set(have) - set(want))
    both = sorted(set(want) & set(have))
    by_etag = [] if deep else [k for k in both if "-" not in have[k][1]]
    differ = [k for k in by_etag if have[k] != (want[k]["size"], want[k]["md5"])]
    by_get = sorted(set(both) - set(by_etag))
    digests = pmap(lambda k: digest(get(s3, bucket, k)), by_get, workers)
    differ += [k for k, d in zip(by_get, digests) if d != want[k]["sha256"]]
    differ.sort()
    return {
        "manifest_objects": len(want),
        "bucket_objects": len(have),
        "checked_by_etag": len(by_etag),
        "checked_by_get": len(by_get),
        "missing": missing[:20],
        "extra": extra[:20],
        "differ": differ[:20],
        "archive_bad": archive_bad[:20],
        "counts": {
            "missing": len(missing),
            "extra": len(extra),
            "differ": len(differ),
            "archive_bad": len(archive_bad),
        },
        "ok": not (missing or extra or differ or archive_bad),
    }


def client(port: int, workers: int):
    import boto3
    from botocore.config import Config

    def secret(field: str) -> str:
        return base64.b64decode(
            subprocess.run(
                [
                    "kubectl",
                    "-n",
                    "viking",
                    "get",
                    "secret",
                    "openviking-s3-credentials",
                    "-o",
                    f"jsonpath={{.data.{field}}}",
                ],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        ).decode()

    return boto3.client(
        "s3",
        endpoint_url=f"http://127.0.0.1:{port}",
        region_name="garage",
        aws_access_key_id=secret("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=secret("AWS_SECRET_ACCESS_KEY"),
        config=Config(s3={"addressing_style": "path"}, max_pool_connections=workers),
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("export", "import", "verify"):
        p = sub.add_parser(name)
        p.add_argument("--bucket", required=True)
        p.add_argument("--port", type=int, default=19390)
        p.add_argument("--workers", type=int, default=32)
        if name == "export":
            p.add_argument("--out", required=True, type=Path)
            p.add_argument("--base", type=Path)
        else:
            p.add_argument("--from", dest="src", required=True, type=Path)
        if name == "import":
            p.add_argument("--delete", action="store_true")
        if name == "verify":
            p.add_argument("--deep", action="store_true")
    a = ap.parse_args()
    pf = subprocess.Popen(
        ["kubectl", "-n", "garage", "port-forward", "svc/garage", f"{a.port}:3900"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(3)
        s3 = client(a.port, a.workers)
        t0 = time.monotonic()
        if a.cmd == "export":
            m = export(s3, a.bucket, a.out, a.workers, a.base)
            summary = {k: v for k, v in m.items() if k != "entries"}
            print(json.dumps({**summary, "elapsed_s": int(time.monotonic() - t0)}))
            return 0
        if a.cmd == "import":
            print(json.dumps(import_(s3, a.bucket, a.src, a.delete, a.workers)))
        r = verify(s3, a.bucket, a.src, a.workers, getattr(a, "deep", False))
        r["elapsed_s"] = int(time.monotonic() - t0)
        print(json.dumps(r))
        return 0 if r["ok"] else 1
    finally:
        pf.terminate()


if __name__ == "__main__":
    sys.exit(main())
