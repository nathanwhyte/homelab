#!/usr/bin/env python3
"""Minimal stdlib-only S3 client for the OpenViking AGFS bucket (IMPR-1095).

The OV image ships no S3 SDK (no boto3/botocore/minio — the server rolls its
own client), so the exporter sidecar and the ovlock-janitor CronJob use this
SigV4 + ListObjectsV2/GetObject/DeleteObject subset instead. Garage speaks
standard SigV4; nothing here is Garage-specific beyond defaulting the region.

Deliberately tiny: only what lock sweeping needs. Not a general S3 client.

Both consumers mount this file next to their entry script (same ConfigMap
directory), so a plain `import s3lite` works in-pod and in-repo alike.
"""

from __future__ import annotations

import datetime
import email.utils
import hashlib
import hmac
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


class S3Error(Exception):
    """Any failure talking to the S3 endpoint (network, HTTP, XML)."""


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def _uri_encode(value: str, *, is_key: bool = False) -> str:
    # SigV4 canonical URI encoding: RFC 3986 unreserved chars stay literal;
    # object keys keep their `/` separators unencoded.
    safe = "-._~/" if is_key else "-._~"
    return urllib.parse.quote(value, safe=safe)


class S3Client:
    """SigV4-signed requests against one S3-compatible endpoint."""

    def __init__(
        self,
        endpoint: str,
        region: str,
        access_key: str,
        secret_key: str,
        timeout: float = 15.0,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.region = region
        self.access_key = access_key
        self.secret_key = secret_key
        self.timeout = timeout
        self._host = urllib.parse.urlparse(self.endpoint).netloc

    # ── Signing ──────────────────────────────────────────────────────────

    def _signed_request(
        self, method: str, path: str, query: dict[str, str]
    ) -> urllib.request.Request:
        now = datetime.datetime.now(datetime.timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        datestamp = now.strftime("%Y%m%d")

        canonical_uri = _uri_encode(path, is_key=True)
        canonical_query = "&".join(
            f"{_uri_encode(k)}={_uri_encode(v)}" for k, v in sorted(query.items())
        )
        headers = {
            "host": self._host,
            "x-amz-content-sha256": _EMPTY_SHA256,
            "x-amz-date": amz_date,
        }
        canonical_headers = "".join(f"{k}:{v}\n" for k, v in sorted(headers.items()))
        signed_headers = ";".join(sorted(headers))
        canonical_request = "\n".join(
            [
                method,
                canonical_uri,
                canonical_query,
                canonical_headers,
                signed_headers,
                _EMPTY_SHA256,
            ]
        )

        scope = f"{datestamp}/{self.region}/s3/aws4_request"
        string_to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256",
                amz_date,
                scope,
                hashlib.sha256(canonical_request.encode()).hexdigest(),
            ]
        )
        key = _hmac(f"AWS4{self.secret_key}".encode(), datestamp)
        key = _hmac(key, self.region)
        key = _hmac(key, "s3")
        key = _hmac(key, "aws4_request")
        signature = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()

        url = f"{self.endpoint}{canonical_uri}"
        if canonical_query:
            url += f"?{canonical_query}"
        req = urllib.request.Request(url, method=method)
        req.add_header("x-amz-date", amz_date)
        req.add_header("x-amz-content-sha256", _EMPTY_SHA256)
        req.add_header(
            "Authorization",
            f"AWS4-HMAC-SHA256 Credential={self.access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}",
        )
        return req

    def _send(self, method: str, path: str, query: dict[str, str]) -> bytes:
        return self._send_meta(method, path, query)[0]

    def _send_meta(
        self, method: str, path: str, query: dict[str, str]
    ) -> tuple[bytes, datetime.datetime | None]:
        """Like _send, but also returns the parsed Last-Modified header."""
        req = self._signed_request(method, path, query)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read()
                last_modified = None
                header = resp.headers.get("Last-Modified")
                if header:
                    try:
                        last_modified = email.utils.parsedate_to_datetime(header)
                    except (TypeError, ValueError):
                        last_modified = None
                return body, last_modified
        except urllib.error.HTTPError as e:
            raise S3Error(f"{method} {path}: HTTP {e.code} {e.read()[:200]!r}") from e
        except (urllib.error.URLError, OSError) as e:
            raise S3Error(f"{method} {path}: {e}") from e

    # ── Operations ───────────────────────────────────────────────────────

    def list_objects(self, bucket: str) -> list[dict]:
        """All objects in the bucket as dicts with Key/LastModified/Size/ETag."""
        objects: list[dict] = []
        token: str | None = None
        while True:
            query = {"list-type": "2"}
            if token:
                query["continuation-token"] = token
            body = self._send("GET", f"/{bucket}", query)
            try:
                root = ET.fromstring(body)
            except ET.ParseError as e:
                raise S3Error(f"ListObjectsV2 XML parse failed: {e}") from e
            ns = root.tag.partition("}")[0] + "}" if root.tag.startswith("{") else ""
            for contents in root.iter(f"{ns}Contents"):
                objects.append(
                    {
                        "Key": contents.findtext(f"{ns}Key"),
                        "LastModified": datetime.datetime.fromisoformat(
                            contents.findtext(f"{ns}LastModified").replace(
                                "Z", "+00:00"
                            )
                        ),
                        "Size": int(contents.findtext(f"{ns}Size") or 0),
                        "ETag": contents.findtext(f"{ns}ETag"),
                    }
                )
            if root.findtext(f"{ns}IsTruncated") == "true":
                token = root.findtext(f"{ns}NextContinuationToken")
                if not token:
                    raise S3Error("truncated listing without continuation token")
            else:
                return objects

    def get_object(self, bucket: str, key: str) -> bytes:
        return self._send("GET", f"/{bucket}/{key}", {})

    def get_object_meta(
        self, bucket: str, key: str
    ) -> tuple[bytes, datetime.datetime | None]:
        """Object body plus its Last-Modified (second precision, or None)."""
        return self._send_meta("GET", f"/{bucket}/{key}", {})

    def delete_object(self, bucket: str, key: str) -> None:
        self._send("DELETE", f"/{bucket}/{key}", {})


def is_lock_key(key: str) -> bool:
    """True for OpenViking lock objects: `.path.ovlock` / `.exact.ovlock.*`."""
    basename = key.rsplit("/", 1)[-1]
    return basename == ".path.ovlock" or basename.startswith(".exact.ovlock.")
