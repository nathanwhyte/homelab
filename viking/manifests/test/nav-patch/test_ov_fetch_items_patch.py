"""Parser, request and guard checks; the set_tags round trip is a live ov-test run."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "_scripts" / "lib"))
from git_env import scrub_environ  # noqa: E402

scrub_environ()

import json
import os
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ov_fetch_items_patch as fetch_patch


class Result:
    def __init__(self):
        self.items = []
        self.ids_not_exist = []


class Item:
    def __init__(self, id, fields):
        self.id = id
        self.fields = fields


def parse(data):
    return fetch_patch.parse_fetch_response(data, Result, Item)


class Response:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self.text = json.dumps(body)


def fake_module(response):
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return response

    class HttpCollection:
        url_prefix = "http://vdb:5000/"
        project_name = "default"
        collection_name = "context"

        def fetch_data(self, primary_keys):
            return "original"

    module = types.SimpleNamespace(
        requests=types.SimpleNamespace(get=get),
        headers={"Content-Type": "application/json"},
        DEFAULT_TIMEOUT=30,
        FetchDataInCollectionResult=Result,
        DataItem=Item,
        HttpCollection=HttpCollection,
    )
    return module, calls


class ParseTests(unittest.TestCase):
    def test_reads_items_as_served_by_ov_vectordb(self):
        got = parse({"items": [{"id": "a", "fields": {"vector": [0.1], "uri": "/u"}}]})
        self.assertEqual(
            [(i.id, i.fields) for i in got.items],
            [("a", {"vector": [0.1], "uri": "/u"})],
        )

    def test_reads_fetch_and_prefers_it_over_items(self):
        got = parse({"fetch": [{"id": "f"}], "items": [{"id": "i"}]})
        self.assertEqual([i.id for i in got.items], ["f"])
        self.assertIsNone(got.items[0].fields)

    def test_ids_not_exist_and_odd_shapes(self):
        got = parse({"items": [], "ids_not_exist": ["x"]})
        self.assertEqual((got.items, got.ids_not_exist), ([], ["x"]))
        self.assertEqual(parse([{"id": "a"}]).items, [])
        self.assertEqual(parse({}).items, [])


class FetchDataTests(unittest.TestCase):
    def test_sends_the_upstream_request_and_parses_items(self):
        module, calls = fake_module(
            Response(200, {"code": 0, "data": {"items": [{"id": "a", "fields": {}}]}})
        )
        fetch = fetch_patch.make_fetch_data(module)
        got = fetch(module.HttpCollection(), ["a"])
        self.assertEqual([i.id for i in got.items], ["a"])
        url, kwargs = calls[0]
        self.assertEqual(url, "http://vdb:5000/api/vikingdb/data/fetch_in_collection")
        self.assertEqual(
            kwargs["params"],
            {"project": "default", "collection_name": "context", "ids": '["a"]'},
        )
        self.assertEqual(kwargs["timeout"], 30)

    def test_non_200_is_an_empty_result(self):
        module, _ = fake_module(Response(500, {"data": {"items": [{"id": "a"}]}}))
        got = fetch_patch.make_fetch_data(module)(module.HttpCollection(), ["a"])
        self.assertEqual(got.items, [])


class ApplyTests(unittest.TestCase):
    def test_guards_and_idempotence(self):
        module, _ = fake_module(Response(200, {"data": {"items": []}}))
        original = module.HttpCollection.fetch_data
        ov = types.SimpleNamespace(__version__="v0.4.99")
        with (
            patch.dict(sys.modules, {"openviking": ov}),
            patch.dict(os.environ, {}, clear=True),
        ):
            self.assertFalse(fetch_patch.apply(module))
            ov.__version__ = fetch_patch.EXPECTED_VERSION
            self.assertFalse(fetch_patch.apply(module))
            digest = fetch_patch.hashlib.sha256(
                fetch_patch.inspect.getsource(original).encode()
            ).hexdigest()
            with patch.object(fetch_patch, "EXPECTED_SHA256", digest):
                with patch.dict(os.environ, {"OV_FETCH_ITEMS_PATCH": "0"}):
                    self.assertFalse(fetch_patch.apply(module))
                self.assertIs(module.HttpCollection.fetch_data, original)
                self.assertTrue(fetch_patch.apply(module))
                installed = module.HttpCollection.fetch_data
                self.assertTrue(installed._ov_fetch_items_patch)
                self.assertTrue(fetch_patch.apply(module))
                self.assertIs(module.HttpCollection.fetch_data, installed)


if __name__ == "__main__":
    unittest.main()
