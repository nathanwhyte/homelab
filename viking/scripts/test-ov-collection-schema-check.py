#!/usr/bin/env python3
"""Tests for ov-collection-schema-check.py (IMPR-1431 row 2). Stdlib only, no cluster:

    python3 test-ov-collection-schema-check.py

The fixtures are the shapes read live on 2026-10-26: prod's `context` collection
had 21 fields and no `md5`, while v0.5.1.1's `CollectionSchemas.context_collection`
has 22 (BUG-1271). run() and main() are tested with their collaborators mocked.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_scripts" / "lib"))
from git_env import scrub_environ  # noqa: E402

scrub_environ()

import contextlib
import importlib.util
import io
import json
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "schema_check", HERE / "ov-collection-schema-check.py"
)
sc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sc)


def field(name, kind="string", **extra):
    return {"FieldName": name, "FieldType": kind, **extra}


# v0.5.1.1's context collection, in its own order.
RELEASE = [
    field("id", IsPrimaryKey=True), field("uri", "path"), field("type"),
    field("context_type"), field("vector", "vector", Dim=2560),
    field("sparse_vector", "sparse_vector"), field("created_at", "date_time"),
    field("updated_at", "date_time"), field("active_count", "int64"),
    field("level", "int64"), field("name"), field("description"), field("tags"),
    field("search_tags", "list<string>"), field("abstract"), field("content", "text"),
    field("md5", DefaultValue=""), field("account_id"), field("owner_user_id"),
    field("owner_agent_id"), field("owner_space"), field("meta", "text"),
]  # fmt: skip
PROD = [f for f in RELEASE if f["FieldName"] != "md5"]
CONF = {
    "default_account": "default",
    "default_user": "noot",
    "storage": {
        "vectordb": {
            "backend": "http",
            "name": "context",
            "dimension": 2560,
            "url": "http://ov-vectordb.viking.svc.cluster.local:5000",
        }
    },
}


class CompareTests(unittest.TestCase):
    def test_todays_prod_is_missing_md5(self):
        # BUG-1271: the in-place upgrade kept the pre-v0.5.1.1 collection.
        self.assertEqual(len(PROD), 21)
        self.assertEqual(
            sc.compare(PROD, RELEASE), {"missing": ["md5"], "extra": [], "retyped": {}}
        )

    def test_the_same_schema_has_no_drift(self):
        self.assertEqual(
            sc.compare(list(reversed(RELEASE)), RELEASE),
            {"missing": [], "extra": [], "retyped": {}},
        )

    def test_extra_and_retyped_fields_are_reported(self):
        live = [*RELEASE[:-1], field("meta", "string"), field("legacy")]
        self.assertEqual(
            sc.compare(live, RELEASE),
            {"missing": [], "extra": ["legacy"],
             "retyped": {"meta": {"live": "string", "release": "text"}}},
        )  # fmt: skip

    def test_only_missing_or_retyped_fields_fail(self):
        # An extra live field is one the release no longer writes: kept, not a fault.
        self.assertEqual(sc.drift({"missing": [], "extra": ["x"], "retyped": {}}), [])
        self.assertEqual(
            sc.drift({"missing": ["md5"], "extra": [], "retyped": {"meta": {}}}),
            ["missing: md5", "retyped: meta"],
        )

    def test_nameless_entries_are_ignored(self):
        self.assertEqual(sc.field_types([{"FieldType": "x"}, "junk", field("a")]),
                         {"a": "string"})  # fmt: skip


class RunTests(unittest.TestCase):
    def run_with(self, live, conf=CONF):
        collection = mock.Mock(**{"get_meta_data.return_value": {"Fields": live}})
        adapter = mock.Mock(**{"get_collection.return_value": collection})
        cls = mock.Mock(return_value=adapter)
        schema = mock.Mock(return_value={"Fields": RELEASE})
        forward = mock.Mock(return_value=40000)
        canary = mock.Mock(
            read_ov_conf=mock.Mock(return_value=conf),
            target_from_conf=sc.load_canary().target_from_conf,
            forward=forward,
            adapter_class=mock.Mock(return_value=cls),
        )
        with mock.patch.object(sc, "load_canary", return_value=canary), \
             mock.patch.object(sc, "release_schema", return_value=(schema, "v0.5.1.1")):  # fmt: skip
            out = sc.run("openviking-standalone-config")
        return out, forward, cls, schema

    def test_reads_the_named_configmaps_collection_read_only(self):
        out, forward, cls, schema = self.run_with(PROD)
        forward.assert_called_once()
        self.assertEqual(forward.call_args.args[:2], ("svc/ov-vectordb", 5000))
        self.assertEqual(cls.call_args.args, ("127.0.0.1", 40000, "default", "context",
                                              "default"))  # fmt: skip
        self.assertEqual(schema.call_args.args, ("context", 2560))
        self.assertEqual(
            {k: out[k] for k in ("collection", "release", "live_fields",
                                 "release_fields", "missing", "failed")},
            {"collection": "context", "release": "v0.5.1.1", "live_fields": 21,
             "release_fields": 22, "missing": ["md5"], "failed": ["missing: md5"]},
        )  # fmt: skip

    def test_a_collection_with_no_fields_is_an_error_not_a_pass(self):
        # An empty or unreadable meta would otherwise report every field missing,
        # or, with no release fields, nothing at all.
        for meta in (None, {}, {"Fields": []}):
            collection = mock.Mock(**{"get_meta_data.return_value": meta})
            with self.assertRaises(sc.CheckError, msg=meta):
                sc.live_fields(collection)


class MainTests(unittest.TestCase):
    def run_main(self, effect, argv=()):
        buf = io.StringIO()
        with mock.patch.object(sc, "run", side_effect=effect), \
             contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):  # fmt: skip
            code = sc.main(list(argv))
        return code, buf.getvalue()

    def test_drift_exits_3_with_one_json_receipt(self):
        code, out = self.run_main(
            lambda cm: {"configmap": cm, "failed": ["missing: md5"]}
        )
        self.assertEqual(code, 3)
        self.assertEqual(len(out.splitlines()), 1)
        self.assertEqual(json.loads(out)["configmap"], "openviking-standalone-config")

    def test_no_drift_exits_0_and_the_configmap_is_selectable(self):
        code, out = self.run_main(lambda cm: {"configmap": cm, "failed": []},
                                  ["--configmap", "openviking-test-config"])  # fmt: skip
        self.assertEqual(
            (code, json.loads(out)["configmap"]), (0, "openviking-test-config")
        )

    def test_an_error_exits_1_with_a_receipt(self):
        def fail(cm):
            raise sc.CheckError("openviking is not importable")

        code, out = self.run_main(fail)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["error"], "openviking is not importable")


if __name__ == "__main__":
    unittest.main()
