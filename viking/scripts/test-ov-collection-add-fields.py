#!/usr/bin/env python3
"""Tests for ov-collection-add-fields.py (BUG-1271). Stdlib only, no cluster:

    python3 test-ov-collection-add-fields.py

The fixtures are test-ov-collection-schema-check.py's: prod's `context` had 21
fields and no `md5`, v0.5.1.1's schema has 22. run() and main() are tested with
the ConfigMap read, the port-forward and the adapter mocked.
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


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


af = load("add_fields", "ov-collection-add-fields.py")
fixtures = load("schema_check_tests", "test-ov-collection-schema-check.py")
RELEASE, PROD, CONF, field = (
    fixtures.RELEASE, fixtures.PROD, fixtures.CONF, fixtures.field
)  # fmt: skip
MD5 = next(f for f in RELEASE if f["FieldName"] == "md5")


class RunTests(unittest.TestCase):
    def run_with(self, *reads, apply=False, collection=None):
        """run() against a collection whose metadata reads return `reads` in turn."""
        coll = mock.Mock(**{
            "get_meta_data.side_effect": [{"Fields": r} for r in reads],
        })  # fmt: skip
        adapter = mock.Mock(**{"get_collection.return_value": coll})
        cls = mock.Mock(return_value=adapter)
        schema = mock.Mock(return_value={"Fields": RELEASE})
        canary = mock.Mock(
            read_ov_conf=mock.Mock(return_value=CONF),
            target_from_conf=af.sc.load_canary().target_from_conf,
            forward=mock.Mock(return_value=40000),
            adapter_class=mock.Mock(return_value=cls),
        )
        with mock.patch.object(af.sc, "load_canary", return_value=canary), \
             mock.patch.object(af.sc, "release_schema", return_value=(schema, "v0.5.1.1")):  # fmt: skip
            out = af.run("openviking-standalone-config", collection, apply)
        return out, coll, cls, schema

    def test_a_dry_run_names_the_field_and_changes_nothing(self):
        out, coll, _, _ = self.run_with(PROD)
        coll.update.assert_not_called()
        self.assertEqual(
            {k: out[k] for k in ("collection", "missing", "add", "applied", "failed")},
            {"collection": "context", "missing": ["md5"], "add": [MD5],
             "applied": False, "failed": []},
        )  # fmt: skip

    def test_apply_sends_only_the_missing_fields_and_checks_they_took(self):
        # The vectordb service appends the Fields it is sent to the collection's
        # list, so sending the whole release schema would duplicate every field.
        out, coll, _, _ = self.run_with(PROD, RELEASE, apply=True)
        coll.update.assert_called_once_with(fields=[MD5])
        self.assertEqual((out["applied"], out["live_fields_after"]), (True, 22))

    def test_an_update_the_service_dropped_is_an_error(self):
        # UpdateVikingdbCollection reports success when its own validation
        # rejects the fields, so only a fresh metadata read shows the outcome.
        with self.assertRaisesRegex(af.sc.CheckError, "still missing: md5"):
            self.run_with(PROD, PROD, apply=True)

    def test_an_update_that_lost_fields_is_an_error(self):
        with self.assertRaisesRegex(af.sc.CheckError, "lost: abstract"):
            self.run_with(PROD, [f for f in RELEASE if f["FieldName"] != "abstract"],
                          apply=True)  # fmt: skip

    def test_a_retyped_field_is_refused_even_with_apply(self):
        live = [f if f["FieldName"] != "meta" else field("meta") for f in PROD]
        out, coll, _, _ = self.run_with(live, apply=True)
        coll.update.assert_not_called()
        self.assertEqual(out["failed"], ["retyped: meta"])
        self.assertFalse(out["applied"])

    def test_nothing_missing_sends_nothing(self):
        out, coll, _, _ = self.run_with(RELEASE, apply=True)
        coll.update.assert_not_called()
        self.assertEqual((out["add"], out["applied"], out["failed"]), ([], False, []))

    def test_a_named_collection_replaces_the_configmaps(self):
        # The rehearsal runs against a scratch collection on the test vectordb.
        out, _, cls, schema = self.run_with(PROD, collection="bug1271_rehearsal")
        self.assertEqual(cls.call_args.args[3], "bug1271_rehearsal")
        self.assertEqual(schema.call_args.args, ("bug1271_rehearsal", 2560))
        self.assertEqual(out["collection"], "bug1271_rehearsal")


class MainTests(unittest.TestCase):
    def run_main(self, argv, result=None):
        run = mock.Mock(return_value=result or {"failed": []})
        buf = io.StringIO()
        with mock.patch.object(af, "run", run), contextlib.redirect_stdout(buf), \
             contextlib.redirect_stderr(io.StringIO()):  # fmt: skip
            try:
                code = af.main(argv)
            except SystemExit as e:
                code = e.code
        return code, buf.getvalue(), run

    def test_dry_run_is_the_default(self):
        code, out, run = self.run_main([])
        run.assert_called_once_with("openviking-standalone-config", None, False)
        self.assertEqual((code, json.loads(out)["apply"]), (0, False))

    def test_apply_and_collection_pass_through(self):
        _, out, run = self.run_main(
            ["--configmap", "openviking-test-config", "--collection", "x", "--apply"]
        )
        run.assert_called_once_with("openviking-test-config", "x", True)
        self.assertEqual(json.loads(out)["collection_override"], "x")

    def test_a_refusal_exits_3_with_one_receipt(self):
        code, out, _ = self.run_main(["--apply"], {"failed": ["retyped: meta"]})
        self.assertEqual((code, len(out.splitlines())), (3, 1))

    def test_bad_arguments_exit_1_with_a_receipt(self):
        code, out, run = self.run_main(["--collection"])
        self.assertEqual(code, 1)
        self.assertTrue(json.loads(out)["error"].startswith("bad arguments"))
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
