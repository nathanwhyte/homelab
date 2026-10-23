"""Tests for ov-agfs-export.py against an in-memory S3 fake. Stdlib only — runs anywhere:

    python3 test-ov-agfs-export.py

The fixture bucket carries the two shapes IMPR-1152 lost with `aws s3 sync`: a key
that is also another key's prefix (`default/_system` and `default/_system/x`), and
zero-byte objects.
"""

import hashlib
import importlib.util
import io
import os
import tempfile
import unittest
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_SPEC = importlib.util.spec_from_file_location(
    "ov_agfs_export", os.path.join(_HERE, "ov-agfs-export.py")
)
ax = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ax)


class FakeS3:
    """The four calls the tool makes, with list pages of two keys to exercise paging.

    The listed ETag is the quoted md5, as Garage serves it for a single-part PUT;
    `etags` overrides it per key, e.g. with a multipart-style `…-2`.
    """

    def __init__(self, buckets):
        self.b = {name: dict(objs) for name, objs in buckets.items()}
        self.etags: dict[str, str] = {}
        self.gets: list[str] = []

    def get_paginator(self, _op):
        fake = self

        class P:
            def paginate(self, Bucket):
                keys = sorted(fake.b[Bucket])
                for i in range(0, max(len(keys), 1), 2):
                    yield {
                        "Contents": [
                            {
                                "Key": k,
                                "Size": len(fake.b[Bucket][k]),
                                "ETag": '"%s"'
                                % fake.etags.get(
                                    k, hashlib.md5(fake.b[Bucket][k]).hexdigest()
                                ),
                            }
                            for k in keys[i : i + 2]
                        ]
                    }

        return P()

    def get_object(self, Bucket, Key):
        self.gets.append(Key)
        return {"Body": io.BytesIO(self.b[Bucket][Key])}

    def put_object(self, Bucket, Key, Body):
        self.b[Bucket][Key] = bytes(Body)

    def delete_object(self, Bucket, Key):
        del self.b[Bucket][Key]


PROD = {
    "default/_system": b"",
    "default/_system/backend_meta.json": b"",
    "default/_system/x": b"payload",
    "default/resources/a.md": b"# A\n",
    "default/resources/empty.md": b"",
}


class RoundTripTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "export"
        self.s3 = FakeS3(
            {
                "prod": PROD,
                "test": {"stale/old.md": b"old", "default/resources/a.md": b"changed"},
            }
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_export_keeps_every_key_including_prefix_keys_and_zero_byte_objects(self):
        m = ax.export(self.s3, "prod", self.dir)
        self.assertEqual(m["objects"], 5)
        self.assertEqual(m["zero_byte"], 3)
        self.assertEqual({e["key"] for e in m["entries"]}, set(PROD))
        self.assertTrue(ax.verify(self.s3, "prod", self.dir)["ok"])

    def test_import_with_delete_makes_the_bucket_equal_the_export(self):
        ax.export(self.s3, "prod", self.dir)
        r = ax.import_(self.s3, "test", self.dir, delete=True)
        self.assertEqual(r, {"put": 5, "deleted": 1})
        self.assertEqual(self.s3.b["test"], PROD)
        self.assertTrue(ax.verify(self.s3, "test", self.dir)["ok"])

    def test_import_without_delete_leaves_extra_keys_and_verify_reports_them(self):
        ax.export(self.s3, "prod", self.dir)
        ax.import_(self.s3, "test", self.dir, delete=False)
        r = ax.verify(self.s3, "test", self.dir)
        self.assertFalse(r["ok"])
        self.assertEqual(r["extra"], ["stale/old.md"])

    def test_verify_catches_a_changed_object_of_the_same_size(self):
        ax.export(self.s3, "prod", self.dir)
        self.s3.b["prod"]["default/resources/a.md"] = b"# B\n"
        for deep in (False, True):
            r = ax.verify(self.s3, "prod", self.dir, deep=deep)
            self.assertFalse(r["ok"])
            self.assertEqual(r["differ"], ["default/resources/a.md"])

    def test_verify_by_etag_makes_no_gets_except_for_multipart_etags(self):
        self.s3.etags["default/_system/x"] = "abc-2"
        ax.export(self.s3, "prod", self.dir)
        self.s3.gets.clear()
        r = ax.verify(self.s3, "prod", self.dir)
        self.assertTrue(r["ok"])
        self.assertEqual(self.s3.gets, ["default/_system/x"])
        self.assertEqual((r["checked_by_etag"], r["checked_by_get"]), (4, 1))

    def test_export_with_base_fetches_only_new_changed_and_multipart_keys(self):
        self.s3.etags["default/_system/x"] = "abc-2"
        base = Path(self.tmp.name) / "base"
        ax.export(self.s3, "prod", base)
        self.s3.b["prod"]["default/resources/a.md"] = b"# A, edited\n"
        self.s3.b["prod"]["default/resources/new.md"] = b"new"
        del self.s3.b["prod"]["default/resources/empty.md"]
        self.s3.gets.clear()
        m = ax.export(self.s3, "prod", self.dir, base=base)
        self.assertEqual(
            sorted(self.s3.gets),
            ["default/_system/x", "default/resources/a.md", "default/resources/new.md"],
        )
        self.assertEqual((m["fetched"], m["reused"], m["objects"]), (3, 2, 5))
        self.assertTrue(ax.verify(self.s3, "prod", self.dir, deep=True)["ok"])


if __name__ == "__main__":
    unittest.main()
