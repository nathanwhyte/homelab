#!/usr/bin/env -S uv run --with botocore python3
"""
Drain the `homelab` R2 bucket's `media/` subtree and report per-category size.

Why this exists: macOS LibreSSL has a TLS handshake failure with the
S3-compatible endpoint when the host is wrong. The fix is to use the
account-id-based hostname (`<ACCOUNT_ID>.r2.cloudflarestorage.com`, not
`<bucket>.r2.cloudflarestorage.com`) and sign with SigV4 over TLS 1.3.

The cf CLI can do bucket-level metadata (size, object count) but not per-prefix
breakdowns, so this script fills that gap. Read-only — no writes.

Usage:
    # 1. Get your R2 access key + secret from the Cloudflare dashboard
    #    (R2 → homelab → Manage R2 API Tokens → Create Account API token)
    # 2. Get the account ID: `cf context show` (under accountId.value)
    # 3. export R2_AK=... R2_SK=... R2_ACCOUNT_ID=...
    # 4. uv run scripts/r2-prefix-sizes.py
"""

import datetime
import hashlib
import hmac
import os
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter

AK = os.environ.get("R2_AK", "")
SK = os.environ.get("R2_SK", "")
ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "")
BUCKET = os.environ.get("R2_BUCKET", "homelab")
MEDIA_PREFIX = "backups/cluster/homelab-k3s/volumes/archive/media/"

if not (AK and SK and ACCOUNT_ID):
    raise SystemExit("Set R2_AK, R2_SK, R2_ACCOUNT_ID in the environment")

HOST = f"{ACCOUNT_ID}.r2.cloudflarestorage.com"
NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}


def sigv4(canonical_query: str) -> tuple[str, str]:
    now = datetime.datetime.now(datetime.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = amz_date[:8]
    canonical_uri = f"/{BUCKET}"
    canonical_headers = (
        f"host:{HOST}\nx-amz-content-sha256:UNSIGNED-PAYLOAD\nx-amz-date:{amz_date}\n"
    )
    canonical_request = "\n".join(
        [
            "GET",
            canonical_uri,
            canonical_query,
            canonical_headers,
            "host;x-amz-content-sha256;x-amz-date",
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
        f"SignedHeaders=host;x-amz-content-sha256;x-amz-date, Signature={sig}",
        amz_date,
    )


def list_v2(
    prefix: str = "", with_delim: bool = True, continuation_token: str | None = None
):
    time.sleep(1.0)  # avoid sigv4 replay protection on identical-second requests
    params: dict[str, str] = {"list-type": "2", "max-keys": "1000"}
    if with_delim:
        params["delimiter"] = "/"
    if prefix:
        params["prefix"] = prefix
    if continuation_token:
        params["continuation-token"] = continuation_token
    canonical_query = urllib.parse.urlencode(sorted(params.items()))
    auth, amz_date = sigv4(canonical_query)
    url = f"https://{HOST}/{BUCKET}?{canonical_query}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": auth,
            "x-amz-date": amz_date,
            "x-amz-content-sha256": "UNSIGNED-PAYLOAD",
            "Host": HOST,
        },
        method="GET",
    )
    with urllib.request.urlopen(req) as r:
        return ET.fromstring(r.read().decode())


def drain(prefix: str) -> tuple[int, int]:
    """Paged to completion. Returns (total_bytes, total_objects)."""
    total = 0
    objs = 0
    token = None
    while True:
        root = list_v2(prefix, with_delim=False, continuation_token=token)
        for c in root.findall("s3:Contents", NS):
            total += int(c.find("s3:Size", NS).text)
            objs += 1
        if root.get("IsTruncated") == "true":
            token = root.get("NextContinuationToken")
        else:
            return total, objs


def main():
    print(f"Listing R2 bucket `{BUCKET}` (account {ACCOUNT_ID}) media subtree:\n")
    root = list_v2(MEDIA_PREFIX)
    sub_prefixes = [p.text for p in root.findall("s3:CommonPrefixes/s3:Prefix", NS)]
    grand_total = 0
    grand_objs = 0
    for sp in sub_prefixes:
        size, objs = drain(sp)
        grand_total += size
        grand_objs += objs
        label = sp.replace(MEDIA_PREFIX, "")
        print(f"  {label:<50} {size / 1e9:>8.3f} GB   {objs:>6} objs")
    print()
    print(f"  TOTAL: {grand_total / 1e9:.3f} GB / {grand_objs} objects")


if __name__ == "__main__":
    main()
